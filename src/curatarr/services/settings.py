"""Installation display preferences and one explicit movie-only automation policy."""

import json
import unicodedata

from curatarr.storage import StorageError


class InstallationSettings:
    def __init__(self, storage):
        self.storage = storage

    def get(self):
        row = self.storage.connection.execute(
            "SELECT payload_json FROM state_snapshot WHERE media_key='installation' AND source='settings'").fetchone()
        defaults = {"display_name": "Household", "owned_cleanup_enabled": False,
                    "owned_cleanup_generation": 0, "cleanup_answered_prompts": False,
                    "cleanup_unanswered_prompts": False, "revision": 0}
        return {**defaults, **json.loads(row[0])} if row else defaults

    def display_name(self):
        return self.get()["display_name"] or "Household"

    def save(self, *, expected_revision, actor, display_name=None, owned_cleanup_enabled=None,
             cleanup_answered_prompts=None, cleanup_unanswered_prompts=None):
        if display_name is not None:
            if not isinstance(display_name, str) or any(unicodedata.category(c).startswith("C") for c in display_name):
                raise ValueError("Enter a name without control characters.")
            display_name = display_name.strip()
            if not 1 <= len(display_name) <= 60:
                raise ValueError("Enter a name from 1 to 60 characters.")
        if owned_cleanup_enabled is not None and type(owned_cleanup_enabled) is not bool:
            raise ValueError("Cleanup must be on or off.")
        if any(value is not None and type(value) is not bool for value in (cleanup_answered_prompts, cleanup_unanswered_prompts)):
            raise ValueError("Telegram cleanup options must be on or off.")
        connection = self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.get()
            if current["revision"] != expected_revision:
                raise StorageError("Settings changed. Refresh before saving.")
            updated = {**current, "revision": current["revision"] + 1}
            if display_name is not None:
                updated["display_name"] = display_name
            if owned_cleanup_enabled is not None:
                updated["owned_cleanup_enabled"] = owned_cleanup_enabled
                if owned_cleanup_enabled and not current["owned_cleanup_enabled"]:
                    updated["owned_cleanup_generation"] = current["owned_cleanup_generation"] + 1
            if cleanup_answered_prompts is not None:
                updated["cleanup_answered_prompts"] = cleanup_answered_prompts
            if cleanup_unanswered_prompts is not None:
                updated["cleanup_unanswered_prompts"] = cleanup_unanswered_prompts
            connection.execute(
                "INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('installation','settings','',?) "
                "ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP",
                (json.dumps(updated, sort_keys=True),))
            connection.execute("INSERT INTO audit_log(actor,action,details_json) VALUES(?,'installation_settings_changed',?)",
                               (actor, json.dumps({"before": current, "after": updated}, sort_keys=True)))
        return updated

    def public(self):
        result = self.get()
        row = self.storage.connection.execute(
            "SELECT status,started_at,finished_at,cursor_json FROM job_run "
            "WHERE job_name='owned_cleanup' ORDER BY id DESC LIMIT 1").fetchone()
        counts = json.loads(row["cursor_json"]) if row else {}
        result["owned_cleanup_last_run"] = (None if row is None else {
            "status": row["status"], "at": row["finished_at"] or row["started_at"],
            "removed": counts.get("removed", 0), "total": counts.get("total", 0)})
        row = self.storage.connection.execute("SELECT status,finished_at,cursor_json FROM job_run "
            "WHERE job_name='telegram_prompt_cleanup' ORDER BY id DESC LIMIT 1").fetchone()
        result["telegram_cleanup_last_run"] = (None if row is None else {
            "status": row["status"], "at": row["finished_at"], **json.loads(row["cursor_json"])})
        return result
