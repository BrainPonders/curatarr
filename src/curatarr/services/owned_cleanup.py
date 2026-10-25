"""Daily opt-in movie inventory policy with durable claims and async verification."""

import json
import secrets
import time

from curatarr.adapters import AdapterError
from curatarr.services.settings import InstallationSettings


class OwnedCleanupAutomation:
    def __init__(self, storage, adapter, *, clock=time.time):
        self.storage, self.adapter, self.clock = storage, adapter, clock
        self.settings = InstallationSettings(storage)

    def tick(self):
        if not self.settings.get()["owned_cleanup_enabled"]:
            return
        now = self.clock()
        connection = self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            # Policy may have been switched off while this worker waited for the lock.
            policy = self.settings.get()
            if not policy["owned_cleanup_enabled"]:
                return
            row = connection.execute("SELECT * FROM job_run WHERE job_name='owned_cleanup' ORDER BY id DESC LIMIT 1").fetchone()
            previous = json.loads(row["cursor_json"]) if row else {}
            if row and row["status"] in {"running", "pending"}:
                if previous.get("next_check", 0) > now:
                    return
                operation = {**previous, "next_check": now + 300, "claim": secrets.token_hex(12)}
                connection.execute("UPDATE job_run SET cursor_json=? WHERE id=?", (json.dumps(operation), row["id"]))
                run_id = row["id"]
            else:
                if (row and previous.get("generation") == policy["owned_cleanup_generation"]
                        and previous.get("started_epoch", now) + 86400 > now):
                    return
                operation = {"started_epoch": now, "next_check": now + 300, "removed": 0, "total": 0,
                             "claim": secrets.token_hex(12), "generation": policy["owned_cleanup_generation"],
                             "scope": previous.get("scope") if previous.get("generation") == policy["owned_cleanup_generation"] else None}
                run_id = connection.execute("INSERT INTO job_run(job_name,status,cursor_json) VALUES('owned_cleanup','running',?)",
                                            (json.dumps(operation),)).lastrowid
        try:
            if "snapshot" in operation:
                return self.verify(run_id, operation)
            # Interrupted scans had not persisted a provider-write intent and are safe to repeat.
            snapshot = self.adapter.snapshot()
            scope = {key: snapshot[key] for key in ("account_id", "collection_id")}
            if operation.get("scope") is not None and operation["scope"] != scope:
                raise AdapterError("Cleanup account changed; policy must be explicitly re-enabled.")
            operation["scope"] = scope
            if not self.settings.get()["owned_cleanup_enabled"]:
                return self.finish(run_id, operation, "stopped")
            if not snapshot["movies"]:
                return self.finish(run_id, operation, "completed")
            operation.update(snapshot=snapshot, total=len(snapshot["movies"]))
            # A persisted pending state precedes the remote attempt. No resumed
            # worker can submit it twice after a crash or uncertain response.
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT cursor_json FROM job_run WHERE id=?", (run_id,)).fetchone()
                if json.loads(current[0]).get("claim") != operation["claim"]:
                    return  # A newer scan owns the run; this snapshot is stale.
                connection.execute("UPDATE job_run SET status='pending',cursor_json=? WHERE id=?",
                                   (json.dumps(operation), run_id))
                connection.execute("INSERT INTO audit_log(actor,action,details_json) VALUES('system','owned_cleanup_requested',?)",
                                   (json.dumps({"run_id": run_id, "snapshot": snapshot}),))
            if not self.settings.get()["owned_cleanup_enabled"]:
                return self.finish(run_id, operation, "stopped")
            self.adapter.remove(snapshot)
            self.verify(run_id, operation)
        except AdapterError:
            if "snapshot" in operation:
                self.save(run_id, operation, "pending")
            else:
                self.finish(run_id, operation, "failed")

    def verify(self, run_id, operation):
        fresh = self.adapter.snapshot()
        original = operation["snapshot"]
        if any(fresh[key] != original[key] for key in ("account_id", "collection_id")):
            raise AdapterError("Owned verification belongs to another account.")
        remaining = {m["id"] for m in original["movies"]}.intersection(fresh["owned_ids"])
        operation["removed"] = operation["total"] - len(remaining)
        if remaining:
            self.save(run_id, operation, "pending")
        else:
            self.finish(run_id, operation, "completed")

    def save(self, run_id, operation, status):
        with self.storage.connection:
            self.storage.connection.execute("BEGIN IMMEDIATE")
            row = self.storage.connection.execute("SELECT cursor_json FROM job_run WHERE id=?", (run_id,)).fetchone()
            if json.loads(row[0]).get("claim") != operation["claim"]:
                return
            self.storage.connection.execute("UPDATE job_run SET status=?,cursor_json=? WHERE id=?",
                                            (status, json.dumps(operation), run_id))

    def finish(self, run_id, operation, status):
        with self.storage.connection:
            self.storage.connection.execute("BEGIN IMMEDIATE")
            row = self.storage.connection.execute("SELECT cursor_json FROM job_run WHERE id=?", (run_id,)).fetchone()
            if json.loads(row[0]).get("claim") != operation["claim"]:
                return
            self.storage.connection.execute("UPDATE job_run SET status=?,cursor_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                                            (status, json.dumps(operation), run_id))
            self.storage.connection.execute("INSERT INTO audit_log(actor,action,details_json) VALUES('system','owned_cleanup_result',?)",
                                            (json.dumps({"run_id": run_id, "status": status, "removed": operation["removed"],
                                                         "total": operation["total"]}),))
