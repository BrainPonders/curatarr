"""Optional 36-hour Telegram message cleanup, independent from question resolution."""

import json
import time
from datetime import datetime, timezone

from curatarr.adapters import AdapterError
from curatarr.services.settings import InstallationSettings
from curatarr.storage import StorageError
from curatarr.storage.viewing import ViewingStore


class TelegramPromptCleanup:
    def __init__(self, storage, client, bot_key, *, clock=time.time):
        self.storage, self.client, self.bot_key, self.clock = storage, client, bot_key, clock
        self.settings, self.store = InstallationSettings(storage), ViewingStore(storage)

    def tick(self):
        settings = self.settings.get()
        binding = self.store.binding()
        if (not (settings["cleanup_answered_prompts"] or settings["cleanup_unanswered_prompts"])
                or not binding or binding.get("bot_key") != self.bot_key):
            return
        now = self.clock()
        counts = {"removed": 0, "failed": 0, "skipped": 0}
        attempted = 0
        for row in self.storage.connection.execute(
                "SELECT id FROM pending_decision WHERE decision_type='viewing_followup' AND audience='shared_household' "
                "AND status IN ('open','resolved') ORDER BY id").fetchall():
            settings = self.settings.get()
            decision = self.storage.get_pending_decision(row[0])
            context = decision.context
            delivery = context.get("delivery", {})
            if (context.get("workflow") != "viewing_v1" or context.get("source") != binding["source"]
                    or delivery.get("state") != "sent" or delivery.get("chat_id") != binding["chat_id"]
                    or type(delivery.get("message_id")) is not int
                    or delivery.get("cleanup_status") in {"too_old", "unknown_age"}
                    or not settings["cleanup_answered_prompts" if decision.status == "resolved" or context.get("declined_all") else "cleanup_unanswered_prompts"]
                    or delivery.get("cleanup_next_check", 0) > now):
                continue
            sent_at = delivery.get("sent_at")
            if sent_at is None:
                # Older confirmed deliveries have a dated, event-specific audit.
                # An uncertain send/recovered callback is not proof of send time.
                for audit in self.storage.connection.execute(
                        "SELECT created_at,details_json FROM audit_log WHERE action='viewing_prompt_delivered' "
                        "AND media_key=? ORDER BY id", (decision.media_key,)):
                    if json.loads(audit["details_json"]).get("decision_id") == decision.decision_id:
                        sent_at = datetime.fromisoformat(audit["created_at"]).replace(tzinfo=timezone.utc).timestamp()
                        break
            if type(sent_at) not in (int, float) or sent_at <= 0 or sent_at > now:
                reason = "unknown_age"
            elif now - sent_at < 129600:
                continue
            elif now - sent_at >= 172800:
                reason = "too_old"
            else:
                reason = None
            try:
                if reason:
                    uncertain = reason == "too_old" and "cleanup_next_check" in delivery
                    self.store.update_delivery(decision, {**delivery, "cleanup_status": reason,
                                               **({"state": "uncertain_removal"} if uncertain else {})},
                                               actor="system", action="telegram_cleanup_skipped")
                    counts["skipped"] += 1
                    continue
                # Claim the exact message before deletion; retries can only delete
                # that same ID, and missing-message responses count as success.
                claimed_delivery = {**delivery, "sent_at": sent_at, "cleanup_next_check": now + 300}
                claimed = self.store.update_delivery(decision, claimed_delivery, actor="system", action="telegram_cleanup_attempted")
                attempted += 1
                result = self.client.call("deleteMessage", {"chat_id": binding["chat_id"], "message_id": delivery["message_id"]})
                if result is not True:
                    raise AdapterError("Telegram removal was not confirmed.")
                # A reply may have changed the question while the API request ran.
                # Preserve its fresh answers and update only the same message's state.
                fresh = self.storage.get_pending_decision(claimed.decision_id)
                current = fresh.context.get("delivery", {})
                if current.get("message_id") == delivery["message_id"]:
                    self.store.update_delivery(fresh, {**current, "state": "removed", "message_id": None,
                        "removed_message_id": delivery["message_id"], "removed_at": now, "cleanup_status": "removed"},
                        actor="system", action="telegram_prompt_removed")
                counts["removed"] += 1
            except (AdapterError, StorageError):
                counts["failed"] += 1
            if attempted >= 5:
                break
        if any(counts.values()):
            with self.storage.connection:
                self.storage.connection.execute(
                    "INSERT INTO job_run(job_name,status,cursor_json,finished_at) VALUES('telegram_prompt_cleanup',?,?,CURRENT_TIMESTAMP)",
                    ("partial" if counts["failed"] else "completed", json.dumps(counts)))
