"""Private-chat Telegram search and revalidated decision polling."""

from __future__ import annotations

import hashlib
import logging
import time
import threading
from dataclasses import dataclass, replace
from typing import Any

from curatarr.adapters import AdapterError
from curatarr.domain import MediaType
from curatarr.integrations.telegram.client import TelegramClient, TelegramError
from curatarr.services.telegram import TelegramDecisionCallbacks, TelegramPrompt, TelegramSearchCommand
from curatarr.services.viewing_workflow import ViewingResponseError
from curatarr.storage import Storage, StorageError
from curatarr.services.settings import InstallationSettings


LOGGER = logging.getLogger(__name__)


@dataclass
class TelegramBot:
    client: TelegramClient
    search: TelegramSearchCommand
    callbacks: TelegramDecisionCallbacks
    storage: Storage
    allowed_users: frozenset[int]
    admins: frozenset[int] = frozenset()
    followups: object | None = None
    removals: object | None = None
    viewing_worker: object | None = None
    automation_worker: object | None = None
    maintenance_worker: object | None = None
    source_worker: object | None = None

    @property
    def cursor_name(self) -> str:
        return "telegram:" + hashlib.sha256(self.client.token.encode()).hexdigest()[:24]

    def poll_once(self) -> None:
        offset = self.storage.get_cursor(self.cursor_name)
        for update in self.client.get_updates(offset):
            if update["update_id"] < offset:
                continue
            self.handle_update(update)
            offset = update["update_id"] + 1
            self.storage.set_cursor(self.cursor_name, offset)

    def run(self) -> None:
        LOGGER.info("Telegram search/decision polling started.")
        stop = threading.Event()
        workers = [threading.Thread(target=target, args=(stop,), daemon=True)
                   for target in (self.viewing_worker, self.automation_worker, self.maintenance_worker, self.source_worker) if target]
        for worker in workers:
            worker.start()
        try:
            while True:
                try:
                    self.poll_once()
                except TelegramError as exc:
                    if exc.retry_after is None:
                        raise
                    LOGGER.warning("Telegram request failed; retrying.")
                    time.sleep(exc.retry_after)
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=2)

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict) and not isinstance(callback.get("id"), str):
            return
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        if not isinstance(message, dict):
            return
        sender = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {})
        chat = message.get("chat", {})
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return
        if isinstance(callback, dict) and type(message.get("message_id")) is not int:
            return
        user_id = sender.get("id")
        text = message.get("text", "")
        data = callback.get("data", "") if isinstance(callback, dict) else ""
        viewing_command = text.strip().split(" ", 1)[0] if isinstance(text, str) else ""
        command_name, _, mentioned_bot = viewing_command.partition("@")
        followup_command = not isinstance(callback, dict) and command_name in {"/followups_here", "/followups_stop"}
        removal_callback = isinstance(data, str) and data.startswith("remove:")
        if self.removals and (removal_callback or not isinstance(callback, dict) and command_name == "/removals"):
            if (type(user_id) is not int or user_id not in self.allowed_users or type(chat.get("id")) is not int
                    or chat.get("type") not in {"private", "group", "supergroup"}
                    or chat.get("type") == "private" and chat["id"] != user_id):
                if isinstance(callback, dict):
                    self.client.answer_callback(callback["id"], "Only configured users may answer here.")
                return
            if isinstance(callback, dict):
                self.client.answer_callback(callback["id"])
            try:
                if removal_callback:
                    prompt = self.removals.respond(data, user_id, chat["id"], message["message_id"])
                    self.client.send_prompt(chat["id"], prompt, message_id=message["message_id"], replace_missing=False)
                else:
                    self.removals.reopen(chat["id"])
            except (AdapterError, StorageError, ValueError):
                self.client.send_prompt(chat["id"], TelegramPrompt(
                    "The removal could not be verified. Your request is retained; retry /removals or ask the maintainer."))
            return
        viewing_callback = isinstance(data, str) and data.startswith("view:")
        if self.followups and (followup_command or viewing_callback):
            if (type(user_id) is not int or user_id not in self.allowed_users or type(chat.get("id")) is not int
                    or chat.get("type") not in {"private", "group", "supergroup", "channel"}
                    or chat.get("type") == "private" and chat["id"] != user_id):
                if isinstance(callback, dict):
                    self.client.answer_callback(callback["id"], "Only configured users may answer here.")
                return
            if isinstance(callback, dict):
                self.client.answer_callback(callback["id"])
            try:
                if followup_command:
                    if user_id not in self.admins:
                        self.client.send_prompt(chat["id"], TelegramPrompt("Only an admin can configure viewing delivery."))
                        return
                    if mentioned_bot:
                        me = self.client.call("getMe", {})
                        if not isinstance(me, dict) or str(me.get("username", "")).casefold() != mentioned_bot.casefold():
                            return
                    if command_name == "/followups_here":
                        prompt = self.followups.activate(chat["id"], chat["type"])
                    else:
                        binding = self.followups.binding()
                        if not binding or binding["chat_id"] != chat["id"]:
                            raise ValueError("Stop follow-ups from their configured destination.")
                        self.followups.store.bind({**binding, "enabled": False})
                        prompt = TelegramPrompt("Viewing follow-ups paused. Existing questions remain open.")
                    self.client.send_prompt(chat["id"], prompt)
                else:
                    prompt = self.followups.respond(data, user_id=user_id, chat_id=chat["id"], message_id=message["message_id"],
                                                    user_name=" ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")))
                    # Keep the original message even if initial poster delivery fell
                    # back to text. Replacing it would orphan its persisted ID.
                    photos = message.get("photo") or []
                    prompt = replace(prompt, photo_url=photos[-1]["file_id"] if photos else None)
                    self.client.send_prompt(chat["id"], prompt, message_id=message["message_id"],
                                            current_has_photo=bool(photos), replace_missing=False)
            except ViewingResponseError as exc:
                self.client.send_prompt(chat["id"], TelegramPrompt(str(exc)))
            except (AdapterError, StorageError, ValueError):
                LOGGER.warning("Viewing follow-up could not be verified.")
                self.client.send_prompt(chat["id"], TelegramPrompt(
                    "This viewing response could not be verified. The question remains available; "
                    "a rating attempt may have reached Ryot. Retry the same answer or review externally."))
            return
        if (type(user_id) is not int or user_id not in self.allowed_users
                or chat.get("type") != "private" or chat.get("id") != user_id):
            if isinstance(callback, dict):
                self.client.answer_callback(callback["id"], "This bot is available to configured users in private chats.")
            return
        audience = f"telegram:user:{user_id}"
        if isinstance(callback, dict):
            # Acknowledge promptly, before potentially slow Ryot/Arr reads.
            self.client.answer_callback(callback["id"])
        try:
            if isinstance(callback, dict):
                result = self.callbacks.handle(str(callback.get("data", "")), audience=audience)
                if result.prompt is not None:
                    self.client.send_prompt(user_id, result.prompt, message_id=message["message_id"],
                                            current_has_photo=bool(message.get("photo")))
                else:
                    self.client.send_prompt(user_id, TelegramPrompt(result.notice))
                return
            text = message.get("text", "")
            if not isinstance(text, str):
                return
            command, _, query = text.strip().partition(" ")
            if command in {"/start", "/help"}:
                prompt = TelegramPrompt(f"Curatarr · {InstallationSettings(self.storage).display_name()}\nSearch movies with /movie followed by a title.\n"
                                        "Admins can change the name and automations in Mini App Settings.\n"
                                        "Use /removals to reopen movie removal requests in the configured follow-up chat.\n"
                                        "Admins: /followups_here enables viewing prompts in this chat; /followups_stop pauses them.")
            elif command == "/movie":
                prompt = self.search.search_choices(query, MediaType.MOVIE, audience=audience)
            elif text.startswith("/"):
                prompt = TelegramPrompt("Unknown command. Use /help.")
            else:
                prompt = self.search.search_choices(text, MediaType.MOVIE, audience=audience)
            self.client.send_prompt(user_id, prompt)
        except TelegramError:
            raise
        except AdapterError:
            LOGGER.warning("Media service unavailable while handling Telegram request.")
            self.client.send_prompt(user_id, TelegramPrompt(
                "A media service is unavailable. No media changes were made. Please try again later."
            ))
