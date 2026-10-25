"""Authentication, isolation, and factual read-only Mini App responses."""

from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import yaml

from curatarr.adapters import AdapterError
from curatarr.cli import main
from curatarr.config import parse_config
from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaType
from curatarr.services.identity import IdentityMatcher
from curatarr.services.search import SearchDecisionService
from curatarr.storage import Storage
from curatarr.web.app import create_app
from curatarr.web.auth import AuthenticationError, validate_init_data
from test_config import VALID_CONFIG
from test_telegram_service import _FakeMetadataAdapter, _FakeRyotAdapter, _FakeRadarrAdapter, _movie


TOKEN = "123:test-token"


def signed_data(user_id=1, *, timestamp=None, **extra):
    fields = {"auth_date": str(int(time.time()) if timestamp is None else timestamp),
              "user": json.dumps({"id": user_id, "first_name": "Test"}), **extra}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniAppAuthTests(unittest.TestCase):
    def test_valid_signed_identity_and_additional_fields(self):
        self.assertEqual(validate_init_data(signed_data(signature="future-field", query_id="abc"), TOKEN,
                                           frozenset({1})), 1)

    def test_invalid_launch_data_never_authenticates(self):
        now = int(time.time())
        cases = ["", "broken", signed_data(3), signed_data(True), signed_data(timestamp=now - 3601),
                 signed_data(timestamp=now + 31), signed_data() + "&auth_date=1",
                 signed_data().replace("Test", "Forged"), "x" * 8193,
                 "hash=x&auth_date=1&user=%7B%7D"]
        for raw in cases:
            with self.subTest(raw=raw[:30]), self.assertRaises(AuthenticationError):
                validate_init_data(raw, TOKEN, frozenset({1, 2}), now=now)
        with self.assertRaises(AuthenticationError):
            validate_init_data(signed_data(), "wrong-token", frozenset({1}))


