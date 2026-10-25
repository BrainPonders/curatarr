"""SQLite persistence for Curatarr workflow state."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from curatarr.domain import MediaIdentity


SCHEMA_VERSION = 5


class StorageError(RuntimeError):
    """Raised when Curatarr storage cannot be initialized or queried."""


@dataclass(frozen=True)
class IdentityMapping:
    """Positive or negative confirmed mapping between two typed identities."""

    left: MediaIdentity
    right: MediaIdentity
    is_same: bool
    source: str


@dataclass(frozen=True)
class PendingDecision:
    """Persisted user/admin question waiting for revalidation and resolution."""

    decision_id: int
    media_key: str
    decision_type: str
    audience: str
    state_fingerprint: str
    status: str
    context: dict[str, Any]
    expires_at: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    """Traceable record of an automated or human action."""

    entry_id: int
    actor: str
    action: str
    media_key: str | None
    details: dict[str, Any]


class Storage:
    """Small SQLite wrapper for Curatarr workflow persistence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def get_cursor(self, name: str) -> int:
        row = self.connection.execute("SELECT value FROM runtime_cursor WHERE name = ?", (name,)).fetchone()
        return int(row["value"]) if row is not None else 0

    def start_reconciliation(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO job_run(job_name, status) VALUES ('movie_reconciliation', 'running')")
        return int(cursor.lastrowid)

    def maintenance_progress(self, user_id: int, *, source="maintenance_progress") -> dict:
        if source not in {"maintenance_progress", "viewing_progress"}:
            raise ValueError("Unknown progress source.")
        row = self.connection.execute(
            "SELECT payload_json FROM state_snapshot WHERE media_key = ? AND source = ?",
            (f"telegram:{user_id}", source)).fetchone()
        return json.loads(row["payload_json"]) if row else {"cursor": None, "skipped": [], "category": "all", "parked": False}

    def save_maintenance_progress(self, user_id: int, progress: dict, *, source="maintenance_progress") -> None:
        if source not in {"maintenance_progress", "viewing_progress"}:
            raise ValueError("Unknown progress source.")
        with self.connection:
            self.connection.execute(
                "INSERT INTO state_snapshot(media_key, source, fingerprint, payload_json) "
                "VALUES (?, ?, '', ?) ON CONFLICT(media_key, source) "
                "DO UPDATE SET payload_json = excluded.payload_json, updated_at = CURRENT_TIMESTAMP",
                (f"telegram:{user_id}", source, json.dumps(progress, sort_keys=True)))

    def viewing_followups(self) -> tuple[PendingDecision, ...]:
        """Only genuine open household follow-ups, never search or sync decisions."""
        rows = self.connection.execute(
            "SELECT * FROM pending_decision WHERE decision_type = 'viewing_followup' "
            "AND audience = 'shared_household' AND status = 'open' ORDER BY id").fetchall()
        return tuple(_pending_decision_from_row(row) for row in rows)

    def refresh_movie_findings(self, media_key: str, expected: list[dict], replacements: list[dict]) -> None:
        """Compare-and-swap one freshly checked movie, without touching other titles."""
        with self.connection:
            self.connection.execute("UPDATE pending_decision SET status = status WHERE media_key = ?", (media_key,))
            rows = self.connection.execute(
                "SELECT * FROM pending_decision WHERE media_key = ? AND audience = 'admin' AND status = 'open' "
                "AND substr(decision_type, 1, 10) = 'reconcile:'", (media_key,)).fetchall()
            current = {row["id"]: json.loads(row["context_json"]) for row in rows}
            if current != {item["decision_id"]: item["context"] for item in expected}:
                raise StorageError("Maintenance findings changed; refresh before continuing.")
            by_kind = {row["decision_type"][10:]: row for row in rows}
            retained = set()
            for item in replacements:
                if item["media_key"] != media_key:
                    raise StorageError("Maintenance evidence belongs to a different movie.")
                fingerprint = hashlib.sha256(json.dumps(item["context"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                previous = by_kind.get(item["kind"])
                if previous and previous["state_fingerprint"] == fingerprint:
                    retained.add(previous["id"])
                    continue
                if previous:
                    self.connection.execute("UPDATE pending_decision SET status = 'superseded' WHERE id = ?", (previous["id"],))
                inserted = self.connection.execute(
                    "INSERT INTO pending_decision(media_key, decision_type, audience, state_fingerprint, context_json) "
                    "VALUES (?, ?, 'admin', ?, ?)", (media_key, "reconcile:" + item["kind"], fingerprint,
                                                    json.dumps(item["context"], sort_keys=True)))
                retained.add(inserted.lastrowid)
            for row in rows:
                if row["id"] not in retained:
                    self.connection.execute("UPDATE pending_decision SET status = 'resolved' WHERE id = ? AND status = 'open'", (row["id"],))

    def publish_reconciliation(self, run_id: int, findings: list[dict], errors: list[str], counts: dict) -> dict:
        """Publish one snapshot atomically; failed/older scans never clear pending findings."""
        with self.connection:
            # Take the write lock before comparing run IDs or modifying the shared queue.
            cursor = self.connection.execute("UPDATE job_run SET cursor_json = ? WHERE id = ? AND status = 'running' "
                                             "AND job_name = 'movie_reconciliation'",
                                             (json.dumps(counts, sort_keys=True), run_id))
            if cursor.rowcount != 1:
                raise StorageError("Reconciliation run is not open.")
            latest = self.connection.execute(
                "SELECT MAX(id) FROM job_run WHERE job_name = 'movie_reconciliation'").fetchone()[0]
            status = "superseded" if latest != run_id else "failed" if errors else "completed"
            if status == "completed":
                open_rows = self.connection.execute(
                    "SELECT * FROM pending_decision WHERE status = 'open' AND audience = 'admin' "
                    "AND substr(decision_type, 1, 10) = 'reconcile:'").fetchall()
                existing = {(row["media_key"], row["decision_type"]): row for row in open_rows}
                retained = set()
                for finding in findings:
                    decision_type = "reconcile:" + finding["kind"]
                    context = finding["context"]
                    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    previous = existing.get((finding["media_key"], decision_type))
                    if previous is not None and previous["state_fingerprint"] == fingerprint:
                        retained.add(previous["id"])
                        continue
                    if previous is not None:
                        self.connection.execute("UPDATE pending_decision SET status = 'superseded' WHERE id = ?", (previous["id"],))
                    inserted = self.connection.execute(
                        "INSERT INTO pending_decision(media_key, decision_type, audience, state_fingerprint, context_json) "
                        "VALUES (?, ?, 'admin', ?, ?)",
                        (finding["media_key"], decision_type, fingerprint, json.dumps(context, sort_keys=True)))
                    retained.add(inserted.lastrowid)
                for row in open_rows:
                    if row["id"] not in retained:
                        self.connection.execute("UPDATE pending_decision SET status = 'resolved' WHERE id = ? AND status = 'open'",
                                                 (row["id"],))
            if status != "superseded":
                for provider in ("ryot", "radarr"):
                    key = "provider:" + provider
                    issue = self.connection.execute(
                        "SELECT id FROM operational_issue WHERE issue_type = 'reconciliation_provider_unavailable' "
                        "AND media_key = ? AND status = 'open'", (key,)).fetchone()
                    if provider in errors:
                        context = json.dumps({"provider": provider, "run_id": run_id,
                                              "message": "Snapshot unavailable or incomplete; earlier findings were not revalidated."})
                        if issue is None:
                            self.connection.execute(
                                "INSERT INTO operational_issue(issue_type, severity, media_key, context_json) "
                                "VALUES ('reconciliation_provider_unavailable', 'error', ?, ?)", (key, context))
                        else:
                            self.connection.execute("UPDATE operational_issue SET context_json = ? WHERE id = ?", (context, issue["id"]))
                    elif issue is not None:
                        self.connection.execute("UPDATE operational_issue SET status = 'resolved' WHERE id = ?", (issue["id"],))
            self.connection.execute("UPDATE job_run SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?", (status, run_id))
            return self.reconciliation_report()

    def reconciliation_report(self) -> dict:
        """Stored reports are historical; callers must run a fresh scan before review."""
        if self.connection.in_transaction:
            return self._reconciliation_report()
        with self.connection:
            self.connection.execute("BEGIN")
            return self._reconciliation_report()

    def _reconciliation_report(self) -> dict:
        run = self.connection.execute(
            "SELECT * FROM job_run WHERE job_name = 'movie_reconciliation' ORDER BY id DESC LIMIT 1").fetchone()
        validated = self.connection.execute(
            "SELECT id, finished_at FROM job_run WHERE job_name = 'movie_reconciliation' "
            "AND status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
        findings = []
        for row in self.connection.execute(
                "SELECT * FROM pending_decision WHERE audience = 'admin' AND status = 'open' "
                "AND substr(decision_type, 1, 10) = 'reconcile:' ORDER BY media_key, decision_type"):
            findings.append({"decision_id": row["id"], "media_key": row["media_key"],
                             "kind": row["decision_type"][10:], "context": json.loads(row["context_json"])})
        issues = [json.loads(row["context_json"]) for row in self.connection.execute(
            "SELECT context_json FROM operational_issue WHERE issue_type = 'reconciliation_provider_unavailable' "
            "AND status = 'open' ORDER BY media_key")]
        return {"run_id": run["id"] if run else None, "status": run["status"] if run else "not_run",
                "observed_at": run["finished_at"] if run else None,
                "counts": json.loads(run["cursor_json"]) if run else {},
                "findings": findings, "finding_count": len(findings),
                "movies_requiring_attention": len({finding["media_key"] for finding in findings}),
                "technical_issues": issues, "read_only": True,
                "last_validated_at": validated["finished_at"] if validated else None,
                "last_validated_run_id": validated["id"] if validated else None,
                "revalidated": bool(run and run["status"] == "completed")}

    def set_cursor(self, name: str, value: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_cursor(name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value),
            )

    def initialize(self) -> None:
        # executescript implicitly commits: execute complete statements inside an
        # explicit transaction so a failed additive migration cannot publish a newer version.
        from curatarr.storage.journal import JOURNAL_SCHEMA
        if self.connection.in_transaction:
            raise StorageError("Finish the current transaction before initializing storage.")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
                existing = self.connection.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
                if existing is None or type(existing[0]) is not int or existing[0] < 1:
                    raise StorageError("Workflow database has an invalid schema version; refusing to migrate.")
                if existing[0] > SCHEMA_VERSION:
                    raise StorageError("Workflow database is newer than this runtime; refusing to downgrade.")
            statement = ""
            for line in (SCHEMA_SQL + JOURNAL_SCHEMA).splitlines():
                statement += line + "\n"
                if sqlite3.complete_statement(statement):
                    self.connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise StorageError("Incomplete workflow schema migration.")
            self.connection.execute(
                "INSERT INTO schema_version(id,version) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version", (SCHEMA_VERSION,))

    def schema_version(self) -> int:
        try:
            row = self.connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise StorageError("Storage schema is not initialized.") from exc
        if row is None:
            raise StorageError("Storage schema version is missing.")
        return int(row["version"])

    def upsert_media_identity(self, identity: MediaIdentity, *, media_type: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO media_identity(namespace, value, media_type)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace, value) DO UPDATE SET media_type = excluded.media_type
                """,
                (identity.namespace, identity.value, media_type),
            )

    def add_identity_mapping(
        self,
        left: MediaIdentity,
        right: MediaIdentity,
        *,
        is_same: bool,
        source: str,
    ) -> IdentityMapping:
        self.upsert_media_identity(left)
        self.upsert_media_identity(right)
        left_text = str(left)
        right_text = str(right)
        if right_text < left_text:
            left, right = right, left
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO identity_mapping(
                    left_namespace,
                    left_value,
                    right_namespace,
                    right_value,
                    is_same,
                    source
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(left_namespace, left_value, right_namespace, right_value)
                DO UPDATE SET is_same = excluded.is_same, source = excluded.source
                """,
                (left.namespace, left.value, right.namespace, right.value, int(is_same), source),
            )
        return IdentityMapping(left=left, right=right, is_same=is_same, source=source)

    def get_identity_mapping(
        self,
        left: MediaIdentity,
        right: MediaIdentity,
    ) -> IdentityMapping | None:
        left_text = str(left)
        right_text = str(right)
        if right_text < left_text:
            left, right = right, left
        row = self.connection.execute(
            """
            SELECT *
            FROM identity_mapping
            WHERE left_namespace = ?
              AND left_value = ?
              AND right_namespace = ?
              AND right_value = ?
            """,
            (left.namespace, left.value, right.namespace, right.value),
        ).fetchone()
        if row is None:
            return None
        return IdentityMapping(
            left=MediaIdentity(row["left_namespace"], row["left_value"]),
            right=MediaIdentity(row["right_namespace"], row["right_value"]),
            is_same=bool(row["is_same"]),
            source=row["source"],
        )

    def create_pending_decision(
        self,
        *,
        media_key: str,
        decision_type: str,
        audience: str,
        state_fingerprint: str,
        context: dict[str, Any] | None = None,
        lifetime_seconds: int = 86400,
    ) -> PendingDecision:
        if lifetime_seconds <= 0:
            raise ValueError("Pending decision lifetime must be positive.")
        context_json = json.dumps(context or {}, sort_keys=True)
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO pending_decision(
                    media_key,
                    decision_type,
                    audience,
                    state_fingerprint,
                    context_json,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, datetime('now', ?))
                """,
                (media_key, decision_type, audience, state_fingerprint, context_json,
                 f"+{lifetime_seconds} seconds"),
            )
        decision_id = int(cursor.lastrowid)
        decision = self.get_pending_decision(decision_id)
        assert decision is not None
        return decision

    def get_pending_decision(self, decision_id: int) -> PendingDecision | None:
        row = self.connection.execute(
            "SELECT * FROM pending_decision WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return _pending_decision_from_row(row)

    def close_open_decision(self, decision_id: int, status: str) -> bool:
        """Close an open decision once without reopening terminal decisions."""

        if status not in {"resolved", "superseded", "expired"}:
            raise ValueError("A terminal decision status is required.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE pending_decision SET status = ? WHERE id = ? AND status = 'open'",
                (status, decision_id),
            )
        return cursor.rowcount == 1

    def update_pending_decision_status(self, decision_id: int, status: str) -> PendingDecision:
        normalized_status = status.strip().lower()
        if normalized_status not in {"open", "resolved", "superseded", "expired"}:
            raise ValueError(f"Unsupported pending decision status {status!r}.")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE pending_decision
                SET status = ?
                WHERE id = ?
                """,
                (normalized_status, decision_id),
            )
        if cursor.rowcount != 1:
            raise StorageError(f"Pending decision {decision_id} does not exist.")
        decision = self.get_pending_decision(decision_id)
        assert decision is not None
        return decision

    def append_audit_log(
        self,
        *,
        actor: str,
        action: str,
        media_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        details_json = json.dumps(details or {}, sort_keys=True)
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO audit_log(actor, action, media_key, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (actor, action, media_key, details_json),
            )
        entry_id = int(cursor.lastrowid)
        row = self.connection.execute("SELECT * FROM audit_log WHERE id = ?", (entry_id,)).fetchone()
        assert row is not None
        return _audit_entry_from_row(row)

    def record_confirmed_file_deletion(self, identity: MediaIdentity, *, actor: str, source: str) -> AuditEntry:
        """Record verified completion, not an intent, missing-file observation, or UI action."""
        if identity.namespace not in {"tmdb", "imdb"} or source not in {"curatarr", "admin_verified"} or not actor.strip():
            raise ValueError("A stable movie identity, actor, and verified deletion source are required.")
        return self.append_audit_log(actor=actor, action="media_file_deleted", media_key=str(identity),
                                     details={"verified": True, "outcome": "success", "source": source})

    def has_confirmed_file_deletion(self, identities) -> bool:
        keys = sorted({str(i) for i in identities if i.namespace in {"tmdb", "imdb"}})
        if not keys:
            return False
        placeholders = ",".join("?" for _ in keys)
        try:
            rows = self.connection.execute(
                f"SELECT details_json FROM audit_log WHERE action = 'media_file_deleted' AND media_key IN ({placeholders})",
                keys,
            )
            for row in rows:
                try:
                    details = json.loads(row["details_json"])
                except (ValueError, TypeError):
                    continue
                if (isinstance(details, dict) and details.get("verified") is True
                        and details.get("outcome") == "success" and details.get("source") in {"curatarr", "admin_verified"}):
                    return True
        except sqlite3.Error as exc:
            raise StorageError("Deletion evidence could not be read.") from exc
        return False


def _pending_decision_from_row(row: sqlite3.Row) -> PendingDecision:
    return PendingDecision(
        decision_id=int(row["id"]),
        media_key=row["media_key"],
        decision_type=row["decision_type"],
        audience=row["audience"],
        state_fingerprint=row["state_fingerprint"],
        status=row["status"],
        context=json.loads(row["context_json"]),
        expires_at=row["expires_at"],
    )


def _audit_entry_from_row(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        entry_id=int(row["id"]),
        actor=row["actor"],
        action=row["action"],
        media_key=row["media_key"],
        details=json.loads(row["details_json"]),
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_cursor (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1) DEFAULT 1,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media_identity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    media_type TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(namespace, value)
);

CREATE TABLE IF NOT EXISTS identity_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_namespace TEXT NOT NULL,
    left_value TEXT NOT NULL,
    right_namespace TEXT NOT NULL,
    right_value TEXT NOT NULL,
    is_same INTEGER NOT NULL CHECK (is_same IN (0, 1)),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(left_namespace, left_value, right_namespace, right_value)
);

CREATE TABLE IF NOT EXISTS state_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key TEXT NOT NULL,
    source TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(media_key, source)
);

CREATE TABLE IF NOT EXISTS pending_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    audience TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS approval_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_decision_id INTEGER,
    media_key TEXT NOT NULL,
    action TEXT NOT NULL,
    approver_group TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    FOREIGN KEY(pending_decision_id) REFERENCES pending_decision(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS reconciliation_open_finding
ON pending_decision(media_key, decision_type, audience)
WHERE status = 'open' AND substr(decision_type, 1, 10) = 'reconcile:';

CREATE TABLE IF NOT EXISTS operational_issue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    media_key TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS job_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    media_key TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS viewing_event (
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    media_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    decision_id INTEGER REFERENCES pending_decision(id),
    PRIMARY KEY(source, event_id)
);
"""
