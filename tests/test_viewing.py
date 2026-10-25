"""Viewing safeguard stays separate from technical sync and search decisions."""

from dataclasses import replace
import unittest

from curatarr.domain import DurableState as State, MediaIdentity
from curatarr.services.viewing import ViewingFollowupService
import test_maintenance as fixtures


class ViewingTests(unittest.TestCase):
    def setUp(self):
        fixtures.MaintenanceTests.setUp(self)
        self.service = ViewingFollowupService(self.storage, self.ryot)

    def add_question(self, key="ryot:1", **overrides):
        args = dict(media_key=key, decision_type="viewing_followup", audience="shared_household",
                    state_fingerprint="fixture", context={"questions": ["watched", "rating", "retention"]})
        args.update(overrides)
        return self.storage.create_pending_decision(**args)

    def test_empty_does_not_turn_downloads_search_or_sync_into_viewing_questions(self):
        self.add_question(decision_type="search_add")
        self.add_question(audience="telegram:99")
        self.assertEqual(self.service.review(1)["movie_count"], 0)
        self.assertFalse(self.service.review(1)["delivery_enabled"])

    def test_genuine_questions_use_fresh_ryot_facts_and_group_by_movie(self):
        self.add_question()
        self.add_question(context={"questions": ["rating"]})
        self.ryot.items[0] = replace(self.ryot.items[0], completed_watch_count=2, last_watched_date="2026-10-22",
                                   durable_states={State.WATCHED, State.LIBRARY})
        data = self.service.review(1)
        self.assertEqual(data["movie_count"], 1)
        self.assertTrue(data["validated"])
        self.assertEqual(data["movie"]["watch_count"], 2)
        self.assertEqual(len(data["movie"]["questions"]), 3)
        self.assertFalse(data["updates_enabled"])

    def test_legacy_postponed_questions_return_to_one_list_and_navigation_never_clears(self):
        self.add_question()
        self.add_question("ryot:2")
        self.storage.save_maintenance_progress(1, {"cursor": "ryot:1", "skipped": ["ryot:1"],
                                                 "category": "all", "parked": True}, source="viewing_progress")
        first = self.service.review(1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["progress"]["skipped"], [])
        self.assertFalse(first["progress"]["parked"])
        second = self.service.review(1, "next", expected_cursor=first["progress"]["cursor"])
        self.assertEqual(second["movie"]["key"], "ryot:2")
        self.assertEqual(self.service.review(1)["movie"]["key"], "ryot:2")
        self.assertEqual(self.storage.maintenance_progress(1)["skipped"], [])
        self.assertEqual(self.service.review(2)["total"], 2)
        self.assertEqual(len(self.storage.viewing_followups()), 2)

    def test_answered_questions_disappear_but_expired_unanswered_questions_remain(self):
        resolved = self.add_question()
        self.storage.close_open_decision(resolved.decision_id, "resolved")
        expired = self.add_question("ryot:2")
        with self.storage.connection:
            self.storage.connection.execute("UPDATE pending_decision SET expires_at='2000-01-01' WHERE id=?", (expired.decision_id,))
        self.assertEqual(self.service.review(1)["movie_count"], 1)
        self.assertEqual(self.service.review(1)["movie"]["key"], "ryot:2")
        self.assertEqual(self.storage.get_pending_decision(expired.decision_id).status, "open")

    def test_unavailable_identity_or_malformed_question_hides_current_movie(self):
        decision = self.add_question()
        self.ryot.error = True
        self.assertIsNone(self.service.review(1)["movie"])
        self.ryot.error = False
        self.ryot.items[0] = replace(self.ryot.items[0], identities={MediaIdentity("ryot", "other")})
        self.assertFalse(self.service.review(1)["validated"])
        self.storage.close_open_decision(decision.decision_id, "resolved")
        self.add_question("ryot:2", context={"questions": ["delete_now"]})
        self.assertFalse(self.service.review(1)["validated"])

    def test_stale_navigation_and_arbitrary_actions_rejected(self):
        self.add_question()
        self.service.review(1)
        with self.assertRaises(ValueError):
            self.service.review(1, "next", expected_cursor="ryot:999")
        for action in ("archive", "skip", "skipped", "pending"):
            with self.assertRaises(ValueError):
                self.service.review(1, action)
