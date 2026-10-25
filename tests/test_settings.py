"""Admin settings persistence, authorization and safe display naming."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from curatarr.adapters import AdapterError
from curatarr.services.settings import InstallationSettings
from curatarr.storage import Storage, StorageError
from curatarr.web.app import create_app
import test_miniapp as miniapp_fixtures
import test_viewing_workflow as viewing_fixtures


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workflow.sqlite"
        self.storage = Storage(self.path)
        self.storage.initialize()
        self.addCleanup(self.storage.close)
        self.settings = InstallationSettings(self.storage)

    def test_defaults_persisted_name_and_independent_policy_changes(self):
        self.assertEqual(self.settings.get()["display_name"], "Household")
        self.assertFalse(self.settings.get()["owned_cleanup_enabled"])
        self.settings.save(expected_revision=0, actor="admin", display_name=" Chickenhouse ")
        second = Storage(self.path)
        try:
            self.assertEqual(InstallationSettings(second).display_name(), "Chickenhouse")
        finally:
            second.close()
        self.settings.save(expected_revision=1, actor="admin", owned_cleanup_enabled=True)
        self.assertEqual(self.settings.display_name(), "Chickenhouse")
        self.assertTrue(self.settings.get()["owned_cleanup_enabled"])
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], 2)

    def test_invalid_names_and_stale_admin_changes_cannot_overwrite_policy(self):
        for name in ("", "   ", "x" * 61, "line\nbreak", "bad\x00name", "bad\u202ename"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.settings.save(expected_revision=0, actor="admin", display_name=name)
        self.settings.save(expected_revision=0, actor="admin", display_name="Chickenhouse")
        with self.assertRaises(StorageError):
            self.settings.save(expected_revision=0, actor="other", owned_cleanup_enabled=True)
        self.assertFalse(self.settings.get()["owned_cleanup_enabled"])

    def test_admin_api_only_and_public_name_without_operation_details(self):
        fixtures = miniapp_fixtures.MiniAppTests()
        fixtures.setUp()
        self.addCleanup(fixtures.doCleanups)
        client = TestClient(create_app(fixtures.config, database=self.path, service=fixtures.service))
        headers = {"Authorization": "tma " + miniapp_fixtures.signed_data(1)}
        standard = {"Authorization": "tma " + miniapp_fixtures.signed_data(2)}
        self.assertEqual(client.get("/api/settings").status_code, 401)
        for method in (client.get, client.post):
            self.assertEqual(method("/api/settings", headers=standard).status_code, 403)
        response = client.post("/api/settings", headers=headers,
                               json={"expected_revision": 0, "display_name": "<b>Chickenhouse</b>", "owned_cleanup_enabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["owned_cleanup_enabled"])
        self.assertEqual(client.get("/api/me", headers=standard).json()["display_name"], "<b>Chickenhouse</b>")
        self.assertEqual(client.post("/api/settings", headers=headers,
                                    json={"expected_revision": 0, "display_name": "Other"}).status_code, 409)
        for body in ({"expected_revision": 1, "owned_cleanup_enabled": "yes"},
                     {"expected_revision": 1, "unknown": True},
                     {"expected_revision": 1, "display_name": " "}):
            self.assertIn(client.post("/api/settings", headers=headers, json=body).status_code, (400, 422))
        with self.storage.connection:
            self.storage.connection.execute("INSERT INTO job_run(job_name,status,cursor_json) VALUES('owned_cleanup','pending',?)",
                (json.dumps({"removed": 2, "total": 3, "snapshot": {"private": "not for API"}}),))
        result = client.get("/api/settings", headers=headers)
        self.assertNotIn("snapshot", result.text)
        self.assertEqual(result.json()["owned_cleanup_last_run"]["removed"], 2)

    def test_prompt_and_new_review_use_name_but_uncertain_review_text_survives_rename(self):
        fixtures = viewing_fixtures.ViewingWorkflowTests()
        fixtures.setUp()
        self.addCleanup(fixtures.doCleanups)
        settings = InstallationSettings(fixtures.storage)
        settings.save(expected_revision=0, actor="admin", display_name="Chickenhouse")
        fixtures.create_question()
        self.assertIn("Chickenhouse viewing", fixtures.workflow.prompt(fixtures.pending()).text)
        fixtures.answer("yes")
        fixtures.native.fail_after_create = True
        with self.assertRaises(AdapterError):
            fixtures.answer("r8")
        text = fixtures.native.reviews[0]["textOriginal"]
        self.assertIn("Chickenhouse viewing confirmed", text)
        settings.save(expected_revision=1, actor="admin", display_name="New name")
        fixtures.answer("r8")
        self.assertEqual(len(fixtures.native.reviews), 1)
        self.assertEqual(fixtures.native.reviews[0]["textOriginal"], text)

    def test_independent_message_cleanup_switches_persist(self):
        self.settings.save(expected_revision=0, actor="admin", cleanup_answered_prompts=True)
        self.assertTrue(self.settings.get()["cleanup_answered_prompts"])
        self.assertFalse(self.settings.get()["cleanup_unanswered_prompts"])
        self.settings.save(expected_revision=1, actor="admin", cleanup_unanswered_prompts=True)
        self.assertTrue(self.settings.get()["cleanup_answered_prompts"])
        self.assertTrue(self.settings.get()["cleanup_unanswered_prompts"])
        self.settings.save(expected_revision=2, actor="admin", cleanup_answered_prompts=False)
        self.assertTrue(self.settings.get()["cleanup_unanswered_prompts"])
        with self.assertRaises(ValueError):
            self.settings.save(expected_revision=3, actor="admin", cleanup_unanswered_prompts="yes")

    def test_reopen_api_requires_admin_and_current_movie_cursor(self):
        fixtures = miniapp_fixtures.MiniAppTests()
        fixtures.setUp()
        self.addCleanup(fixtures.doCleanups)
        client = TestClient(create_app(fixtures.config, database=self.path, service=fixtures.service))
        headers = {"Authorization": "tma " + miniapp_fixtures.signed_data(1)}
        body = {"decision_id": 1, "media_key": "ryot:movie"}
        with patch("curatarr.web.app.ViewingWorkflow") as workflow:
            standard = {"Authorization": "tma " + miniapp_fixtures.signed_data(2)}
            self.assertEqual(client.post("/api/maintenance/viewing/reopen", headers=standard, json=body).status_code, 403)
            self.assertEqual(client.post("/api/maintenance/viewing/reopen", headers=headers, json=body).status_code, 409)
            workflow.assert_not_called()
            self.storage.save_maintenance_progress(1, {"cursor": "ryot:movie"}, source="viewing_progress")
            response = client.post("/api/maintenance/viewing/reopen", headers=headers, json=body)
            self.assertEqual(response.status_code, 200)
            workflow.return_value.reopen.assert_called_once_with(1, "ryot:movie", user_id=1)