class MiniAppTests(unittest.TestCase):
    def setUp(self):
        config = yaml.safe_load(VALID_CONFIG)
        config["telegram"]["bot_token"] = TOKEN
        self.config = parse_config(config)
        self.candidate = replace(_movie(), poster_url="https://image.tmdb.org/t/p/w500/poster.jpg",
                                 overview="A movie description.")
        self.metadata = _FakeMetadataAdapter((self.candidate,))
        self.ryot = _FakeRyotAdapter(None)
        self.arr = _FakeRadarrAdapter(None)
        self.service = SearchDecisionService(self.metadata, self.ryot, IdentityMatcher(), self.arr)
        self.client = TestClient(create_app(self.config, service=self.service))
        self.headers = {"Authorization": f"tma {signed_data()}"}

    def search(self):
        response = self.client.get("/api/search?q=Spider-Man", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def get(self, result, endpoint="details", **kwargs):
        return self.client.get(f'/api/search/{result["search_id"]}/0/{endpoint}',
                               headers=kwargs.pop("headers", self.headers), **kwargs)

    def test_shell_is_public_but_all_data_requires_signed_allowlisted_identity(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/search?q=a", headers={"Authorization": "tma forged"}).status_code, 401)
        response = self.client.get("/api/me", headers=self.headers)
        self.assertEqual(response.json(), {"history_scope": "shared_household", "read_only": True, "display_name": "Household",
                                          "is_admin": True, "maintenance_available": False, "show_sync_diagnostics": False})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("object-src 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_summary_includes_counts_and_ready_copy_even_if_arr_is_down(self):
        class Playback:
            def playback_facts(self, candidate):
                return {"status": "ready", "version_count": 1,
                        "versions": [{"audio_languages": ["eng", "fra"], "audio_complete": True}]}
        self.ryot.item = replace(_movie(durable_states={DurableState.LIBRARY, DurableState.WATCHED, DurableState.PREVIOUSLY_ACQUIRED}),
                                 completed_watch_count=2, last_watched_date="2024-06-01")
        self.client = TestClient(create_app(self.config, service=self.service, playback_adapter=Playback()))
        result = self.search()
        with patch.object(self.arr, "lookup_movie", side_effect=AdapterError("down")):
            data = self.get(result, "status").json()
        self.assertEqual(data["availability"]["label"], "Ready to watch")
        self.assertEqual(data["history"]["watch_count"], 2)
        self.assertEqual(data["history"]["last_watched_date"], "2024-06-01")
        self.assertEqual(data["playback"]["versions"][0]["audio_languages"], ["eng", "fra"])
        self.assertIsNone(data["decision_path"])

    def test_unconfigured_or_unavailable_playback_never_becomes_ready_from_arr_file(self):
        self.arr.item = _movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)
        self.assertEqual(self.get(self.search(), "status").json()["availability"],
                         {"code": "unknown", "label": "Check unavailable"})

    def test_jellyfin_absence_is_independent_of_arr_acquisition_and_outages(self):
        class Playback:
            def playback_facts(self, candidate):
                return {"status": "not_present", "version_count": 0, "versions": []}
        self.client = TestClient(create_app(self.config, service=self.service, playback_adapter=Playback()))
        result = self.search()
        for state in (ArrRuntimeState.ACTIVE_MISSING_MONITORED, ArrRuntimeState.ACTIVE_MISSING_UNMONITORED,
                      ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED):
            self.arr.item = _movie(runtime_state=state)
            for endpoint in ("status", "details"):
                with self.subTest(state=state, endpoint=endpoint):
                    self.assertEqual(self.get(result, endpoint).json()["availability"],
                                     {"code": "not_available", "label": "Not available"})
        with patch.object(self.arr, "lookup_movie", side_effect=AdapterError("down")):
            self.assertEqual(self.get(result, "status").json()["availability"],
                             {"code": "not_available", "label": "Not available"})

    def test_profile_read_is_detail_only_and_failure_preserves_file_and_readiness(self):
        class Playback:
            def playback_facts(self, candidate):
                return {"status": "ready", "version_count": 1, "versions": []}
        self.arr.item = replace(_movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED), quality_profile_id=3)
        self.client = TestClient(create_app(self.config, service=self.service, playback_adapter=Playback()))
        result = self.search()
        with patch.object(self.arr, "quality_profile", create=True, return_value={
                "name": "Movies > 4K", "upgrade_allowed": True, "cutoff_quality": "Remux-2160p"}) as read:
            self.get(result, "status")
            read.assert_not_called()
            details = self.get(result).json()
            self.assertEqual(details["radarr"]["profile"]["cutoff_quality"], "Remux-2160p")
            read.assert_called_once_with(3)
        with patch.object(self.arr, "quality_profile", create=True, side_effect=AdapterError("secret")):
            data = self.get(result).json()
        self.assertEqual(data["radarr"]["profile_status"], "unavailable")
        self.assertEqual(data["radarr"]["status"], "matched")
        self.assertTrue(data["radarr"]["file_present"])
        self.assertEqual(data["availability"]["code"], "ready")
        self.assertNotIn("secret", json.dumps(data))

    def test_cli_starts_loopback_web_runtime_without_starting_bot(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            with patch("curatarr.cli.load_config", return_value=self.config), \
                    patch("curatarr.web.app.create_app", return_value=object()) as build, \
                    patch("uvicorn.run") as run, patch("curatarr.runtime.build_telegram_bot") as bot:
                self.assertEqual(main(["--config", str(config_path), "--run-web"]), 0)
                self.assertEqual(build.call_args.kwargs["database"], Path(temp).resolve() / "data" / "curatarr.sqlite")
                self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
                self.assertFalse(run.call_args.kwargs["access_log"])
                self.assertFalse(run.call_args.kwargs["proxy_headers"])
                bot.assert_not_called()

    def test_native_file_score_reads_are_detail_only_and_failure_preserves_other_facts(self):
        self.arr.item = replace(_movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED),
                                identities=frozenset({MediaIdentity("tmdb", "558"), MediaIdentity("radarr", "123")}),
                                quality_profile_id=3, movie_file_id=8)
        result = self.search()
        with patch.object(self.arr, "quality_profile", create=True, return_value={"name": "Movies", "format_items": []}), \
                patch.object(self.arr, "file_details", create=True, return_value={"score": 0}) as read:
            self.get(result, "status")
            read.assert_not_called()
            data = self.get(result).json()
            read.assert_called_once_with(8, 123)
            self.assertEqual(data["radarr"]["file"]["score"], 0)
        with patch.object(self.arr, "quality_profile", create=True, return_value={"name": "Movies", "format_items": []}), \
                patch.object(self.arr, "file_details", create=True, side_effect=AdapterError("secret")):
            data = self.get(result).json()
            self.assertEqual(data["radarr"]["file_status"], "unavailable")
            self.assertEqual(data["radarr"]["status"], "matched")
            self.assertTrue(data["radarr"]["file_present"])
            self.assertEqual(data["radarr"]["upgrade_goals"][0]["status"], "unknown")
            self.assertNotIn("secret", json.dumps(data))

    def test_missing_or_uncertain_record_never_reads_other_movie_file(self):
        self.arr.item = replace(_movie(identities={MediaIdentity("tmdb", "other"), MediaIdentity("radarr", "123")},
                                      runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED),
                                quality_profile_id=3, movie_file_id=8)
        with patch.object(self.arr, "file_details", create=True, side_effect=AssertionError("Unconfirmed identity")):
            self.assertEqual(self.get(self.search()).json()["radarr"]["status"], "needs_review")

    def test_format_definition_failure_preserves_native_score_and_known_goals(self):
        self.arr.item = replace(_movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED),
                                identities=frozenset({MediaIdentity("tmdb", "558"), MediaIdentity("radarr", "123")}),
                                quality_profile_id=3, movie_file_id=8)
        with patch.object(self.arr, "quality_profile", create=True, return_value={
                "name": "Movies", "cutoff_qualities": [{"resolution": 2160}],
                "format_items": [{"id": 50, "score": 100}]}), \
                patch.object(self.arr, "file_details", create=True, return_value={"score": -5000, "quality": {"resolution": 2160}}), \
                patch.object(self.arr, "core_formats", create=True, side_effect=AdapterError("secret")):
            data = self.get(self.search()).json()["radarr"]
        self.assertEqual(data["status"], "matched")
        self.assertEqual(data["file"]["score"], -5000)
        self.assertEqual(data["upgrade_goals"][0]["status"], "met")
        self.assertEqual(data["upgrade_goals"][-1]["status"], "unknown")

    def test_confirmed_deletion_is_loaded_from_durable_evidence_and_hidden_for_new_copy(self):
        class Playback:
            status = "not_present"

            def playback_facts(self, candidate):
                return {"status": self.status, "versions": [], "version_count": 0}

        self.ryot.item = _movie(durable_states={DurableState.LIBRARY, DurableState.ARCHIVED, DurableState.PREVIOUSLY_ACQUIRED})
        playback = Playback()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "curatarr.sqlite"
            storage = Storage(path)
            storage.initialize()
            storage.record_confirmed_file_deletion(MediaIdentity("tmdb", "558"), actor="admin", source="admin_verified")
            storage.close()
            self.client = TestClient(create_app(self.config, database=path, service=self.service, playback_adapter=playback))
            result = self.search()
            summary = self.get(result).json()["library_status"]
            self.assertEqual(summary["code"], "archived")
            self.assertTrue(summary["deleted"])
            playback.status = "ready"
            summary = self.get(result).json()["library_status"]
            self.assertFalse(summary["deleted"])
            self.assertIn("Removal needs review", summary["note"])

    def test_actual_ryot_collection_mapping_reaches_api_without_local_deletion_audit(self):
        from curatarr.integrations.ryot import RyotClient, RyotReadAdapter

        names = ["Library", "Archived", "Previously acquired", "Deleted", "Owned", "Completed"]

        def transport(query, variables):
            self.assertNotIn("mutation", query.lower())
            return {"data": {
                "metadataDetails": {"response": {"id": "met_1", "title": "Spider-Man 2", "lot": "MOVIE",
                                                   "identifier": "558", "source": "TMDB"}},
                "userMetadataDetails": {"response": {"hasInteracted": True, "history": [],
                    "collections": [{"details": {"collectionName": name}} for name in names]}}}}

        adapter = RyotReadAdapter(RyotClient(url="http://ryot/graphql", api_key="test", transport=transport))
        adapter._resolved_ids[MediaIdentity("tmdb", "558")] = "met_1"
        service = replace(self.service, ryot_adapter=adapter)

        class Playback:
            status = "not_present"

            def playback_facts(self, candidate):
                return {"status": self.status, "versions": [], "version_count": 0}

        playback = Playback()
        self.client = TestClient(create_app(self.config, service=service, playback_adapter=playback))
        result = self.search()
        for endpoint in ("status", "details"):
            data = self.get(result, endpoint).json()
            self.assertEqual(data["history"]["states"], ["archived", "deleted", "library", "previously_acquired", "watched"])
            self.assertIn("Owned", data["history"]["collections"])
            self.assertIsNone(data["history"]["watch_count"])
            self.assertTrue(data["library_status"]["deleted"])
        playback.status = "ready"
        data = self.get(result).json()
        self.assertFalse(data["library_status"]["deleted"])
        self.assertIn("deleted", data["history"]["states"])
        names[:] = ["Owned"]
        playback.status = "not_present"
        data = self.get(result).json()
        self.assertEqual(data["history"]["states"], [])
        self.assertIn("not in Library", data["library_status"]["note"])
        self.assertFalse(data["library_status"]["deleted"])

    def test_ryot_deletion_does_not_override_identity_review_or_provider_outages(self):
        class Playback:
            def playback_facts(self, candidate):
                return {"status": "not_present", "versions": [], "version_count": 0}

        self.ryot.item = _movie(durable_states={DurableState.LIBRARY, DurableState.ARCHIVED, DurableState.DELETED})
        self.client = TestClient(create_app(self.config, service=self.service, playback_adapter=Playback()))
        result = self.search()
        with patch.object(self.ryot, "get_item", side_effect=AdapterError("private")):
            self.assertFalse(self.get(result).json()["library_status"]["deleted"])
        with patch.object(self.arr, "lookup_movie", side_effect=AdapterError("private")):
            self.assertFalse(self.get(result).json()["library_status"]["deleted"])
        self.ryot.item = replace(self.ryot.item, identities=frozenset({MediaIdentity("tmdb", "999")}))
        data = self.get(result).json()
        self.assertEqual(data["history"]["status"], "needs_review")
        self.assertFalse(data["library_status"]["deleted"])

    def test_public_ratings_are_display_only_and_imdb_requires_matched_identity(self):
        self.metadata = _FakeMetadataAdapter((replace(self.candidate, tmdb_rating=5.46),))
        self.service = replace(self.service, metadata_adapter=self.metadata)
        self.client.close()
        self.client = TestClient(create_app(self.config, service=self.service))
        self.arr.item = replace(_movie(), imdb_rating=7.2)
        result = self.search()
        self.assertEqual(result['candidates'][0]['tmdb_rating'], 5.46)
        self.assertEqual(self.get(result, 'status').json()['radarr']['imdb_rating'], 7.2)
        self.arr.item = replace(_movie(identities={MediaIdentity('tmdb', '999')}), imdb_rating=9)
        self.assertNotIn('imdb_rating', self.get(result, 'status').json()['radarr'])
        self.arr.item = None
        self.assertNotIn('imdb_rating', self.get(result, 'status').json()['radarr'])

    def test_search_is_metadata_only_and_never_returns_credentials(self):
        with patch.object(self.ryot, "get_item", side_effect=AssertionError("Unexpected history read")), \
                patch.object(self.arr, "lookup_movie", side_effect=AssertionError("Unexpected Arr read")):
            result = self.search()
        self.assertEqual(result["candidates"][0]["overview"], self.candidate.overview)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("_item", self.get(result, "history").json())

    def test_searches_are_owner_scoped_and_indexes_bounded(self):
        result = self.search()
        self.assertEqual(self.get(result, headers={"Authorization": f"tma {signed_data(2)}"}).status_code, 404)
        for index in (-1, 1, 999):
            self.assertEqual(self.client.get(f'/api/search/{result["search_id"]}/{index}/details', headers=self.headers).status_code, 404)
        self.assertEqual(self.get({"search_id": "not-a-search"}).status_code, 404)

    def test_expired_search_is_not_usable(self):
        with patch("curatarr.web.app.SEARCH_LIFETIME_SECONDS", -1):
            result = self.search()
        self.assertEqual(self.get(result).status_code, 404)

    def test_unknown_is_not_unwatched_and_lookup_failure_is_not_absence(self):
        result = self.search()
        details = self.get(result).json()
        self.assertEqual(details["history"]["status"], "unknown")
        self.assertEqual(details["decision_path"], "add_new")
        with patch.object(self.ryot, "get_item", side_effect=AdapterError("SECRET")):
            response = self.get(result)
            self.assertEqual(response.json()["history"]["status"], "unavailable")
            self.assertIsNone(response.json()["decision_path"])
            self.assertNotIn("SECRET", response.text)

    def test_completed_and_archived_history_drives_existing_planner(self):
        self.ryot.item = _movie(durable_states={DurableState.WATCHED, DurableState.ARCHIVED})
        result = self.search()
        data = self.get(result).json()
        self.assertEqual(data["history"]["status"], "watched")
        self.assertEqual(data["decision_path"], "reactivate_archived")
        self.assertNotIn("_item", json.dumps(data))
        self.ryot.item = _movie(durable_states={DurableState.LIBRARY})
        self.assertEqual(self.get(result, "history").json()["status"], "no_completed_watch")

    def test_identity_ambiguity_does_not_expose_unconfirmed_facts(self):
        self.ryot.item = _movie(identities={MediaIdentity("tmdb", "other")},
                                durable_states={DurableState.WATCHED})
        data = self.get(self.search()).json()
        self.assertEqual(data["history"]["status"], "needs_review")
        self.assertEqual(data["history"]["states"], [])
        self.assertIsNone(data["decision_path"])

    def test_arr_facts_survive_history_outage_and_history_survives_arr_outage(self):
        self.arr.item = _movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED)
        result = self.search()
        with patch.object(self.ryot, "get_item", side_effect=AdapterError("down")):
            data = self.get(result).json()
            self.assertTrue(data["radarr"]["file_present"])
            self.assertEqual(data["radarr"]["status"], "matched")
            self.assertIsNone(data["decision_path"])

        self.ryot.item = _movie(durable_states={DurableState.WATCHED})
        with patch.object(self.arr, "lookup_movie", side_effect=AdapterError("down")):
            data = self.get(result).json()
            self.assertEqual(data["history"]["status"], "watched")
            self.assertEqual(data["radarr"]["status"], "unavailable")
            self.assertIsNone(data["decision_path"])

    def test_same_numeric_provider_id_on_a_series_is_not_movie_watch_evidence(self):
        self.ryot.item = replace(_movie(durable_states={DurableState.WATCHED}), media_type=MediaType.SERIES)
        data = self.get(self.search()).json()
        self.assertEqual(data["history"]["status"], "needs_review")
        self.assertIsNone(data["decision_path"])

    def test_search_validation_rate_limit_and_upstream_error_sanitization(self):
        for query in ("", "x" * 201):
            self.assertEqual(self.client.get("/api/search", params={"q": query}, headers=self.headers).status_code, 400)
        with patch.object(self.metadata, "search", side_effect=AdapterError("SECRET")):
            response = self.client.get("/api/search?q=a", headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("SECRET", response.text)
        for _ in range(7):
            self.client.get("/api/search?q=a", headers=self.headers)
        self.assertEqual(self.client.get("/api/search?q=a", headers=self.headers).status_code, 429)

    def test_no_write_endpoints_or_pending_decisions(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "state.sqlite"
            self.client = TestClient(create_app(self.config, service=self.service, database=database))
            result = self.search()
            self.assertTrue(self.get(result).json()["read_only"])
            self.assertEqual(self.client.post(f'/api/search/{result["search_id"]}/0/details', headers=self.headers).status_code, 405)
            storage = Storage(database)
            try:
                count = storage.connection.execute("SELECT COUNT(*) FROM pending_decision").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                storage.close()
