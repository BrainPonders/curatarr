"""Atomic event baseline, question creation and optimistic shared answer updates."""

import hashlib
import json
from datetime import datetime, timezone

from curatarr.storage import Storage, StorageError


def encoded(value):
    return json.dumps(value, sort_keys=True)


class ViewingStore:
    def __init__(self, storage: Storage):
        self.storage = storage

    def binding(self):
        row = self.storage.connection.execute(
            "SELECT payload_json FROM state_snapshot WHERE media_key='shared_household' AND source='viewing_binding'").fetchone()
        return json.loads(row[0]) if row else None

    def reset_operation(self, media_key):
        row = self.storage.connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key=? AND source='viewing_reset'",
                                              (media_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def reset_pending(self, media_key):
        operation = self.reset_operation(media_key)
        return bool(operation and operation.get("state") != "completed")

    def bind(self, value):
        with self.storage.connection:
            self.storage.connection.execute(
                "INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('shared_household','viewing_binding','',?) "
                "ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP",
                (encoded(value),))

    def observe(self, account, events, *, participants=None):
        """Publish only complete snapshots. Baseline and new questions commit together."""
        source = account["source"]
        connection = self.storage.connection
        created = []
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            baseline = connection.execute("SELECT 1 FROM state_snapshot WHERE media_key=? AND source='viewing_baseline'", (source,)).fetchone()
            for event in events:
                old = connection.execute("SELECT media_key FROM viewing_event WHERE source=? AND event_id=?",
                                         (source, event["event_id"])).fetchone()
                if old:
                    if old[0] != event["media_key"]:
                        raise StorageError("A viewing event changed movie identity.")
                    continue
                # A direct completion is linked to its original question only after exact
                # native readback. Defer observation while that association is pending.
                identities = set(event.get('identities', []))
                pending_direct = connection.execute(
                    "SELECT media_key FROM pending_decision WHERE status='open' "
                    "AND json_extract(context_json,'$.source')=? "
                    "AND json_extract(context_json,'$.direct_candidate') IS NOT NULL "
                    "AND json_extract(context_json,'$.confirmed_by') IS NOT NULL "
                    "AND coalesce(json_extract(context_json,'$.native_linked'),0)=0", (source,)).fetchall()
                if any(r['media_key'] in identities for r in pending_direct):
                    continue
                # Catalogue registrations are already explicit household confirmations.
                # Defer native observation until their exact event is committed to this ledger.
                manual = connection.execute(
                    "SELECT media_key FROM coordination_operation WHERE status IN ('pending','running','uncertain','blocked') "
                    "AND json_extract(expected_json,'$.manual_watch.source')=?", (source,)).fetchall()
                if any(r['media_key'] in identities for r in manual):
                    continue
                decision_id = None
                if baseline:
                    context = {**event, "source": source, "account_id": account["id"], "workflow": "viewing_v1",
                               "questions": ["watched", "rating"], "confirmed": False, "rating": None,
                               "delivery": {"state": "pending"}, "operation": None, "rating_scale": 5}
                    if participants is not None:
                        context.update(participants=sorted(set(participants)), responses={})
                    cursor = connection.execute(
                        "INSERT INTO pending_decision(media_key,decision_type,audience,state_fingerprint,context_json,expires_at) "
                        "VALUES(?,'viewing_followup','shared_household',?,?,NULL)",
                        (event["media_key"], hashlib.sha256((source + event["event_id"]).encode()).hexdigest(), encoded(context)))
                    decision_id = int(cursor.lastrowid)
                    created.append(decision_id)
                connection.execute("INSERT INTO viewing_event(source,event_id,media_key,payload_json,decision_id) VALUES(?,?,?,?,?)",
                                   (source, event["event_id"], event["media_key"], encoded(event), decision_id))
            connection.execute(
                "INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES(?,'viewing_baseline','',?) "
                "ON CONFLICT(media_key,source) DO UPDATE SET updated_at=CURRENT_TIMESTAMP",
                (source, encoded({"account_id": account["id"], "baseline_at": datetime.now(timezone.utc).isoformat()})))
        return {"baseline_created": not bool(baseline), "completed_events": len(events), "created": created}

    def update(self, decision, context, *, actor, action, resolve=False):
        with self.storage.connection:
            self.storage.connection.execute("BEGIN IMMEDIATE")
            if self.reset_pending(decision.media_key):
                raise StorageError("A viewing history reset is pending for this movie.")
            cursor = self.storage.connection.execute(
                "UPDATE pending_decision SET context_json=?,status=? WHERE id=? AND context_json=? "
                "AND status='open' AND decision_type='viewing_followup' AND audience='shared_household'",
                (encoded(context), "resolved" if resolve else "open", decision.decision_id, encoded(decision.context)))
            if cursor.rowcount != 1:
                raise StorageError("Another household response changed this question. Refresh before retrying.")
            self.storage.connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)",
                                            (actor, action, decision.media_key,
                                             encoded({"decision_id": decision.decision_id, "event_id": context["event_id"],
                                                      "review_id": context.get("review_id"), "rating": context.get("rating"),
                                                      "confirmed_by": context.get("confirmed_by"),
                                                      "declined_all": context.get("declined_all", False),
                                                      "rated_by": context.get("rated_by"),
                                                      "previous_responses": (decision.context.get("responses")
                                                                             if action == "viewing_review_restarted" else None),
                                                      "requested_score": (context.get("operation") or {}).get("score")})))
        return self.storage.get_pending_decision(decision.decision_id)

    def update_delivery(self, decision, delivery, *, actor, action):
        """Message lifecycle changes never resolve or erase an unanswered question."""
        context = {**decision.context, "delivery": delivery}
        with self.storage.connection:
            cursor = self.storage.connection.execute(
                "UPDATE pending_decision SET context_json=? WHERE id=? AND context_json=? AND status=? "
                "AND decision_type='viewing_followup' AND audience='shared_household'",
                (encoded(context), decision.decision_id, encoded(decision.context), decision.status))
            if cursor.rowcount != 1:
                raise StorageError("The question changed. Recheck its message state.")
            self.storage.connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)",
                (actor, action, decision.media_key, encoded({"decision_id": decision.decision_id,
                 "message_id": delivery.get("message_id"), "cleanup_status": delivery.get("cleanup_status")})))
        return self.storage.get_pending_decision(decision.decision_id)
