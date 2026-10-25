"""Telegram message expiry preserves questions, answers and event-specific recovery."""

from copy import deepcopy
import unittest
from unittest.mock import Mock

from curatarr.integrations.telegram.client import TelegramError
from curatarr.adapters import AdapterError
from curatarr.services.prompt_cleanup import TelegramPromptCleanup
from curatarr.services.settings import InstallationSettings
from curatarr.services.viewing import ViewingFollowupService
import test_viewing_workflow as fixtures


class PromptCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.ViewingWorkflowTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.question = self.fx.create_question()
        self.now = self.question.context["delivery"]["sent_at"] + 129600
        self.client = Mock(wraps=self.fx.client)
        self.worker = TelegramPromptCleanup(self.fx.storage, self.client, self.fx.workflow.bot_key, clock=lambda: self.now)
        self.settings = InstallationSettings(self.fx.storage)

    def policy(self, answered=None, unanswered=None):
        self.settings.save(expected_revision=self.settings.get()["revision"], actor="admin",
                           cleanup_answered_prompts=answered, cleanup_unanswered_prompts=unanswered)

    def current(self):
        return self.fx.storage.get_pending_decision(self.question.decision_id)

    def delivery(self, **changes):
        decision = self.current()
        return self.fx.workflow.store.update_delivery(decision, {**decision.context["delivery"], **changes},
                                                      actor="test", action="fixture_delivery")

    def test_disabled_and_answered_only_leave_unanswered_prompt_alone(self):
        self.worker.tick()
        self.policy(answered=True)
        self.worker.tick()
        self.client.call.assert_not_called()
        self.assertEqual(self.current().status, "open")

    def test_unanimous_decline_uses_answered_cleanup_and_preserves_admin_review(self):
        context = deepcopy(self.current().context)
        context["declined_all"] = True
        self.fx.workflow.store.update(self.current(), context, actor="test", action="declined_fixture")
        self.policy(unanswered=True)
        self.worker.tick()
        self.client.call.assert_not_called()
        self.policy(answered=True, unanswered=False)
        self.worker.tick()
        self.client.call.assert_called_once_with("deleteMessage", {"chat_id": -100, "message_id": 1})
        self.assertTrue(self.current().context["declined_all"])
        self.assertEqual(self.current().status, "open")

    def test_unanswered_only_removes_at_36_hours_without_resolving_or_writing_to_ryot(self):
        self.policy(unanswered=True)
        self.now -= 1
        self.worker.tick()
        self.client.call.assert_not_called()
        self.now += 1
        before = deepcopy(self.fx.native.events)
        self.worker.tick()
        self.client.call.assert_called_once_with("deleteMessage", {"chat_id": -100, "message_id": 1})
        self.assertEqual(self.current().context["delivery"]["state"], "removed")
        self.assertEqual(self.current().status, "open")
        self.assertEqual(self.fx.native.events, before)
        self.assertEqual(self.fx.native.reviews, [])
        service = ViewingFollowupService(self.fx.storage, self.fx.reader, self.fx.workflow.bot_key)
        self.assertEqual(service.review(1)["movie"]["reopen_decision_id"], self.question.decision_id)

    def test_answered_switch_independent_of_unanswered_switch_and_preserves_rating(self):
        self.fx.answer("yes")
        self.fx.answer("r8")
        before = deepcopy((self.fx.native.events, self.fx.native.reviews))
        self.policy(unanswered=True)
        self.worker.tick()
        self.client.call.assert_not_called()
        self.policy(answered=True)
        self.worker.tick()
        self.assertEqual(self.current().context["delivery"]["state"], "removed")
        self.assertEqual(self.current().status, "resolved")
        self.assertEqual((self.fx.native.events, self.fx.native.reviews), before)

    def test_reopen_reuses_question_and_rejects_old_callbacks_or_duplicate_reopen(self):
        self.policy(unanswered=True)
        self.worker.tick()
        self.fx.workflow.reopen(self.question.decision_id, self.question.media_key, user_id=1)
        with self.assertRaises(ValueError):
            self.fx.workflow.reopen(self.question.decision_id, self.question.media_key, user_id=1)
        self.fx.workflow.tick()
        self.assertEqual(len(self.fx.sent), 2)
        self.assertEqual(len(self.fx.storage.viewing_followups()), 1)
        self.assertEqual(self.current().context["delivery"]["message_id"], 2)
        with self.assertRaises(ValueError):
            self.fx.answer("yes", decision=self.question)
        self.fx.answer("yes")
        self.assertTrue(self.current().context["confirmed"])

    def test_disputed_or_changed_native_event_cannot_be_reopened(self):
        self.fx.answer("no")
        self.policy(unanswered=True)
        self.worker.tick()
        with self.assertRaises(ValueError):
            self.fx.workflow.reopen(self.question.decision_id, self.question.media_key, user_id=1)
        decision = self.current()
        context = deepcopy(decision.context)
        context["disputed"] = False
        self.fx.workflow.store.update(decision, context, actor="test", action="fixture")
        self.fx.native.events.pop()
        with self.assertRaises(AdapterError):
            self.fx.workflow.reopen(self.question.decision_id, self.question.media_key, user_id=1)
        self.assertEqual(self.current().context["delivery"]["state"], "removed")

    def test_expired_deletion_window_is_skipped_and_unanswered_question_remains(self):
        self.policy(unanswered=True)
        self.now += 43200
        self.worker.tick()
        self.client.call.assert_not_called()
        self.assertEqual(self.current().context["delivery"]["cleanup_status"], "too_old")
        self.assertEqual(self.current().status, "open")

    def test_unknown_or_uncertain_deliveries_are_not_guessed(self):
        self.policy(unanswered=True)
        self.delivery(state="uncertain", sent_at=None)
        self.worker.tick()
        self.client.call.assert_not_called()
        # Remove dated delivery evidence as well; an old row alone is not send proof.
        with self.fx.storage.connection:
            self.fx.storage.connection.execute("DELETE FROM audit_log WHERE action='viewing_prompt_delivered'")
        self.delivery(state="sent", sent_at=None)
        self.worker.tick()
        self.client.call.assert_not_called()
        self.assertEqual(self.current().context["delivery"]["cleanup_status"], "unknown_age")

    def test_legacy_confirmed_delivery_uses_exact_question_audit_date(self):
        self.policy(unanswered=True)
        self.delivery(sent_at=None)
        self.worker.tick()
        self.client.call.assert_called_once()
        self.assertEqual(self.current().context["delivery"]["state"], "removed")

    def test_lost_delete_response_retries_only_same_message_after_restart(self):
        self.policy(unanswered=True)
        self.client.call.side_effect = TelegramError("Response lost")
        self.worker.tick()
        self.assertEqual(self.current().context["delivery"]["state"], "sent")
        self.client.call.side_effect = None
        self.client.call.return_value = True
        self.now += 301
        TelegramPromptCleanup(self.fx.storage, self.client, self.fx.workflow.bot_key, clock=lambda: self.now).tick()
        self.assertEqual(self.client.call.call_count, 2)
        self.assertEqual(self.client.call.call_args_list[0], self.client.call.call_args_list[1])
        self.assertEqual(self.current().context["delivery"]["state"], "removed")

    def test_uncertain_removal_after_long_outage_can_be_explicitly_reopened(self):
        self.policy(unanswered=True)
        self.client.call.side_effect = TelegramError("Response lost")
        self.worker.tick()
        self.now += 43201
        self.worker.tick()
        self.assertEqual(self.current().context["delivery"]["state"], "uncertain_removal")
        self.fx.workflow.reopen(self.question.decision_id, self.question.media_key, user_id=1)
        self.assertEqual(self.current().context["delivery"]["state"], "pending")

    def test_wrong_bot_or_destination_cannot_delete(self):
        self.policy(unanswered=True)
        TelegramPromptCleanup(self.fx.storage, self.client, "other-bot", clock=lambda: self.now).tick()
        self.delivery(chat_id=-999)
        self.worker.tick()
        self.client.call.assert_not_called()

    def test_clear_request_and_rating_banners(self):
        self.assertTrue(self.fx.workflow.prompt(self.current()).text.startswith("👀 WATCH CONFIRMATION REQUEST\n"))
        self.assertTrue(self.fx.answer("yes").text.startswith("⭐ RATING REQUEST\n"))
        self.assertTrue(self.fx.answer("r8").text.startswith("✅ VIEWING CONFIRMED\n"))

    def test_reply_during_deletion_is_preserved(self):
        self.policy(unanswered=True)
        def delete(method, payload):
            self.fx.answer("yes")
            self.fx.answer("r8")
            return True
        self.client.call.side_effect = delete
        self.worker.tick()
        self.assertEqual(self.current().status, "resolved")
        self.assertEqual(self.current().context["rating"], 8)
        self.assertEqual(self.current().context["delivery"]["state"], "removed")

    def test_existing_open_message_gets_banner_without_replacement(self):
        decision = self.current()
        delivery = {**decision.context["delivery"]}
        delivery.pop("presentation_version")
        self.fx.workflow.store.update_delivery(decision, delivery, actor="test", action="legacy_presentation")
        client = Mock(wraps=self.fx.client)
        self.fx.workflow.client = client
        self.fx.workflow.tick()
        args, kwargs = client.send_prompt.call_args
        self.assertTrue(args[1].text.startswith("👀 WATCH CONFIRMATION REQUEST\n"))
        self.assertEqual(kwargs["message_id"], 1)
        self.assertFalse(kwargs["replace_missing"])
        self.assertEqual(len(self.fx.sent), 1)
        self.assertEqual(self.current().context["delivery"]["presentation_version"], 2)
