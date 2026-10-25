"""Explicit viewing corrections, native flag cleanup and interrupted-write safety."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from curatarr.integrations.ryot.viewing_reset import RyotViewingReset
from curatarr.services.viewing_corrections import ViewingCorrections
from curatarr.services.viewing_workflow import ViewingWorkflow
from curatarr.storage import Storage, StorageError
import test_watch_checkin as fixtures


class ViewingCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.WatchCheckInTests()
        self.fx.setUp()
        self.fx.enable_diagnostics()
        self.addCleanup(self.fx.doCleanups)
        self.native = self.fx.fixture.native
        self.storage = self.fx.fixture.storage
        self.assertEqual(self.fx.answer("no").status_code, 200)
        self.assertEqual(self.fx.answer("no", headers=self.fx.admin).status_code, 200)

    def item(self):
        response = self.fx.client.get("/api/maintenance/viewing/declined", headers=self.fx.admin)
        self.assertEqual(response.status_code, 200, response.text)
        movies = response.json()["movies"]
        if not movies:
            movies = self.fx.client.get("/api/maintenance/viewing/issues", headers=self.fx.admin).json()["movies"]
        return movies[0]

    def body(self, action):
        item = self.item()
        return {"decision_id": item["question"]["decision_id"], "media_key": item["media_key"],
                "expected_version": item["question"]["version"], "action": action}

    def preview(self):
        body = self.body("clear_history")
        response = self.fx.client.post("/api/maintenance/viewing/reset-preview", headers=self.fx.admin, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return body, response.json()

    def submit(self, body, preview, **extra):
        return self.fx.client.post("/api/maintenance/viewing/confirm-declined", headers=self.fx.admin,
                                   json={**body, "confirm": True, "preview_token": preview["token"], **extra})

    def deletes(self):
        return [v["id"] for q, v in self.native.calls if "mutation CuratarrClearViewing(" in q]

    def test_all_buttons_are_visible_with_precise_disabled_reasons(self):
        actions = self.item()["question"]["actions"]
        self.assertEqual([a["action"] for a in actions], ["confirm_declined", "review_again", "clear_history", "clear_stale"])
        self.assertEqual([a["enabled"] for a in actions], [True, True, True, False])
        self.assertIn("still exists", actions[-1]["reason"])
        self.native.events.pop()
        actions = self.item()["question"]["actions"]
        self.assertEqual([a["enabled"] for a in actions], [False, False, True, True])
        self.assertIn("no longer exists", actions[1]["reason"])

    def test_review_again_reuses_event_and_question_and_preserves_previous_votes_in_audit(self):
        decision = self.fx.fixture.pending()
        events = deepcopy(self.native.events)
        ledger = tuple(self.storage.connection.execute("SELECT source,event_id FROM viewing_event"))
        result = self.fx.client.post("/api/maintenance/viewing/confirm-declined", headers=self.fx.admin, json=self.body("review_again"))
        self.assertEqual(result.status_code, 200, result.text)
        current = self.fx.fixture.pending()
        self.assertEqual(current.decision_id, decision.decision_id)
        self.assertEqual(current.context["responses"], {})
        self.assertFalse(current.context["declined_all"])
        self.assertEqual(self.native.events, events)
        self.assertEqual(self.native.reviews, [])
        self.assertEqual(tuple(self.storage.connection.execute("SELECT source,event_id FROM viewing_event")), ledger)
        for headers in (self.fx.admin, self.fx.standard):
            self.assertEqual(self.fx.client.get("/api/viewing/summary", headers=headers).json()["pending_movie_count"], 1)
        row = self.storage.connection.execute("SELECT details_json FROM audit_log WHERE action='viewing_review_restarted'").fetchone()
        self.assertIn('"previous_responses": {"1":', row[0])

    def test_review_again_cannot_recreate_a_missing_native_event(self):
        self.native.events.pop()
        result = self.fx.client.post("/api/maintenance/viewing/confirm-declined", headers=self.fx.admin, json=self.body("review_again"))
        self.assertEqual(result.status_code, 409)
        self.assertTrue(self.fx.fixture.pending().context["declined_all"])
        self.assertEqual(len(self.native.events), 1)

    def test_clear_history_requires_admin_explicit_confirmation_and_current_preview(self):
        body, preview = self.preview()
        self.assertEqual(self.deletes(), [])
        self.assertEqual(preview["record_count"], 2)
        path = "/api/maintenance/viewing/confirm-declined"
        self.assertEqual(self.fx.client.post(path, headers=self.fx.standard, json={**body, "confirm": True, "preview_token": preview["token"]}).status_code, 403)
        self.assertEqual(self.fx.client.post(path, headers=self.fx.admin, json=body).status_code, 409)
        self.assertEqual(self.submit(body, preview, confirm=False).status_code, 409)
        self.assertEqual(self.submit(body, preview, preview_token="0" * 64).status_code, 409)
        self.native.events.append(self.native.event("new-after-preview"))
        self.assertEqual(self.submit(body, preview).status_code, 409)
        self.assertEqual(self.deletes(), [])

    def test_reset_removes_repeat_history_and_completed_flag_preserving_reviews_and_library(self):
        self.native.reviews.append({"id": "manual", "rating": "80", "textOriginal": "Keep this review", "visibility": "PRIVATE",
                                    "isSpoiler": False, "postedBy": {"id": "household"}})
        self.native.events[-1]["reviewId"] = "manual"
        reviews, library = deepcopy(self.native.reviews), deepcopy(self.native.reset_collections["Library"])
        body, preview = self.preview()
        result = self.submit(body, preview)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("cleared", result.json()["notice"])
        self.assertEqual(self.native.events, [])
        self.assertNotIn("Completed", self.native.reset_collections)
        self.assertEqual(self.native.reset_collections["Library"], library)
        self.assertEqual(self.native.reviews, reviews)
        self.assertEqual(self.storage.viewing_followups(), ())
        self.assertEqual(sorted(self.deletes()), ["new", "old"])
        self.assertEqual(self.native.reset_jobs[0]["collectionName"], "Completed")
        closed = self.storage.get_pending_decision(body["decision_id"])
        self.assertEqual(closed.context["closed_reason"], "history_cleared")
        self.assertIn("VIEWING HISTORY CLEARED", self.fx.fixture.workflow.prompt(closed).text)
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM viewing_event").fetchone()[0], 2)

    def test_native_intent_membership_blocks_unsafe_delete_side_effects(self):
        self.native.reset_collections["Watchlist"] = {"collectionId": "col-Watchlist", "collectionName": "Watchlist", "rank": 4, "information": {"note": "Keep intent"}}
        clear = self.item()["question"]["actions"][2]
        self.assertFalse(clear["enabled"])
        self.assertIn("Watchlist", clear["reason"])
        response = self.fx.client.post("/api/maintenance/viewing/reset-preview", headers=self.fx.admin, json=self.body("clear_history"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.deletes(), [])

    def test_flag_only_reset_keeps_watchlist_when_no_seen_deletion_is_needed(self):
        self.native.events = []
        member = {"collectionId": "col-Watchlist", "collectionName": "Watchlist", "rank": 4, "information": {"note": "Keep intent"}}
        self.native.reset_collections["Watchlist"] = deepcopy(member)
        body, preview = self.preview()
        self.assertEqual(preview["record_count"], 0)
        self.assertEqual(self.submit(body, preview).status_code, 200)
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.native.reset_collections["Watchlist"], member)
        self.assertNotIn("Completed", self.native.reset_collections)

    def test_flags_created_by_deleting_progress_record_are_removed_and_verified(self):
        event = self.native.event("zzz-progress")
        event.update(state="IN_PROGRESS", progress="50")
        self.native.events.append(event)
        body, preview = self.preview()
        self.assertEqual(self.submit(body, preview).status_code, 200)
        self.assertEqual(self.native.events, [])
        self.assertEqual(set(self.native.reset_collections), {"Library"})
        self.assertEqual({j["collectionName"] for j in self.native.reset_jobs}, {"Completed", "In Progress", "Monitoring"})

    def test_lost_delete_response_resumes_after_restart_without_redeleting_absent_ids(self):
        self.native.fail_after_delete = True
        body, preview = self.preview()
        result = self.submit(body, preview)
        self.assertIn("pending", result.json()["notice"])
        self.assertEqual(len(self.deletes()), 1)
        restarted = Storage(self.fx.path)
        try:
            workflow = ViewingWorkflow(restarted, self.fx.fixture.adapter, self.fx.fixture.client, participants=(1, 2))
            correction = ViewingCorrections(workflow)
            decision = restarted.viewing_followups()[0]
            preview = correction.preview(decision)
            self.assertTrue(preview["resume"])
            self.assertTrue(correction.clear(decision, token=preview["token"], user_id=1))
        finally:
            restarted.close()
        self.assertEqual(sorted(self.deletes()), ["new", "old"])
        self.assertEqual(self.native.events, [])

    def test_async_collection_job_is_not_resubmitted_after_acceptance_or_restart(self):
        self.native.defer_reset_flags = True
        body, preview = self.preview()
        self.assertIn("pending", self.submit(body, preview).json()["notice"])
        body, preview = self.preview()
        self.assertIn("pending", self.submit(body, preview).json()["notice"])
        self.assertEqual(len(self.native.reset_jobs), 1)
        self.native.reset_collections.pop("Completed")
        body, preview = self.preview()
        self.assertIn("cleared", self.submit(body, preview).json()["notice"])
        self.assertEqual(len(self.native.reset_jobs), 1)

    def test_lost_flag_response_verifies_existing_job_without_duplicate_submission(self):
        self.native.fail_flag_response = True
        body, preview = self.preview()
        self.assertIn("pending", self.submit(body, preview).json()["notice"])
        body, preview = self.preview()
        self.assertIn("cleared", self.submit(body, preview).json()["notice"])
        self.assertEqual(len(self.native.reset_jobs), 1)

    def test_external_new_history_stops_partial_reset_and_blocks_other_workflows(self):
        self.native.fail_after_delete = True
        body, preview = self.preview()
        self.assertIn("pending", self.submit(body, preview).json()["notice"])
        self.native.events.append(self.native.event("unapproved-new-viewing"))
        self.assertFalse(self.item()["question"]["actions"][2]["enabled"])
        response = self.fx.client.post("/api/maintenance/viewing/reset-preview", headers=self.fx.admin, json=self.body("clear_history"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.deletes()), 1)
        decision = self.fx.fixture.pending()
        with self.assertRaises(StorageError):
            self.fx.fixture.workflow.store.update(decision, deepcopy(decision.context), actor="test", action="unapproved_answer")
        self.assertEqual(len(self.native.events), 2)

    def test_concurrent_reset_claim_prevents_second_request_submitting_deletes(self):
        body, preview = self.preview()
        original = RyotViewingReset.delete
        attempts = []
        def while_deleting(adapter, event_id):
            if not attempts:
                attempts.append(self.submit(body, preview).status_code)
            return original(adapter, event_id)
        with patch.object(RyotViewingReset, "delete", while_deleting):
            self.assertEqual(self.submit(body, preview).status_code, 200)
        self.assertEqual(attempts, [409])
        self.assertEqual(sorted(self.deletes()), ["new", "old"])

    def test_partial_history_is_not_resettable(self):
        self.native.events.append(deepcopy(self.native.events[0]))
        response = self.fx.client.post("/api/maintenance/viewing/reset-preview", headers=self.fx.admin, json=self.body("clear_history"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.deletes(), [])

    def test_wrong_native_collection_owner_blocks_history_reset(self):
        self.native.reset_collection_owner = "another-account"
        self.assertFalse(self.item()["question"]["actions"][2]["enabled"])
        response = self.fx.client.post("/api/maintenance/viewing/reset-preview", headers=self.fx.admin, json=self.body("clear_history"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.deletes(), [])

    def test_pending_rating_for_another_event_blocks_reset_and_keeps_review_text(self):
        original = self.fx.fixture.pending()
        context = deepcopy(original.context)
        context["event_id"] = "old"
        context["operation"] = {"score": 4, "state": "started", "review_text": "Original retry text"}
        other = self.storage.create_pending_decision(media_key=original.media_key, decision_type="viewing_followup",
                    audience="shared_household", state_fingerprint="other", context=context)
        self.assertFalse(self.item()["question"]["actions"][2]["enabled"])
        response = self.fx.client.post("/api/maintenance/viewing/reset-preview", headers=self.fx.admin, json=self.body("clear_history"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.storage.get_pending_decision(other.decision_id).context["operation"]["review_text"], "Original retry text")
