"""Small Telegram Bot API transport with injectable requests for tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from curatarr.adapters import AdapterError
from curatarr.services.telegram import TelegramPrompt


class TelegramError(AdapterError):
    def __init__(self, message: str, *, retry_after: int | None = None,
                 message_missing: bool = False, media_unavailable: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.message_missing = message_missing
        self.media_unavailable = media_unavailable


@dataclass(frozen=True)
class TelegramClient:
    token: str = field(repr=False)
    transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in {"getUpdates", "sendMessage", "editMessageText", "answerCallbackQuery",
                          "sendPhoto", "editMessageMedia", "deleteMessage",
                          "getChatMenuButton", "setChatMenuButton", "getChat", "getMe"}:
            raise ValueError("Unsupported Telegram method.")
        response = self.transport(method, payload) if self.transport else self._request(method, payload)
        if not isinstance(response, dict):
            raise TelegramError("Telegram returned an invalid response.")
        if response.get("ok") is not True:
            code = response.get("error_code")
            description = str(response.get("description", "")).lower()
            if (method in {"editMessageText", "editMessageMedia"} and code == 400 and
                    "message is not modified" in description):
                return True
            if method == "deleteMessage" and code == 400 and "message to delete not found" in description:
                return True
            if (method == "answerCallbackQuery" and code == 400 and
                    ("query is too old" in description or "query id is invalid" in description)):
                return False
            parameters = response.get("parameters") or {}
            delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
            retry = max(1, delay) if type(delay) is int else 3
            raise TelegramError("Telegram rejected the request.",
                                retry_after=retry if code == 429 or (isinstance(code, int) and code >= 500) else None,
                                message_missing=method in {"editMessageText", "editMessageMedia"} and code == 400
                                and "message to edit not found" in description,
                                media_unavailable=method in {"sendPhoto", "editMessageMedia"} and code == 400
                                and any(fragment in description for fragment in (
                                    "failed to get http url", "wrong type of the web page", "image_process_failed",
                                    "webpage_curl_failed", "wrong file identifier", "photo_invalid",
                                )))
        return response.get("result")

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=40) as response:
                body = response.read()
        except HTTPError as exc:
            # Error bodies and URLs can contain bot credentials. Only expose a fixed message.
            try:
                decoded = json.loads(exc.read())
                if isinstance(decoded, dict) and decoded.get("ok") is False:
                    decoded["error_code"] = exc.code
                    return decoded
            except (ValueError, OSError):
                pass
            raise TelegramError("Telegram HTTP request failed.",
                                retry_after=3 if exc.code == 429 or exc.code >= 500 else None) from None
        except (URLError, OSError):
            raise TelegramError("Telegram connection failed.", retry_after=3) from None
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise TelegramError("Telegram returned invalid JSON.") from None

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = self.call("getUpdates", {
            "offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"],
        })
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or type(item.get("update_id")) is not int for item in result
        ):
            raise TelegramError("Telegram returned invalid updates.")
        return result

    def send_prompt(self, chat_id: int, prompt: TelegramPrompt, *, message_id: int | None = None,
                    current_has_photo: bool = False, replace_missing: bool = True) -> Any:
        keyboard = []
        previous_row = None
        for button in prompt.buttons:
            item = {"text": button.label, "callback_data": button.callback_data}
            if button.row is not None and button.row == previous_row:
                keyboard[-1].append(item)
            else:
                keyboard.append([item])
            previous_row = button.row
        payload = {"chat_id": chat_id, "reply_markup": {"inline_keyboard": keyboard}}
        # Telegram cannot remove a photo by editing its text. Replace the message when
        # changing between a poster and a text list/detail, after successful delivery.
        replace_message = message_id is not None and bool(prompt.photo_url) != current_has_photo
        if replace_message and not replace_missing:
            raise TelegramError("The recorded prompt cannot be replaced with another message.")
        edit_id = None if replace_message else message_id
        if prompt.photo_url:
            caption = prompt.text.encode("utf-16-le")[:2048].decode("utf-16-le", errors="ignore")
            if edit_id is None:
                method = "sendPhoto"
                payload.update(photo=prompt.photo_url, caption=caption)
            else:
                method = "editMessageMedia"
                payload["media"] = {"type": "photo", "media": prompt.photo_url, "caption": caption}
        else:
            method = "sendMessage" if edit_id is None else "editMessageText"
            payload["text"] = prompt.text
        if edit_id is not None:
            payload["message_id"] = edit_id
        try:
            result = self.call(method, payload)
        except TelegramError as exc:
            if exc.media_unavailable:
                return self.send_prompt(chat_id, replace(prompt, photo_url=None,
                                        text=prompt.text + "\n\nPoster unavailable."),
                                        message_id=message_id, current_has_photo=current_has_photo, replace_missing=replace_missing)
            if exc.message_missing and replace_missing:
                return self.send_prompt(chat_id, prompt)
            raise
        if replace_message:
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        return result

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
