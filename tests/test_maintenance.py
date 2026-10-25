"""Admin-only maintenance, fresh evidence, isolated progress, and no corrective writes."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from curatarr.adapters import AdapterResult
from curatarr.domain import ArrRuntimeState as Runtime, DurableState as State, MediaIdentity
from curatarr.services.maintenance import MaintenanceService
from curatarr.services.reconciliation import MovieReconciliationService
from curatarr.storage import Storage, StorageError
from curatarr.web.app import create_app
import test_miniapp as miniapp_fixtures
from test_reconciliation import Snapshot, movie


class HistorySnapshot(Snapshot):
    lookups = 0

    def get_item(self, identity):
        self.lookups += 1
        if self.error:
            from curatarr.adapters import AdapterError
            raise AdapterError("PRIVATE")
        return AdapterResult(next((item for item in self.items if identity in item.identities), None))


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "curatarr.sqlite"
        self.storage = Storage(self.path)
        self.addCleanup(self.storage.close)
        self.storage.initialize()
        self.ryot = HistorySnapshot([movie("ryot", str(i), title=f"Movie {i}", states=(State.LIBRARY, State.RADARR))
                                     for i in (1, 2, 3)])
        self.radarr = Snapshot([movie("radarr", str(i), title=f"Movie {i}", runtime=Runtime.ACTIVE_MISSING_UNMONITORED)
                                for i in (1, 2, 3)])
        self.reconciliation = MovieReconciliationService(self.ryot, self.radarr, self.storage)
        self.reconciliation.scan()
        self.service = MaintenanceService(self.reconciliation)

    def test_summary_does_not_return_unvalidated_movie_details(self):
        data = self.service.summary(1)
        self.assertEqual(data["movie_count"], 3)
        self.assertIsNone(data["movie"])
        self.assertFalse(data["validated"])
        self.assertFalse(data["actions_enabled"])

    def test_review_rechecks_providers_and_resumes_across_restart(self):
        first = self.service.review(1)
        second = self.service.review(1, "next", expected_cursor=first["movie"]["key"])
        self.assertEqual(second["movie"]["title"], "Movie 2")
        self.assertTrue(second["validated"])
        self.assertEqual(self.ryot.lookups, 2)
        reopened = Storage(self.path)
        self.addCleanup(reopened.close)
        service = MaintenanceService(MovieReconciliationService(self.ryot, self.radarr, reopened))
        self.assertEqual(service.review(1)["movie"]["key"], second["movie"]["key"])

    def test_skip_parks_movie_without_resolving_shared_findings(self):
        first = self.service.review(1)
        data = self.service.review(1, "skip", expected_cursor=first["movie"]["key"])
        self.assertEqual(data["movie_count"], 3)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["total"], 2)
        skipped = self.service.review(1, "skipped")
        self.assertEqual(skipped["movie"]["key"], first["movie"]["key"])
        self.assertEqual(skipped["total"], 1)
        self.assertEqual(self.service.summary(2)["skipped_count"], 0)

    def test_dropdown_skipped_is_exclusive_and_other_categories_show_pending_only(self):
        first = self.service.review(1)
        self.service.review(1, "skip", expected_cursor=first["movie"]["key"])
        summary = self.service.summary(1)
        count = next(c["count"] for c in summary["categories"] if c["key"] == "missing_unmonitored")
        self.assertEqual(count, 2)
        skipped = self.service.review(1, "filter", category="skipped")
        self.assertEqual(skipped["movie"]["key"], first["movie"]["key"])
        self.assertTrue(skipped["progress"]["parked"])
        self.assertEqual(skipped["total"], 1)
        legacy = self.storage.maintenance_progress(1)
        legacy["category"] = "library_missing"
        self.storage.save_maintenance_progress(1, legacy)
        restored = self.service.review(1)
        self.assertEqual(restored["total"], 1)
        self.assertEqual(restored["progress"]["category"], "all")
        pending = self.service.review(1, "filter", category="missing_unmonitored")
        self.assertFalse(pending["progress"]["parked"])
        self.assertEqual(pending["total"], 2)
        self.assertNotEqual(pending["movie"]["key"], first["movie"]["key"])

    def test_previous_and_next_wrap_and_keep_position(self):
        first = self.service.review(1)
        previous = self.service.review(1, "previous", expected_cursor=first["movie"]["key"])
        self.assertEqual(previous["position"], 3)
        self.assertEqual(self.service.review(1, "next", expected_cursor=previous["movie"]["key"])["position"], 1)

    def test_skipping_middle_movie_advances_instead_of_returning_to_first(self):
        first = self.service.review(1)
        second = self.service.review(1, "next", expected_cursor=first["movie"]["key"])
        third = self.service.review(1, "skip", expected_cursor=second["movie"]["key"])
        self.assertEqual(third["movie"]["title"], "Movie 3")
        self.service.review(1, "skip", expected_cursor=third["movie"]["key"])
        parked = self.service.review(1, "skipped")
        next_parked = self.service.review(1, "skip", expected_cursor=parked["movie"]["key"])
        self.assertNotEqual(next_parked["movie"]["key"], parked["movie"]["key"])

    def test_stale_navigation_and_unknown_categories_are_rejected(self):
        self.service.review(1)
        with self.assertRaises(ValueError):
            self.service.review(1, "skip", expected_cursor="radarr:999")
        with self.assertRaises(ValueError):
            self.service.review(1, "filter", category="delete")

    def test_resolved_finding_is_closed_and_not_presented_as_current(self):
        self.radarr.items[0] = replace(self.radarr.items[0], arr_runtime_state=Runtime.ACTIVE_MISSING_MONITORED)
        data = self.service.review(1)
        self.assertEqual(data["movie"]["title"], "Movie 2")
        self.assertEqual(data["movie_count"], 2)
        self.assertEqual(data["total"], 2)

    def test_new_archived_intent_replaces_monitoring_suggestion(self):
        self.ryot.items[0] = replace(self.ryot.items[0], durable_states=frozenset({State.LIBRARY, State.ARCHIVED}))
        data = self.service.review(1)
        self.assertEqual(data["movie"]["findings"][0]["kind"], "archived_active")
        self.assertNotIn("Enable monitoring", json.dumps(data["movie"]))

    def test_multiple_findings_are_grouped_by_movie(self):
        self.radarr.items[0] = replace(self.radarr.items[0], arr_runtime_state=Runtime.ACTIVE_DOWNLOADED_MONITORED)
        self.ryot.items[0] = replace(self.ryot.items[0], durable_states=frozenset({State.LIBRARY}))
        data = self.service.review(1)
        self.assertEqual(data["movie_count"], 3)
        self.assertEqual(len(data["movie"]["findings"]), 2)
        filtered = self.service.review(1, "filter", category="previously_acquired_missing")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["movie"]["title"], "Movie 1")

    def test_provider_failures_hide_stale_detail_and_preserve_findings(self):
        for provider in (self.ryot, self.radarr):
            provider.error = True
            data = self.service.review(1)
            self.assertFalse(data["validated"])
            self.assertIsNone(data["movie"])
            self.assertEqual(data["movie_count"], 3)
            self.assertNotIn("PRIVATE", json.dumps(data))
            provider.error = False

    def test_failed_full_refresh_does_not_show_previous_movie(self):
        self.service.review(1)
        self.ryot.error = True
        data = self.service.review(1, "refresh")
        self.assertFalse(data["validated"])
        self.assertIsNone(data["movie"])
        self.assertEqual(data["movie_count"], 3)

    def test_identity_findings_require_full_snapshot_revalidation(self):
        self.ryot.items[0] = replace(self.ryot.items[0], identities={MediaIdentity("ryot", "1"), MediaIdentity("tmdb", "999")})
        self.reconciliation.scan()
        calls = self.ryot.calls
        data = self.service.review(1)
        self.assertGreater(self.ryot.calls, calls)
        self.assertEqual(data["movie"]["findings"][0]["kind"], "identity_review")

    def test_missing_match_is_not_revalidated_from_cached_absence(self):
        self.ryot.items = []
        self.reconciliation.scan()
        calls = self.ryot.calls
        data = self.service.review(1)
        self.assertGreater(self.ryot.calls, calls)
        self.assertEqual(data["movie"]["findings"][0]["kind"], "ryot_match_missing")

    def test_identity_review_displays_radarr_reference_and_distinct_ryot_candidate(self):
        self.ryot.items[0] = replace(self.ryot.items[0], year=2004,
                                   identities={MediaIdentity("ryot", "1"), MediaIdentity("tmdb", "999")})
        self.reconciliation.scan()
        finding = self.service.review(1)["movie"]["findings"][0]
        self.assertEqual(finding["conclusion"], "Same title, different catalogue record")
        self.assertIn("no matching external identifier", finding["detail"])
        self.assertIn("acquisition reference", finding["correction"])
        self.assertEqual(finding["identity_comparison"][0]["provider"], "Radarr · acquisition reference")
        self.assertEqual([r["year"] for r in finding["identity_comparison"]], [2026, 2004])
        self.assertFalse(finding["correctable"])

    def test_empty_queues_and_all_resolved_findings(self):
        for index, item in enumerate(self.radarr.items):
            self.radarr.items[index] = replace(item, arr_runtime_state=Runtime.ACTIVE_MISSING_MONITORED)
        data = self.service.review(1)
        self.assertIsNone(data["movie"])
        self.assertEqual(data["movie_count"], 0)
        self.assertIsNone(self.service.review(1, "skipped")["movie"])

    def test_concurrent_scan_change_rejects_partial_update(self):
        findings = self.storage.reconciliation_report()["findings"]
        self.radarr.items[0] = replace(self.radarr.items[0], title="Changed")
        self.reconciliation.scan()
        with self.assertRaises(StorageError):
            self.storage.refresh_movie_findings("radarr:1", [f for f in findings if f["media_key"] == "radarr:1"], [])


class MaintenanceApiTests(MaintenanceTests):
    def setUp(self):
        super().setUp()
        fixture = miniapp_fixtures.MiniAppTests()
        fixture.setUp()
        self.config = replace(fixture.config, maintenance=replace(fixture.config.maintenance, show_diagnostics=True))
        service = replace(fixture.service, ryot_adapter=self.ryot, radarr_adapter=self.radarr)
        self.client = TestClient(create_app(self.config, database=self.path, service=service))
        self.headers = {"Authorization": "tma " + miniapp_fixtures.signed_data()}

    def test_unauthenticated_and_non_admin_requests_cannot_access_or_mutate_progress(self):
        for method, path in (("get", "/api/maintenance"), ("post", "/api/maintenance/review")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 401)
            self.assertEqual(getattr(self.client, method)(path, headers={"Authorization": "tma " + miniapp_fixtures.signed_data(2)}).status_code, 403)
        self.assertEqual(self.storage.maintenance_progress(2)["skipped"], [])

    def test_admin_api_rechecks_and_stores_progress(self):
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertTrue(me["is_admin"])
        self.assertTrue(me["maintenance_available"])
        self.assertEqual(self.client.get("/api/maintenance", headers=self.headers).json()["movie_count"], 3)
        result = self.client.post("/api/maintenance/review", headers=self.headers, json={"action": "open"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertTrue(result.json()["validated"])
        result = self.client.post("/api/maintenance/review", headers=self.headers,
                                  json={"action": "skip", "expected_cursor": result.json()["movie"]["key"]})
        self.assertEqual(result.json()["skipped_count"], 1)

    def test_correction_actions_and_forged_navigation_payloads_are_not_accepted(self):
        for payload in ({"action": "archive"}, {"action": "open", "apply": True},
                        {"action": "enable_monitoring"}):
            self.assertEqual(self.client.post("/api/maintenance/review", headers=self.headers, json=payload).status_code, 422)
        self.assertEqual(self.client.post("/api/maintenance/review", headers=self.headers,
                                          json={"action": "filter", "category": "bad"}).status_code, 409)

    def test_storage_failures_are_sanitized(self):
        with patch("curatarr.services.maintenance.MaintenanceService.review", side_effect=StorageError("PRIVATE")):
            result = self.client.post("/api/maintenance/review", headers=self.headers, json={"action": "open"})
        self.assertEqual(result.status_code, 409)
        self.assertNotIn("PRIVATE", result.text)

    def test_single_correction_endpoint_requires_admin_explicit_confirmation_and_no_batch(self):
        path = "/api/maintenance/correct"
        payload = {"media_key": "radarr:1", "finding_id": 1, "confirm": True}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.assertEqual(self.client.post(path, headers={"Authorization": "tma " + miniapp_fixtures.signed_data(2)}, json=payload).status_code, 403)
        for invalid in ({"media_key": "radarr:1", "finding_id": 1}, {**payload, "all": True},
                        {**payload, "finding_id": "1"}, {**payload, "confirm": False}):
            self.assertEqual(self.client.post(path, headers=self.headers, json=invalid).status_code, 422)
        with patch("curatarr.services.maintenance.MaintenanceService.correct", return_value={"notice": "verified"}) as correct:
            result = self.client.post(path, headers=self.headers, json=payload)
            self.assertEqual(result.status_code, 200)
            correct.assert_called_once_with(1, media_key="radarr:1", finding_id=1)

    def test_viewing_branch_is_admin_only_separate_and_has_no_provider_mutation_action(self):
        path = "/api/maintenance/viewing/review"
        self.assertEqual(self.client.post(path, json={}).status_code, 401)
        self.assertEqual(self.client.post(path, headers={"Authorization": "tma " + miniapp_fixtures.signed_data(2)}, json={}).status_code, 403)
        data = self.client.post(path, headers=self.headers, json={"action": "open"}).json()
        self.assertEqual(data["movie_count"], 0)
        self.assertFalse(data["updates_enabled"])
        self.assertEqual(self.client.post(path, headers=self.headers, json={"action": "archive"}).status_code, 422)
        for action in ("skip", "skipped", "pending"):
            self.assertEqual(self.client.post(path, headers=self.headers, json={"action": action}).status_code, 422)

    def test_maintenance_requests_have_an_independent_rate_limit(self):
        for _ in range(30):
            self.assertEqual(self.client.post("/api/maintenance/review", headers=self.headers, json={}).status_code, 200)
        result = self.client.post("/api/maintenance/review", headers=self.headers, json={})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.headers["retry-after"], "60")
        self.assertEqual(self.client.get("/api/maintenance", headers=self.headers).status_code, 200)

    def test_poster_lookup_is_admin_only_optional_and_independent_of_facts(self):
        path = "/api/maintenance/poster/1"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={"Authorization": "tma " + miniapp_fixtures.signed_data(2)}).status_code, 403)
        self.assertEqual(self.client.get(path, headers=self.headers).json(), {"poster_url": None})
        with patch("test_telegram_service._FakeMetadataAdapter.movie_poster", create=True,
                   return_value="https://image.tmdb.org/t/p/w500/poster.jpg") as resolve:
            self.assertEqual(self.client.get(path, headers=self.headers).json()["poster_url"],
                             "https://image.tmdb.org/t/p/w500/poster.jpg")
            resolve.assert_called_once_with("1")
        self.assertEqual(self.client.get("/api/maintenance/poster/bad", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get("/api/maintenance/poster/9999999999999999", headers=self.headers).status_code, 400)
        from curatarr.adapters import AdapterError
        with patch("test_telegram_service._FakeMetadataAdapter.movie_poster", create=True,
                   side_effect=AdapterError("PRIVATE")):
            self.assertEqual(self.client.get(path, headers=self.headers).json(), {"poster_url": None})
        data = self.client.post("/api/maintenance/review", headers=self.headers, json={"action": "open"}).json()
        self.assertTrue(data["validated"])


if __name__ == "__main__":
    unittest.main()
