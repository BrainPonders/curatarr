"""Native Owned removal stays movie-only, opt-in, durable and independently verified."""

from copy import deepcopy
import json
import unittest

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.adapter import RyotReadAdapter
from curatarr.integrations.ryot.client import RyotClient, RyotError
from curatarr.integrations.ryot.owned import RyotOwnedCleanup
from curatarr.services.owned_cleanup import OwnedCleanupAutomation
from curatarr.services.settings import InstallationSettings
from curatarr.storage import Storage


class NativeOwned:
    def __init__(self):
        self.owned = {"movie", "series"}
        self.calls = []
        self.jobs = []
        self.duplicate = False
        self.fail_scan = False
        self.async_job = False
        self.lost_response = False
        self.ambiguous = False
        self.count_mismatch = False
        self.lots = {"movie": "MOVIE", "movie2": "MOVIE", "series": "SHOW"}
        self.account = "account"
        self.other_memberships = {"movie": ["Library", "Completed"], "series": ["Library"]}
        self.history, self.reviews = ["watch-1"], ["review-1"]

    def transport(self, query, variables):
        self.calls.append((query, deepcopy(variables)))
        if "CuratarrOwnedTarget" in query:
            collection = {"id": "owned", "name": "Owned", "isDefault": True, "creator": {"id": self.account}}
            return {"data": {"userDetails": {"id": self.account},
                             "userCollectionsList": {"response": [collection] * (2 if self.ambiguous else 1)}}}
        if "CuratarrOwnedContents" in query:
            if self.fail_scan:
                raise RyotError("PRIVATE SECRET")
            items = [{"entityId": key, "entityLot": "METADATA"} for key in sorted(self.owned)]
            if self.duplicate:
                items.append(items[0])
            return {"data": {"collectionContents": {"response": {"results": {"items": items,
                "details": {"totalItems": len(items) + int(self.count_mismatch), "nextPage": None}}}}}}
        if "CuratarrOwnedMovies" in query:
            result = {}
            for key, value in variables.items():
                i = key[2:]
                result["m" + i] = {"response": {"id": value, "title": value, "lot": self.lots[value]}}
                result["u" + i] = {"response": {"collections": [{"details": {
                    "collectionId": "owned", "collectionName": "Owned", "information": {"Owned on": "2020-01-01"}}}]}}
            return {"data": result}
        if "CuratarrRemoveMovieOwned" in query:
            payload = variables["input"]
            assert payload["collectionName"] == "Owned"
            assert payload["creatorUserId"] == self.account
            assert all(e["entityLot"] == "METADATA" and self.lots[e["entityId"]] == "MOVIE" for e in payload["entities"])
            self.jobs.append(payload)
            if not self.async_job:
                self.owned.difference_update(e["entityId"] for e in payload["entities"])
            if self.lost_response:
                raise RyotError("Response lost after submission")
            return {"data": {"deployRemoveEntitiesFromCollectionJob": True}}
        raise AssertionError(query)


class OwnedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.storage.initialize()
        self.addCleanup(self.storage.close)
        self.native = NativeOwned()
        self.adapter = RyotOwnedCleanup(RyotReadAdapter(RyotClient("https://ryot.invalid", "secret", self.native.transport)))
        self.settings = InstallationSettings(self.storage)
        self.now = 1000000
        self.worker = OwnedCleanupAutomation(self.storage, self.adapter, clock=lambda: self.now)

    def enable(self, value=True):
        self.settings.save(expected_revision=self.settings.get()["revision"], actor="admin", owned_cleanup_enabled=value)

    def last(self):
        return self.settings.public()["owned_cleanup_last_run"]

    def test_off_performs_no_scan_or_mutation(self):
        self.worker.tick()
        self.assertEqual(self.native.calls, [])
        self.assertIsNone(self.last())

    def test_first_run_needs_only_on_switch_and_preserves_series_history_and_other_collections(self):
        original = deepcopy((self.native.other_memberships, self.native.history, self.native.reviews))
        self.enable()
        self.worker.tick()
        self.assertEqual(self.native.owned, {"series"})
        self.assertEqual((self.native.other_memberships, self.native.history, self.native.reviews), original)
        self.assertEqual(self.last()["status"], "completed")
        self.assertEqual(self.last()["removed"], 1)
        audit = self.storage.connection.execute("SELECT details_json FROM audit_log WHERE action='owned_cleanup_requested'").fetchone()
        self.assertEqual(json.loads(audit[0])["snapshot"]["movies"][0]["membership"]["information"], {"Owned on": "2020-01-01"})

    def test_runs_daily_after_restart_and_removes_readded_owned(self):
        self.enable()
        self.worker.tick()
        self.native.owned.add("movie")
        restarted = OwnedCleanupAutomation(self.storage, self.adapter, clock=lambda: self.now)
        self.now += 86399
        restarted.tick()
        self.assertEqual(len(self.native.jobs), 1)
        self.now += 1
        restarted.tick()
        self.assertEqual(len(self.native.jobs), 2)

    def test_incomplete_duplicate_or_ambiguous_scan_submits_nothing(self):
        self.enable()
        for problem in ("fail_scan", "duplicate", "count_mismatch", "ambiguous"):
            with self.subTest(problem=problem):
                setattr(self.native, problem, True)
                self.worker.tick()
                self.assertEqual(self.last()["status"], "failed")
                self.assertEqual(self.native.jobs, [])
                setattr(self.native, problem, False)
                self.now += 86400

    def test_accepted_async_job_is_not_reported_complete_or_resubmitted(self):
        self.enable()
        self.native.async_job = True
        self.worker.tick()
        self.assertEqual(self.last()["status"], "pending")
        for _ in range(3):
            self.now += 301
            self.worker.tick()
        self.assertEqual(len(self.native.jobs), 1)
        self.native.owned.discard("movie")
        self.now += 301
        self.worker.tick()
        self.assertEqual(self.last()["status"], "completed")

    def test_lost_submit_response_reconciles_after_restart_without_another_job(self):
        self.enable()
        self.native.lost_response = True
        self.worker.tick()
        self.assertEqual(self.last()["status"], "pending")
        self.now += 301
        OwnedCleanupAutomation(self.storage, self.adapter, clock=lambda: self.now).tick()
        self.assertEqual(self.last()["status"], "completed")
        self.assertEqual(len(self.native.jobs), 1)

    def test_off_during_scan_prevents_submission(self):
        self.enable()
        original = self.adapter.snapshot
        def scan():
            result = original()
            self.enable(False)
            return result
        self.adapter.snapshot = scan
        self.worker.tick()
        self.assertEqual(self.native.jobs, [])
        self.assertEqual(self.last()["status"], "stopped")

    def test_pending_job_off_stops_future_work_and_keeps_record(self):
        self.enable()
        self.native.async_job = True
        self.worker.tick()
        self.enable(False)
        count = len(self.native.calls)
        self.now += 86400
        self.worker.tick()
        self.assertEqual(len(self.native.calls), count)
        self.assertEqual(self.last()["status"], "pending")

    def test_fresh_verification_failure_retains_pending_without_second_submit(self):
        self.enable()
        original = self.adapter.remove
        def remove(snapshot):
            original(snapshot)
            self.native.fail_scan = True
        self.adapter.remove = remove
        self.worker.tick()
        self.assertEqual(self.last()["status"], "pending")
        self.native.fail_scan = False
        self.now += 301
        self.worker.tick()
        self.assertEqual(self.last()["status"], "completed")
        self.assertEqual(len(self.native.jobs), 1)

    def test_non_movie_submission_is_rejected(self):
        snapshot = self.adapter.snapshot()
        snapshot["movies"][0]["lot"] = "SHOW"
        with self.assertRaises(AdapterError):
            self.adapter.remove(snapshot)
        self.assertEqual(self.native.jobs, [])

    def test_partial_job_progress_reports_only_verified_removals(self):
        self.enable()
        self.native.owned.add("movie2")
        self.native.async_job = True
        self.worker.tick()
        self.native.owned.discard("movie")
        self.now += 301
        self.worker.tick()
        self.assertEqual(self.last()["status"], "pending")
        self.assertEqual((self.last()["removed"], self.last()["total"]), (1, 2))
        self.native.owned.discard("movie2")
        self.now += 301
        self.worker.tick()
        self.assertEqual((self.last()["status"], self.last()["removed"]), ("completed", 2))
        self.assertEqual(len(self.native.jobs), 1)

    def test_newer_claim_prevents_stale_scan_submitting_a_second_job(self):
        self.enable()
        original = self.adapter.snapshot
        overlap = [True]
        def scan():
            snapshot = original()
            if overlap[0]:
                overlap[0] = False
                self.now += 301
                OwnedCleanupAutomation(self.storage, self.adapter, clock=lambda: self.now).tick()
            return snapshot
        self.adapter.snapshot = scan
        self.worker.tick()
        self.assertEqual(len(self.native.jobs), 1)
        self.assertEqual(self.last()["status"], "completed")

    def test_changed_account_cannot_inherit_enabled_policy_without_reenable(self):
        self.enable()
        self.worker.tick()
        self.native.account = "different-account"
        self.native.owned.add("movie")
        self.now += 86400
        self.worker.tick()
        self.assertEqual(self.last()["status"], "failed")
        self.assertEqual(len(self.native.jobs), 1)
        self.enable(False)
        self.enable(True)
        self.worker.tick()
        self.assertEqual(len(self.native.jobs), 2)
