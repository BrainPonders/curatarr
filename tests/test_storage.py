"""Tests for SQLite workflow storage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from curatarr.domain import MediaIdentity
from curatarr.storage import Storage
from curatarr.storage.sqlite import SCHEMA_VERSION


class StorageTests(unittest.TestCase):
    def _storage(self) -> Storage:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        storage = Storage(Path(temp_dir.name) / "curatarr.sqlite")
        self.addCleanup(storage.close)
        storage.initialize()
        return storage

    def test_schema_initializes_expected_tables(self) -> None:
        storage = self._storage()

        self.assertEqual(storage.schema_version(), SCHEMA_VERSION)
        rows = storage.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        table_names = {row["name"] for row in rows}

        self.assertIn("media_identity", table_names)
        self.assertIn("identity_mapping", table_names)
        self.assertIn("state_snapshot", table_names)
        self.assertIn("pending_decision", table_names)
        self.assertIn("approval_request", table_names)
        self.assertIn("operational_issue", table_names)
        self.assertIn("job_run", table_names)
        self.assertIn("audit_log", table_names)
        self.assertIn("viewing_event", table_names)

    def test_newer_workflow_schema_is_not_downgraded(self):
        storage = self._storage()
        with storage.connection:
            storage.connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION + 1,))
        from curatarr.storage import StorageError
        with self.assertRaises(StorageError):
            storage.initialize()
        self.assertEqual(storage.schema_version(), SCHEMA_VERSION + 1)

    def test_version_two_upgrade_preserves_decisions_and_adds_event_ledger(self):
        storage = self._storage()
        decision = storage.create_pending_decision(media_key="ryot:1", decision_type="legacy", audience="admin", state_fingerprint="x")
        with storage.connection:
            storage.connection.execute("DROP TABLE viewing_event")
            storage.connection.execute("UPDATE schema_version SET version=2")
        storage.initialize()
        self.assertEqual(storage.get_pending_decision(decision.decision_id), decision)
        self.assertEqual(storage.schema_version(), SCHEMA_VERSION)
        self.assertEqual(storage.connection.execute("SELECT count(*) FROM viewing_event").fetchone()[0], 0)

    def test_identity_mapping_supports_positive_and_negative_decisions(self) -> None:
        storage = self._storage()
        tmdb = MediaIdentity("tmdb", "558")
        imdb = MediaIdentity("imdb", "tt0316654")

        positive = storage.add_identity_mapping(tmdb, imdb, is_same=True, source="admin")
        self.assertTrue(positive.is_same)
        self.assertEqual(storage.get_identity_mapping(imdb, tmdb), positive)

        negative = storage.add_identity_mapping(tmdb, imdb, is_same=False, source="admin")
        self.assertFalse(negative.is_same)
        self.assertEqual(storage.get_identity_mapping(tmdb, imdb), negative)

    def test_pending_decision_can_be_inserted_and_read(self) -> None:
        storage = self._storage()

        created = storage.create_pending_decision(
            media_key="tmdb:558",
            decision_type="post_watch",
            audience="user:1",
            state_fingerprint="abc123",
            context={"title": "Spider-Man 2"},
        )
        loaded = storage.get_pending_decision(created.decision_id)

        self.assertEqual(loaded, created)
        assert loaded is not None
        self.assertEqual(loaded.status, "open")
        self.assertEqual(loaded.context["title"], "Spider-Man 2")

    def test_pending_decision_status_can_be_updated(self) -> None:
        storage = self._storage()
        created = storage.create_pending_decision(
            media_key="tmdb:558",
            decision_type="post_watch",
            audience="user:1",
            state_fingerprint="abc123",
            context={},
        )

        updated = storage.update_pending_decision_status(created.decision_id, "superseded")

        self.assertEqual(updated.status, "superseded")
        self.assertEqual(storage.get_pending_decision(created.decision_id), updated)

    def test_audit_log_can_be_appended(self) -> None:
        storage = self._storage()

        entry = storage.append_audit_log(
            actor="system",
            action="config_checked",
            media_key=None,
            details={"result": "ok"},
        )

        self.assertEqual(entry.actor, "system")
        self.assertEqual(entry.action, "config_checked")
        self.assertEqual(entry.details, {"result": "ok"})

    def test_confirmed_deletion_is_exact_identity_and_explicit_verified_outcome(self):
        storage = self._storage()
        identity = MediaIdentity("tmdb", "558")
        for details in ({}, {"verified": True, "outcome": "failed", "source": "curatarr"},
                        {"verified": True, "outcome": "success", "source": "missing_file_scan"}):
            storage.append_audit_log(actor="system", action="media_file_deleted", media_key=str(identity), details=details)
        self.assertFalse(storage.has_confirmed_file_deletion([identity]))
        storage.record_confirmed_file_deletion(identity, actor="admin", source="admin_verified")
        self.assertTrue(storage.has_confirmed_file_deletion([identity]))
        self.assertFalse(storage.has_confirmed_file_deletion([MediaIdentity("imdb", "558")]))
        self.assertFalse(storage.has_confirmed_file_deletion([MediaIdentity("tmdb", "999")]))
        self.assertFalse(storage.has_confirmed_file_deletion([]))
        with self.assertRaises(ValueError):
            storage.record_confirmed_file_deletion(identity, actor="system", source="missing_file_scan")


if __name__ == "__main__":
    unittest.main()
