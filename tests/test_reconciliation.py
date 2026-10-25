"""Complete snapshots, persistent deduplicated findings, and read-only boundaries."""

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from curatarr.adapters import AdapterError, AdapterResult
from curatarr.cli import main
from curatarr.config import parse_config
from curatarr.domain import ArrRuntimeState as Runtime, DurableState as State, MediaIdentity, MediaItem, MediaType
from curatarr.services.reconciliation import MovieReconciliationService, movie_findings
from curatarr.storage import Storage
from test_config import VALID_CONFIG


def movie(provider, number="1", *, title="Movie", states=(), runtime=Runtime.NOT_ACTIVE, external=None):
    identities = {MediaIdentity(provider, number)}
    identities.update({MediaIdentity("tmdb", number)} if external is None else external)
    return MediaItem(MediaType.MOVIE, title, 2026, frozenset(identities), frozenset(states), runtime)


class Snapshot:
    def __init__(self, items):
        self.items = items
        self.error = False
        self.calls = 0

    def scan_movies(self):
        self.calls += 1
        if self.error:
            raise AdapterError("SECRET https://private.example/token /private/path")
        return AdapterResult(tuple(self.items))


class MovieFindingsTests(unittest.TestCase):
    def setUp(self):
        self.history = movie("ryot", states=(State.LIBRARY, State.RADARR, State.PREVIOUSLY_ACQUIRED))
        self.active = movie("radarr", runtime=Runtime.ACTIVE_DOWNLOADED_UNMONITORED)

    def test_healthy_movie_has_no_findings(self):
        self.assertEqual(movie_findings((self.history,), (self.active,)), [])

    def test_missing_unmonitored_explains_policy_and_offers_no_enabled_actions(self):
        item = replace(self.active, arr_runtime_state=Runtime.ACTIVE_MISSING_UNMONITORED)
        finding, = movie_findings((self.history,), (item,))
        self.assertEqual(finding["kind"], "missing_unmonitored")
        self.assertIn("Radarr itself permits", finding["context"]["explanation"])
        self.assertIn("Enable monitoring", finding["context"]["suggested_actions"][0])
        self.assertFalse(finding["context"]["actions_enabled"])

    def test_archived_intent_suppresses_acquisition_and_collection_corrections(self):
        history = replace(self.history, durable_states=frozenset({State.ARCHIVED}))
        item = replace(self.active, arr_runtime_state=Runtime.ACTIVE_MISSING_UNMONITORED)
        finding, = movie_findings((history,), (item,))
        self.assertEqual(finding["kind"], "archived_active")
        self.assertIn("deletion", finding["context"]["suggested_actions"][0])

    def test_native_only_record_is_not_misreported_as_absent(self):
        history = replace(self.history, durable_states=frozenset(), collection_names=("Owned",))
        kinds = {finding["kind"] for finding in movie_findings((history,), (self.active,))}
        self.assertEqual(kinds, {"library_missing", "radarr_membership_missing", "previously_acquired_missing"})

    def test_no_file_does_not_prove_previous_acquisition_or_deletion(self):
        history = replace(self.history, durable_states=frozenset({State.LIBRARY, State.RADARR}))
        active = replace(self.active, arr_runtime_state=Runtime.ACTIVE_MISSING_MONITORED)
        self.assertEqual(movie_findings((history,), (active,)), [])

    def test_watched_or_archived_without_active_intent_is_not_a_missing_file_issue(self):
        for states in ((State.LIBRARY, State.WATCHED), (State.LIBRARY, State.ARCHIVED, State.DELETED)):
            self.assertEqual(movie_findings((replace(self.history, durable_states=frozenset(states)),), ()), [])

    def test_active_overlay_without_arr_record_needs_intent_review(self):
        finding, = movie_findings((self.history,), ())
        self.assertEqual(finding["kind"], "radarr_record_missing")
        self.assertIn("Do not infer archival or deletion", finding["context"]["explanation"])

    def test_missing_exact_ryot_match_defers_monitoring_suggestion(self):
        finding, = movie_findings((), (replace(self.active, arr_runtime_state=Runtime.ACTIVE_MISSING_UNMONITORED),))
        self.assertEqual(finding["kind"], "ryot_match_missing")
        self.assertIn("not proof", finding["context"]["explanation"])

    def test_title_only_and_conflicting_ids_require_review(self):
        for history in (replace(self.history, identities=frozenset({MediaIdentity("ryot", "1"), MediaIdentity("tmdb", "999")})),
                        replace(self.history, identities=self.history.identities | {MediaIdentity("imdb", "tt999")})):
            active = replace(self.active, identities=self.active.identities | {MediaIdentity("imdb", "tt1")})
            finding, = movie_findings((history,), (active,))
            self.assertEqual(finding["kind"], "identity_review")

    def test_duplicate_external_matches_require_review_not_arbitrary_selection(self):
        duplicate = replace(self.history, identities={MediaIdentity("ryot", "2"), MediaIdentity("tmdb", "1")})
        finding, = movie_findings((self.history, duplicate), (self.active,))
        self.assertEqual(finding["kind"], "identity_review")
        self.assertEqual(len(finding["context"]["candidate_keys"]), 2)

    def test_title_only_review_keeps_distinct_records_and_specific_evidence(self):
        history = replace(self.history, title="The Punisher", year=2004,
                          identities={MediaIdentity("ryot", "old"), MediaIdentity("tmdb", "7220")})
        active = replace(self.active, title="The Punisher", year=2017,
                         identities={MediaIdentity("radarr", "3502"), MediaIdentity("tmdb", "1692911"), MediaIdentity("imdb", "tt5675620")})
        finding, = movie_findings((history,), (active,))
        self.assertEqual(finding["context"]["identity_reasons"], ["title_only"])
        self.assertEqual(finding["context"]["identity_candidates"], [
            {"title": "The Punisher", "year": 2004, "identities": ["ryot:old", "tmdb:7220"]}])
        self.assertIn("tmdb:1692911", finding["context"]["identities"])
        self.assertFalse(finding["context"]["actions_enabled"])
        duplicate_arr = replace(self.active, identities={MediaIdentity("radarr", "2"), MediaIdentity("tmdb", "1")})
        self.assertEqual([f["kind"] for f in movie_findings((self.history,), (self.active, duplicate_arr))],
                         ["identity_review", "identity_review"])

    def test_identifier_namespaces_do_not_collide(self):
        history = replace(self.history, title="Different", identities={MediaIdentity("ryot", "1"), MediaIdentity("imdb", "1")})
        self.assertEqual({f["kind"] for f in movie_findings((history,), (self.active,))},
                         {"radarr_record_missing", "ryot_match_missing"})


class ReconciliationPersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "curatarr.sqlite"
        self.storage = Storage(self.path)
        self.addCleanup(self.storage.close)
        self.storage.initialize()
        self.ryot = Snapshot([movie("ryot", states=(State.LIBRARY, State.RADARR))])
        self.radarr = Snapshot([movie("radarr", runtime=Runtime.ACTIVE_MISSING_UNMONITORED)])
        self.service = MovieReconciliationService(self.ryot, self.radarr, self.storage)

    def test_repeated_scans_reuse_open_decision_and_survive_restart(self):
        first = self.service.scan()
        second = self.service.scan()
        self.assertEqual(first["findings"], second["findings"])
        self.assertEqual(second["movies_requiring_attention"], 1)
        self.assertTrue(second["revalidated"])
        reopened = Storage(self.path)
        self.addCleanup(reopened.close)
        reopened.initialize()
        self.assertEqual(reopened.reconciliation_report(), second)
        self.assertEqual(self.ryot.calls, 2)

    def test_changed_evidence_supersedes_and_fixed_findings_resolve(self):
        first = self.service.scan()["findings"][0]
        self.radarr.items[0] = replace(self.radarr.items[0], title="Updated title")
        second = self.service.scan()["findings"][0]
        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(self.storage.get_pending_decision(first["decision_id"]).status, "superseded")
        self.radarr.items[0] = replace(self.radarr.items[0], arr_runtime_state=Runtime.ACTIVE_MISSING_MONITORED)
        report = self.service.scan()
        self.assertEqual(report["finding_count"], 0)
        self.assertEqual(self.storage.get_pending_decision(second["decision_id"]).status, "resolved")

    def test_provider_failure_preserves_findings_and_deduplicates_technical_issue(self):
        complete = self.service.scan()
        first = complete["findings"]
        self.radarr.error = True
        for _ in range(2):
            report = self.service.scan()
            self.assertFalse(report["revalidated"])
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["findings"], first)
            self.assertEqual(len(report["technical_issues"]), 1)
            self.assertEqual(report["last_validated_run_id"], complete["run_id"])
            self.assertEqual(report["last_validated_at"], complete["observed_at"])
            self.assertNotIn("SECRET", json.dumps(report))
            self.assertNotIn("private.example", json.dumps(report))
        self.radarr.error = False
        self.radarr.items[0] = replace(self.radarr.items[0], arr_runtime_state=Runtime.ACTIVE_MISSING_MONITORED)
        report = self.service.scan()
        self.assertEqual(report["technical_issues"], [])
        self.assertEqual(report["findings"], [])

    def test_failed_ryot_scan_does_not_create_mass_missing_memberships(self):
        self.ryot.error = True
        report = self.service.scan()
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["technical_issues"][0]["provider"], "ryot")

    def test_other_user_decisions_are_untouched(self):
        other = self.storage.create_pending_decision(media_key="tmdb:1", decision_type="reactivate",
            audience="telegram:user:1", state_fingerprint="old")
        self.service.scan()
        self.radarr.items = []
        self.service.scan()
        self.assertEqual(self.storage.get_pending_decision(other.decision_id).status, "open")

    def test_older_concurrent_scan_cannot_overwrite_newer_findings(self):
        old = self.storage.start_reconciliation()
        newer = self.service.scan()
        report = self.storage.publish_reconciliation(old, [], [], {})
        self.assertEqual(report, newer)
        self.assertEqual(self.storage.connection.execute("SELECT status FROM job_run WHERE id = ?", (old,)).fetchone()[0], "superseded")

    def test_new_in_progress_scan_marks_stored_findings_unvalidated(self):
        self.service.scan()
        self.storage.start_reconciliation()
        report = self.storage.reconciliation_report()
        self.assertEqual(report["status"], "running")
        self.assertFalse(report["revalidated"])

    def test_duplicate_provider_keys_fail_without_clearing_previous_findings(self):
        first = self.service.scan()["findings"]
        self.radarr.items *= 2
        report = self.service.scan()
        self.assertEqual(report["findings"], first)
        self.assertEqual(report["status"], "failed")

    def test_legacy_database_and_audit_records_survive_initialization(self):
        self.storage.connection.execute("DROP INDEX reconciliation_open_finding")
        entry = self.storage.append_audit_log(actor="admin", action="legacy", details={"keep": True})
        self.storage.initialize()
        self.assertEqual(self.storage.connection.execute("SELECT action FROM audit_log WHERE id = ?", (entry.entry_id,)).fetchone()[0], "legacy")
        self.assertEqual(self.service.scan()["status"], "completed")

    def test_cli_runs_fresh_scan_without_bot_or_tmdb_requirement(self):
        config = parse_config(yaml.safe_load(VALID_CONFIG))
        output = io.StringIO()
        with patch("curatarr.cli.load_config", return_value=config), \
                patch("curatarr.runtime.build_movie_reconciliation", return_value=self.service), \
                patch("curatarr.runtime.build_telegram_bot") as bot, redirect_stdout(output):
            status = main(["--config", str(self.path.parent / "config.yaml"), "--database", str(self.path), "--reconcile-movies"])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["read_only"])
        bot.assert_not_called()

    def test_cli_provider_failure_returns_nonzero_and_sanitized_json(self):
        self.ryot.error = True
        output = io.StringIO()
        with patch("curatarr.cli.load_config", return_value=parse_config(yaml.safe_load(VALID_CONFIG))), \
                patch("curatarr.runtime.build_movie_reconciliation", return_value=self.service), redirect_stdout(output):
            status = main(["--config", str(self.path.parent / "config.yaml"), "--database", str(self.path), "--reconcile-movies"])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["revalidated"])

    def test_cli_storage_errors_are_sanitized(self):
        output = io.StringIO()
        with patch("curatarr.cli.load_config", return_value=parse_config(yaml.safe_load(VALID_CONFIG))), \
                patch("curatarr.storage.Storage", side_effect=OSError("SECRET")), redirect_stderr(output):
            self.assertEqual(main(["--config", "config.yaml", "--database", str(self.path), "--reconcile-movies"]), 1)
        self.assertNotIn("SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
