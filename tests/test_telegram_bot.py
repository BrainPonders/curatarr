"""Offline Bot API transport and polling integration tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from curatarr.adapters import AdapterError
from curatarr.integrations.telegram.bot import TelegramBot
from curatarr.integrations.telegram.client import TelegramClient, TelegramError
from curatarr.services.telegram import TelegramCallbackResult, TelegramPrompt
from curatarr.storage import Storage
from curatarr.storage.sqlite import SCHEMA_VERSION


class TelegramBotTests(unittest.TestCase):
    def test_menu_button_methods_use_the_existing_safe_transport(self):
        calls = []
        button = {"type": "web_app", "text": "Curatarr",
                  "web_app": {"url": "https://example.com"}}
        def transport(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": button if method == "getChatMenuButton" else True}
        client = TelegramClient("test-token", transport)
        self.assertTrue(client.call("setChatMenuButton", {"chat_id": 1, "menu_button": button}))
        self.assertEqual(client.call("getChatMenuButton", {"chat_id": 1}), button)
        self.assertEqual([method for method, _ in calls], ["setChatMenuButton", "getChatMenuButton"])
        self.assertTrue(all(payload["chat_id"] == 1 for _, payload in calls))
        with self.assertRaises(ValueError):
            client.call("setWebhook", {})

    def setUp(self):
        self.storage = Storage(":memory:")
        self.storage.initialize()
        self.addCleanup(self.storage.close)
        self.calls = []
        self.updates = []
        def transport(method, payload):
            self.calls.append((method, payload))
            return {"ok": True, "result": self.updates if method == "getUpdates" else True}
        self.client = TelegramClient("test-secret-token", transport)
        self.search = Mock()
        self.search.search_choices.return_value = TelegramPrompt("Choose a title")
        self.callbacks = Mock()
        self.callbacks.handle.return_value = TelegramCallbackResult("Selected", TelegramPrompt("Movie details"))
        self.bot = TelegramBot(self.client, self.search, self.callbacks, self.storage, frozenset({1}))

    def message(self, user=1, chat_type="private"):
        return {"update_id": 20, "message": {"message_id": 5, "from": {"id": user},
                "chat": {"type": chat_type, "id": user}, "text": "/movie Spider-Man"}}

    def test_authorized_search_and_offset_resume(self):
        self.updates = [self.message()]
        self.bot.poll_once()
        self.search.search_choices.assert_called_once()
        self.assertEqual(self.calls[-1][0], "sendMessage")
        restarted = TelegramBot(self.client, self.search, self.callbacks, self.storage, frozenset({1}))
        restarted.poll_once()
        self.assertEqual(self.calls[-1][1]["offset"], 21)
        self.search.search_choices.assert_called_once()

    def test_unknown_user_and_group_do_not_trigger_search(self):
        self.bot.handle_update(self.message(user=2))
        self.bot.handle_update(self.message(chat_type="group"))
        self.search.search_choices.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_callback_is_acknowledged_and_message_buttons_replaced(self):
        update = {"callback_query": {"id": "cb", "from": {"id": 1}, "data": "select:1:0",
                  "message": self.message()["message"]}}
        self.bot.handle_update(update)
        self.assertEqual([call[0] for call in self.calls], ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(self.calls[-1][1]["reply_markup"], {"inline_keyboard": []})
        self.callbacks.handle.assert_called_with("select:1:0", audience="telegram:user:1")

    def test_backend_failure_is_generic_and_keeps_buttons(self):
        self.search.search_choices.side_effect = AdapterError("SECRET-URL")
        self.bot.handle_update(self.message())
        self.assertNotIn("SECRET", self.calls[-1][1]["text"])
        self.assertIn("unavailable", self.calls[-1][1]["text"])

    def test_failed_delivery_does_not_advance_offset(self):
        self.updates = [self.message()]
        with patch.object(TelegramClient, "send_prompt", side_effect=TelegramError("failed")):
            with self.assertRaises(TelegramError):
                self.bot.poll_once()
        self.assertEqual(self.storage.get_cursor(self.bot.cursor_name), 0)

    def test_transport_error_and_repr_do_not_expose_token(self):
        client = TelegramClient("private-token")
        with patch("curatarr.integrations.telegram.client.urlopen", side_effect=URLError("private-token")):
            with self.assertRaises(TelegramError) as raised:
                client.get_updates(0)
        self.assertNotIn("private-token", str(raised.exception))
        self.assertNotIn("private-token", repr(client))

    def test_api_error_and_rate_limit_are_sanitized(self):
        client = TelegramClient("token", lambda *_: {"ok": False, "error_code": 429,
                                "description": "private-token", "parameters": {"retry_after": 10}})
        with self.assertRaises(TelegramError) as raised:
            client.get_updates(0)
        self.assertEqual(raised.exception.retry_after, 10)
        self.assertNotIn("private-token", str(raised.exception))

    def test_unchanged_edit_is_successful(self):
        client = TelegramClient("token", lambda *_: {"ok": False, "error_code": 400,
                                "description": "Bad Request: message is not modified"})
        client.send_prompt(1, TelegramPrompt("same"), message_id=2)

    def test_expired_callback_acknowledgement_is_nonfatal(self):
        client = TelegramClient("token", lambda *_: {"ok": False, "error_code": 400,
                                "description": "Bad Request: query is too old and response timeout expired"})
        client.answer_callback("old-callback")

    def test_deleted_prompt_falls_back_to_new_message(self):
        calls = []
        def transport(method, payload):
            calls.append((method, dict(payload)))
            if method == "editMessageText":
                return {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}
            return {"ok": True, "result": True}
        TelegramClient("token", transport).send_prompt(1, TelegramPrompt("current"), message_id=2)
        self.assertEqual([method for method, _ in calls], ["editMessageText", "sendMessage"])
        self.assertNotIn("message_id", calls[-1][1])

    def test_real_http_request_uses_post_json_and_no_parse_mode(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true, "result": {"message_id": 1}}'
        with patch("curatarr.integrations.telegram.client.urlopen", return_value=response) as send:
            TelegramClient("token").send_prompt(1, TelegramPrompt("A < B & C"))
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.method, "POST")
        self.assertEqual(payload["text"], "A < B & C")
        self.assertNotIn("parse_mode", payload)

    def test_schema_upgrade_preserves_decisions_and_cursor_across_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite"
            storage = Storage(path)
            storage.initialize()
            decision = storage.create_pending_decision(media_key="tmdb:1", decision_type="search_add",
                         audience="telegram:user:1", state_fingerprint="abc")
            storage.connection.execute("UPDATE schema_version SET version = 1")
            storage.connection.execute("DROP TABLE runtime_cursor")
            storage.connection.commit()
            storage.close()
            storage = Storage(path)
            try:
                storage.initialize()
                storage.set_cursor("bot", 44)
                self.assertEqual(storage.get_pending_decision(decision.decision_id), decision)
                self.assertEqual(storage.schema_version(), SCHEMA_VERSION)
            finally:
                storage.close()
            storage = Storage(path)
            try:
                self.assertEqual(storage.get_cursor("bot"), 44)
            finally:
                storage.close()
