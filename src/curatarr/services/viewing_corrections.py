"""Explicit admin viewing corrections with persisted intent and exact read-back."""

from copy import deepcopy
import hmac
import secrets
import time

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.viewing_reset import RyotViewingReset, fingerprint, normalized
from curatarr.integrations.ryot.viewing import ViewingEventMissing
from curatarr.services.viewing_workflow import ViewingResponseError
from curatarr.storage import StorageError
from curatarr.storage.viewing import encoded


class ViewingCorrections:
    def __init__(self, workflow, *, clock=time.time):
        self.workflow, self.storage, self.clock = workflow, workflow.storage, clock
        self.adapter = RyotViewingReset(workflow.adapter.reader)

    def operation(self, media_key):
        return self.workflow.store.reset_operation(media_key)

    def actions(self, decision, state):
        if decision.context.get('direct_candidate'):
            binding = self.workflow.binding()
            enabled = bool(binding and binding.get('enabled') and state == 'valid'
                           and decision.context.get('declined_all') and not decision.context.get('confirmed_by'))
            return [{'label': label, 'action': action, 'row': 0, 'enabled': enabled if normal else False,
                     'reason': '' if normal and enabled else 'Native corrections are maintained through the repository.'}
                    for label, action, normal in [('Confirm household watched', 'confirm_declined', True),
                        ('Review again', 'review_again', True), ('Clear viewing history', 'clear_history', False),
                        ('Clear stale request', 'clear_stale', False)]]
        operation = self.operation(decision.media_key)
        pending = operation and operation["state"] != "completed"
        running = pending and operation.get("lease_until", 0) > self.clock() and operation["state"] == "running"
        rating_pending = bool(decision.context.get("operation"))
        any_rating_pending = any(d.context.get("operation") for d in self.storage.viewing_followups()
                                 if d.media_key == decision.media_key)
        reason = "A history reset is pending." if pending else "A rating operation needs investigation." if rating_pending else ""
        snapshot, reset_reason = None, reason
        if any_rating_pending:
            reset_reason = "A rating operation for this movie needs investigation first."
        if state != "unavailable" and not any_rating_pending and not running:
            try:
                snapshot = self.adapter.snapshot(decision.context)
                if not pending:
                    reset_reason = self.adapter.blocked_reason(snapshot)
                    if not snapshot["history"] and not any(normalized(n) in {"completed", "inprogress"} for n in snapshot["collections"]):
                        reset_reason = "No viewing history or watched flags remain."
                else:
                    self.adapter.check_remaining(operation["snapshot"], snapshot)
                    reset_reason = ""
            except AdapterError:
                reset_reason = "Complete history and collection evidence could not be verified."
        if running:
            reset_reason = "A history reset is currently running."
        if state == "unavailable":
            reason = reset_reason = "Viewing evidence could not be verified. Refresh to retry."
        binding = self.workflow.binding()
        if not binding or not binding.get("enabled"):
            reason = reset_reason = "Viewing follow-ups are paused. Enable delivery before correcting this request."
        valid = state == "valid" and decision.context.get("declined_all")
        event_reason = ("The original viewing is no longer completed." if state == "changed" else
                        "The original viewing no longer exists in Ryot.")
        def action(label, key, enabled, explanation):
            return {"label": label, "action": key, "row": 0, "enabled": bool(enabled),
                    "reason": "" if enabled else explanation}
        return [
            action("Confirm household watched", "confirm_declined", valid and not reason,
                   reason or "The original viewing is not available for confirmation."),
            action("Review again", "review_again", valid and not reason,
                   reason or event_reason),
            action("Clear viewing history", "clear_history", snapshot is not None and not reset_reason,
                   reset_reason or "History could not be verified."),
            action("Clear stale request", "clear_stale", state == "missing" and not reason,
                   reason or "The original viewing still exists in Ryot.")]

    def decision(self, decision_id, media_key, version):
        decision = self.storage.get_pending_decision(decision_id)
        self.workflow._web_available(decision)
        if decision.media_key != media_key or self.workflow.version(decision) != version:
            raise ViewingResponseError("This request changed. Refresh before correcting it.")
        return decision

    def review_again(self, decision, *, user_id):
        try:
            self.workflow.adapter.validate(decision.context)
        except ViewingEventMissing:
            raise ViewingResponseError("The original viewing no longer exists in Ryot; it cannot be returned for review.") from None
        if (not decision.context.get("declined_all") or decision.context.get("operation")
                or decision.context.get('watch_operation') or self.workflow.store.reset_pending(decision.media_key)):
            raise ViewingResponseError("This viewing cannot be returned for review yet.")
        context = deepcopy(decision.context)
        for key in ("confirmed_by", "selected_rating", "existing_rating", "rating_declined", "dispute_ui_updated"):
            context.pop(key, None)
        context.update(responses={}, participants=list(self.workflow.participants or ()), declined_all=False,
                       disputed=False, confirmed=False, questions=["watched", "rating"], rating=None)
        if context["delivery"].get("state") in {"removed", "uncertain_removal"}:
            context["delivery"] = {"state": "pending", "reopened": True}
        else:
            context["delivery"]["web_refresh"] = True
        self.workflow.store.update(decision, context, actor=f"miniapp:user:{user_id}", action="viewing_review_restarted")
        self.workflow.refresh_web_prompts(decision.decision_id)

    def preview(self, decision):
        if decision.context.get('direct_candidate'):
            raise ViewingResponseError('Direct viewing corrections require maintainer review.')
        operation = self.operation(decision.media_key)
        pending = operation and operation["state"] != "completed"
        if pending and operation["state"] == "running" and operation.get("lease_until", 0) > self.clock():
            raise ViewingResponseError("Another history reset is running. Refresh shortly.")
        fresh = self.adapter.snapshot(decision.context)
        original = operation["snapshot"] if pending else fresh
        if not pending and self.adapter.blocked_reason(original):
            raise ViewingResponseError(self.adapter.blocked_reason(original))
        if not pending and not original["history"] and not any(normalized(n) in {"completed", "inprogress"} for n in original["collections"]):
            raise ViewingResponseError("No viewing history or watched flags remain. Use Clear stale request instead.")
        self.adapter.check_remaining(original, fresh)
        if any(d.context.get("operation") for d in self.storage.viewing_followups()
               if d.media_key == decision.media_key):
            raise ViewingResponseError("A rating operation needs investigation before clearing history.")
        token = hmac.digest(self.workflow.client.token.encode(),
            encoded([decision.decision_id, self.workflow.version(decision), fingerprint(fresh),
                     operation["run_id"] if pending else None]).encode(), "sha256").hex()
        return {"title": fresh["title"], "record_count": len(original["history"]),
                "remaining_count": len(fresh["history"]), "resume": bool(pending), "token": token,
                "snapshot": original, "prior_run": operation["run_id"] if pending else None}

    def clear(self, decision, *, token, user_id):
        preview = self.preview(decision)
        if not hmac.compare_digest(preview["token"], token or ""):
            raise ViewingResponseError("Viewing evidence changed. Open the confirmation again.")
        claim = secrets.token_hex(16)
        connection = self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.storage.get_pending_decision(decision.decision_id)
            if not current or self.workflow.version(current) != self.workflow.version(decision):
                raise StorageError("The viewing question changed.")
            old = self.operation(decision.media_key)
            pending = old and old["state"] != "completed"
            if ((pending and old["run_id"] != preview["prior_run"]) or
                    (pending and old["state"] == "running" and old.get("lease_until", 0) > self.clock())):
                raise StorageError("Another reset owns this movie.")
            if any(d.context.get("operation") for d in self.storage.viewing_followups() if d.media_key == decision.media_key):
                raise StorageError("A rating operation is pending.")
            operation = deepcopy(old) if pending else {"run_id": secrets.token_hex(16), "snapshot": preview["snapshot"], "issued_flags": []}
            operation.update(state="running", claim=claim, lease_until=self.clock() + 300, actor=f"miniapp:user:{user_id}")
            connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES(?,'viewing_reset','',?) "
                "ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP",
                (decision.media_key, encoded(operation)))
            connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,'viewing_history_reset_requested',?,?)",
                               (operation["actor"], decision.media_key, encoded(operation)))
        try:
            original = operation["snapshot"]
            while True:
                fresh = self.adapter.snapshot(decision.context)
                self.adapter.check_remaining(original, fresh)
                if not fresh["history"]:
                    break
                event_id = fresh["history"][0]["id"]
                operation["pending_event"] = event_id
                self.save(decision.media_key, operation)
                self.adapter.delete(event_id)
                # Exact absence is checked on the next loop before doing anything else.
                after = self.adapter.snapshot(decision.context)
                self.adapter.check_remaining(original, after)
                if any(e["id"] == event_id for e in after["history"]):
                    raise AdapterError("Viewing deletion awaits read-back.")
            keys = ["completed", "inprogress"]
            if not any(normalized(n) == "monitoring" for n in original["collections"]):
                keys.append("monitoring")
            for key in keys:
                if any(normalized(n) == key for n in fresh["collections"]):
                    if key not in operation["issued_flags"]:
                        operation["issued_flags"].append(key)
                        self.save(decision.media_key, operation)  # Never blindly resubmit async jobs.
                        self.adapter.remove_flag(original, key)
            fresh = self.adapter.snapshot(decision.context)
            self.adapter.check_remaining(original, fresh)
            if fresh["history"] or any(normalized(n) in keys for n in fresh["collections"]):
                raise AdapterError("Native history flags await read-back.")
            self.finish(decision.media_key, operation)
            self.workflow.refresh_web_prompts()
            return True
        except (AdapterError, StorageError):
            operation.update(state="pending", lease_until=0)
            self.save(decision.media_key, operation)
            return False

    def save(self, media_key, operation):
        operation["lease_until"] = self.clock() + 300 if operation["state"] == "running" else 0
        with self.storage.connection:
            current = self.operation(media_key)
            if not current or current.get("claim") != operation["claim"]:
                raise StorageError("The reset claim changed.")
            self.storage.connection.execute("UPDATE state_snapshot SET payload_json=?,updated_at=CURRENT_TIMESTAMP "
                                            "WHERE media_key=? AND source='viewing_reset'", (encoded(operation), media_key))

    def finish(self, media_key, operation):
        connection = self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.operation(media_key)
            if not current or current.get("claim") != operation["claim"]:
                raise StorageError("The reset claim changed.")
            for decision in self.storage.viewing_followups():
                if decision.media_key != media_key or decision.context.get("source") != operation["snapshot"]["source"]:
                    continue
                context = deepcopy(decision.context)
                context.update(questions=[], closed_reason="history_cleared", reset_run_id=operation["run_id"])
                context.setdefault("delivery", {})["web_refresh"] = True
                connection.execute("UPDATE pending_decision SET context_json=?,status='resolved' WHERE id=? AND status='open'",
                                   (encoded(context), decision.decision_id))
            for event in operation["snapshot"]["history"]:
                connection.execute("INSERT OR IGNORE INTO viewing_event(source,event_id,media_key,payload_json,decision_id) VALUES(?,?,?,?,NULL)",
                                   (operation["snapshot"]["source"], event["id"], media_key, encoded(event)))
            operation.update(state="completed", lease_until=0)
            connection.execute("UPDATE state_snapshot SET payload_json=?,updated_at=CURRENT_TIMESTAMP WHERE media_key=? AND source='viewing_reset'",
                               (encoded(operation), media_key))
            connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,'viewing_history_reset_verified',?,?)",
                               (operation["actor"], media_key, encoded({"run_id": operation["run_id"], "removed": len(operation["snapshot"]["history"])})))
