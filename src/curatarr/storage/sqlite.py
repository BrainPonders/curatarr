"""SQLite persistence for Curatarr workflow state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from curatarr.domain import MediaIdentity


SCHEMA_VERSION = 1


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

    def initialize(self) -> None:
        with self.connection:
            self.connection.executescript(SCHEMA_SQL)
            self.connection.execute(
                """
                INSERT INTO schema_version(version)
                VALUES (?)
                ON CONFLICT(id) DO UPDATE SET version = excluded.version
                """,
                (SCHEMA_VERSION,),
            )

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
    ) -> PendingDecision:
        context_json = json.dumps(context or {}, sort_keys=True)
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO pending_decision(
                    media_key,
                    decision_type,
                    audience,
                    state_fingerprint,
                    context_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (media_key, decision_type, audience, state_fingerprint, context_json),
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


def _pending_decision_from_row(row: sqlite3.Row) -> PendingDecision:
    return PendingDecision(
        decision_id=int(row["id"]),
        media_key=row["media_key"],
        decision_type=row["decision_type"],
        audience=row["audience"],
        state_fingerprint=row["state_fingerprint"],
        status=row["status"],
        context=json.loads(row["context_json"]),
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
"""
