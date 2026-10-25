"""Telegram photo delivery and photo/text transitions keep the result controls usable."""

import unittest

from curatarr.integrations.telegram.client import TelegramClient, TelegramError
from curatarr.services.telegram import TelegramButton, TelegramPrompt


class TelegramPhotoTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def transport(method, payload):
            self.calls.append((method, dict(payload)))
            return {"ok": True, "result": True}
        self.client = TelegramClient("test-token", transport)
        self.prompt = TelegramPrompt(
            "Movie (2026)\nResult 2 of 3",
            buttons=(TelegramButton("< Previous", "browse:1:0", row=0),
                     TelegramButton("Next >", "browse:1:2", row=0),
                     TelegramButton("Select movie", "select:1:1", row=1)),
            photo_url="https://image.tmdb.org/t/p/w500/example.jpg",
        )

    def test_new_poster_sends_photo_with_caption_and_grouped_navigation(self):
        self.client.send_prompt(1, self.prompt)
        method, payload = self.calls[0]
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(payload["photo"], self.prompt.photo_url)
        self.assertEqual(payload["caption"], self.prompt.text)
        self.assertEqual([len(row) for row in payload["reply_markup"]["inline_keyboard"]], [2, 1])
        self.assertNotIn("parse_mode", payload)

    def test_poster_navigation_edits_existing_media(self):
        self.client.send_prompt(1, self.prompt, message_id=5, current_has_photo=True)
        self.assertEqual(len(self.calls), 1)
        method, payload = self.calls[0]
        self.assertEqual(method, "editMessageMedia")
        self.assertEqual(payload["message_id"], 5)
        self.assertEqual(payload["media"], {"type": "photo", "media": self.prompt.photo_url,
                                           "caption": self.prompt.text})

    def test_photo_to_list_delivers_text_before_removing_old_photo(self):
        self.client.send_prompt(1, TelegramPrompt("Select a title."), message_id=5, current_has_photo=True)
        self.assertEqual([method for method, _ in self.calls], ["sendMessage", "deleteMessage"])
        self.assertEqual(self.calls[-1][1]["message_id"], 5)

    def test_list_to_photo_delivers_photo_before_removing_old_list(self):
        self.client.send_prompt(1, self.prompt, message_id=5)
        self.assertEqual([method for method, _ in self.calls], ["sendPhoto", "deleteMessage"])

    def test_failed_new_delivery_never_removes_existing_message(self):
        def fail(method, payload):
            self.calls.append((method, payload))
            return {"ok": False, "error_code": 500}
        client = TelegramClient("token", fail)
        with self.assertRaises(TelegramError):
            client.send_prompt(1, self.prompt, message_id=5)
        self.assertEqual([method for method, _ in self.calls], ["sendPhoto"])

    def test_unavailable_photo_falls_back_to_text_and_preserves_controls(self):
        def unavailable(method, payload):
            self.calls.append((method, payload))
            if method == "sendPhoto":
                return {"ok": False, "error_code": 400, "description": "Bad Request: failed to get HTTP URL content"}
            return {"ok": True, "result": True}
        client = TelegramClient("token", unavailable)
        client.send_prompt(1, self.prompt)
        self.assertEqual([method for method, _ in self.calls], ["sendPhoto", "sendMessage"])
        self.assertIn("Poster unavailable", self.calls[-1][1]["text"])
        self.assertEqual(self.calls[-1][1]["reply_markup"], self.calls[0][1]["reply_markup"])

    def test_photo_caption_respects_utf16_limit(self):
        prompt = TelegramPrompt("\U0001f3ac" * 2000, photo_url=self.prompt.photo_url)
        self.client.send_prompt(1, prompt)
        self.assertLessEqual(len(self.calls[0][1]["caption"].encode("utf-16-le")) // 2, 1024)

    def test_fixed_message_id_disallows_photo_text_replacement(self):
        with self.assertRaises(TelegramError):
            self.client.send_prompt(1, self.prompt, message_id=5, replace_missing=False)
        self.assertEqual(self.calls, [])

    def test_failed_fixed_photo_edit_does_not_send_replacement_text(self):
        def unavailable(method, payload):
            self.calls.append((method, payload))
            return {"ok": False, "error_code": 400, "description": "Bad Request: wrong file identifier"}
        client = TelegramClient("token", unavailable)
        with self.assertRaises(TelegramError):
            client.send_prompt(1, self.prompt, message_id=5, current_has_photo=True, replace_missing=False)
        self.assertEqual([method for method, _ in self.calls], ["editMessageMedia"])

    def test_unchanged_media_edit_is_nonfatal(self):
        client = TelegramClient("token", lambda *_: {"ok": False, "error_code": 400,
                                "description": "Bad Request: message is not modified"})
        client.send_prompt(1, self.prompt, message_id=5, current_has_photo=True)
