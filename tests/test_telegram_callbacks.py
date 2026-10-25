"""Authorization, revalidation, and restart behavior of Telegram decisions."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from curatarr.adapters import AdapterError
from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaType
from curatarr.integrations.telegram.bot import TelegramBot
from curatarr.integrations.telegram.client import TelegramClient
from curatarr.services.decisions import PendingDecisionService
from curatarr.services.identity import IdentityMatcher
from curatarr.services.search import SearchDecisionService
from curatarr.services.telegram import TelegramDecisionCallbacks, TelegramSearchCommand
from curatarr.storage import Storage
from test_telegram_service import _FakeMetadataAdapter, _FakeRadarrAdapter, _FakeRyotAdapter, _movie


class TelegramCallbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "decisions.sqlite"
        self.storage = Storage(self.path)
        self.addCleanup(lambda: self.storage.close())
        self.storage.initialize()
        self.ryot = _FakeRyotAdapter(None)
        self.arr = _FakeRadarrAdapter(None)
        self.service = SearchDecisionService(
            _FakeMetadataAdapter((_movie(), _movie(title="Another Movie"))), self.ryot, IdentityMatcher(),
            radarr_adapter=self.arr, pending_decision_service=PendingDecisionService(self.storage),
        )
        self.command = TelegramSearchCommand(self.service)
        self.callbacks = TelegramDecisionCallbacks(self.service, self.storage)
        self.audience = "telegram:user:1"

    def prompt(self):
        return self.command.handle_search("Spider-Man", MediaType.MOVIE, audience=self.audience)

    def click(self, data, audience=None):
        return self.callbacks.handle(data, audience=audience or self.audience)

    def test_search_requires_user_to_select_title(self):
        choices = self.command.search_choices("Spider-Man", MediaType.MOVIE, audience=self.audience)
        self.assertIn("Result 1 of 2", choices.text)
        listing = self.click(next(button.callback_data for button in choices.buttons if button.callback_data.startswith("list:"))).prompt
        result = self.click(listing.buttons[1].callback_data)
        self.assertIn("Another Movie", result.prompt.text)
        self.assertEqual(self.storage.get_pending_decision(choices.pending_decision_id).status, "resolved")
        repeated = self.click(listing.buttons[0].callback_data)
        self.assertIn("already closed", repeated.notice)
        self.assertIsNone(repeated.prompt)

    def test_cross_user_callback_does_not_read_services_or_close_prompt(self):
        prompt = self.prompt()
        self.ryot.get_item = lambda _: self.fail("Unauthorized callback read Ryot")
        result = self.click(prompt.buttons[0].callback_data, "telegram:user:2")
        self.assertIsNone(result.prompt)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")

    def test_expired_decision_is_closed_without_reading_services(self):
        prompt = self.prompt()
        self.storage.connection.execute("UPDATE pending_decision SET expires_at = '2000-01-01 00:00:00'")
        self.storage.connection.commit()
        self.ryot.get_item = lambda _: self.fail("Expired callback read Ryot")
        result = self.click(prompt.buttons[0].callback_data)
        self.assertEqual(result.prompt.buttons, ())
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "expired")

    def test_changed_state_replaces_prompt_and_requires_new_click(self):
        prompt = self.prompt()
        self.ryot.item = _movie(durable_states={DurableState.ARCHIVED})
        result = self.click(prompt.buttons[0].callback_data)
        self.assertIn("State changed", result.notice)
        self.assertIn("archived", result.prompt.text)
        self.assertNotEqual(prompt.pending_decision_id, result.prompt.pending_decision_id)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "superseded")

    def test_forged_action_is_rejected(self):
        prompt = self.prompt()
        result = self.click(f"decision:{prompt.pending_decision_id}:archive_cancel")
        self.assertIn("not offered", result.notice)
        self.assertIsNone(result.prompt)

    def test_valid_acquisition_action_stays_open_and_does_not_claim_success(self):
        prompt = self.prompt()
        result = self.click(prompt.buttons[0].callback_data)
        self.assertIn("not available yet", result.notice)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], 0)

    def test_keep_as_is_is_terminal_and_audited_once(self):
        self.arr.item = _movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)
        prompt = self.prompt()
        data = f"decision:{prompt.pending_decision_id}:keep_as_is"
        result = self.click(data)
        self.assertEqual(result.notice, "Kept as is.")
        self.assertEqual(result.prompt.buttons, ())
        self.assertIn("already closed", self.click(data).notice)
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], 1)

    def test_weak_identity_blocks_action(self):
        self.arr.item = _movie(identities={MediaIdentity("imdb", "tt0316654")},
                               runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)
        prompt = self.prompt()
        result = self.click(f"decision:{prompt.pending_decision_id}:keep_as_is")
        self.assertIn("Identity review", result.notice)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")

    def test_failed_revalidation_does_not_close_decision(self):
        prompt = self.prompt()
        def unavailable(_):
            raise AdapterError("unavailable")
        self.ryot.get_item = unavailable
        with self.assertRaises(AdapterError):
            self.click(prompt.buttons[0].callback_data)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")

    def test_selection_survives_storage_reopen(self):
        choices = self.command.search_choices("Spider-Man", MediaType.MOVIE, audience=self.audience)
        self.storage.close()
        self.storage = Storage(self.path)
        self.storage.initialize()
        service = replace(self.service, pending_decision_service=PendingDecisionService(self.storage))
        self.callbacks = TelegramDecisionCallbacks(service, self.storage)
        result = self.click(next(button.callback_data for button in choices.buttons if button.callback_data.startswith("select:")))
        self.assertIn("Spider-Man 2", result.prompt.text)

    def test_malformed_and_unknown_callbacks_do_not_mutate_storage(self):
        for data in ("", "decision:x:add", "decision:-1:add", "decision:999:add", "select:999:0",
                     "decision:999999999999999999999999:add"):
            with self.subTest(data=data):
                self.assertIsNone(self.click(data).prompt)

    def test_selection_index_bounds_and_wrong_callback_kind(self):
        prompt = self.prompt()
        self.assertIsNone(self.click(f"select:{prompt.pending_decision_id}:0").prompt)
        choices = self.command.search_choices("movie", MediaType.MOVIE, audience=self.audience)
        for value in ("-1", "2", "x", "999999"):
            self.assertIsNone(self.click(f"select:{choices.pending_decision_id}:{value}").prompt)
        self.assertIsNone(self.click(f"decision:{choices.pending_decision_id}:add").prompt)

    def test_polling_search_selection_and_stale_button_end_to_end(self):
        updates = []
        sent = []
        def transport(method, payload):
            if method == "getUpdates":
                return {"ok": True, "result": updates[:]}
            sent.append((method, payload))
            return {"ok": True, "result": True}
        bot = TelegramBot(TelegramClient("test-token", transport), self.command, self.callbacks,
                          self.storage, frozenset({1}))
        message = {"message_id": 9, "from": {"id": 1}, "chat": {"id": 1, "type": "private"},
                   "text": "/movie Spider-Man"}
        updates[:] = [{"update_id": 1, "message": message}]
        bot.poll_once()
        selection = next(button["callback_data"] for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
                         for button in row if button["callback_data"].startswith("select:"))
        updates[:] = [{"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": 1},
                      "message": message, "data": selection}}]
        bot.poll_once()
        self.assertEqual(sent[-1][0], "editMessageText")
        self.assertIn("Found a new title", sent[-1][1]["text"])
        add = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.ryot.item = _movie(durable_states={DurableState.ARCHIVED, DurableState.WATCHED})
        updates[:] = [{"update_id": 3, "callback_query": {"id": "cb2", "from": {"id": 1},
                      "message": message, "data": add}}]
        bot.poll_once()
        self.assertIn("This title is archived", sent[-1][1]["text"])
        self.assertIn("already watched", sent[-1][1]["text"])
        self.assertEqual(self.storage.get_cursor(bot.cursor_name), 4)
        self.assertEqual(self.storage.get_pending_decision(int(add.split(":")[1])).status, "superseded")
