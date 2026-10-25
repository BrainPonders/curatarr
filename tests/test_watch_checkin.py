"""Authenticated everyday check-ins share native-event safety with Telegram."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from curatarr.services.viewing_workflow import ViewingWorkflow
from curatarr.storage import Storage
from curatarr.web.app import create_app
import test_miniapp as miniapp_fixtures
import test_viewing_workflow as viewing_fixtures


class WatchCheckInTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = viewing_fixtures.ViewingWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.storage.close()
        self.path = Path(self.temp.name) / "workflow.sqlite"
        self.fixture.storage = Storage(self.path)
        self.fixture.storage.initialize()
        self.addCleanup(self.fixture.storage.close)
        self.fixture.workflow = ViewingWorkflow(self.fixture.storage, self.fixture.adapter, self.fixture.client, participants=(1, 2))
        shell = miniapp_fixtures.MiniAppTests()
        shell.setUp()
        self.service = replace(shell.service, ryot_adapter=self.fixture.reader)
        # Match the fixture Telegram client's bot identity; launches still use signed auth.
        config = replace(shell.config, telegram=replace(shell.config.telegram, bot_token="fake-token"))
        self.config = config
        self.client = TestClient(create_app(config, database=self.path, service=self.service))
        self.patcher = patch("curatarr.web.app.TelegramClient", return_value=self.fixture.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # Sign with the actual API token while keeping all credentials synthetic.
        with patch("test_miniapp.TOKEN", "fake-token"):
            self.admin = {"Authorization": "tma " + miniapp_fixtures.signed_data(1)}
            self.standard = {"Authorization": "tma " + miniapp_fixtures.signed_data(2)}
        self.fixture.workflow.activate(-100, "supergroup")
        self.fixture.native.events.append(self.fixture.native.event("new"))
        self.fixture.workflow.tick()

    def enable_diagnostics(self):
        self.config = replace(self.config, maintenance=replace(self.config.maintenance, show_diagnostics=True))
        self.client = TestClient(create_app(self.config, database=self.path, service=self.service))

    def review(self, headers=None):
        response = self.client.post("/api/viewing/review", headers=headers or self.standard, json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def answer(self, action, data=None, headers=None, **overrides):
        data = data or self.review(headers)
        question = data["movie"]["check_ins"][0]
        body = {"media_key": data["movie"]["key"], "decision_id": question["decision_id"],
                "expected_version": question["version"], "action": action, **overrides}
        return self.client.post("/api/viewing/answer", headers=headers or self.standard, json=body)

    def test_standard_user_completes_confirmation_and_rating_with_admin_still_protected(self):
        for path in ("/api/settings", "/api/maintenance"):
            self.assertEqual(self.client.get(path, headers=self.standard).status_code, 403)
        self.assertEqual(self.client.get("/api/viewing/summary", headers=self.standard).json()["pending_movie_count"], 1)
        self.assertEqual(self.answer("yes").status_code, 200)
        self.assertEqual(self.answer("pick3").status_code, 200)
        result = self.answer("confirm3")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["pending_movie_count"], 0)
        self.assertEqual(len(self.fixture.native.reviews), 1)
        self.assertEqual(len(self.fixture.native.events), 2)
        self.assertEqual(self.review()["movie_count"], 0)

    def test_every_endpoint_requires_signed_allowed_user(self):
        for method, path, payload in (("get", "/api/viewing/summary", None),
                                      ("get", "/api/viewing/poster/1", None),
                                      ("post", "/api/viewing/review", {}),
                                      ("post", "/api/viewing/reopen", {}),
                                      ("post", "/api/viewing/answer", {})):
            options = {"json": payload} if payload is not None else {}
            self.assertEqual(getattr(self.client, method)(path, **options).status_code, 401)

    def test_shared_cursor_and_revision_prevent_stale_or_cross_movie_answer(self):
        original = self.review()
        self.assertEqual(self.answer("yes", original, media_key="ryot:another").status_code, 409)
        self.assertEqual(self.answer("confirm5", original).status_code, 409)
        self.assertEqual(self.answer("yes", original, expected_version="0" * 64).status_code, 409)
        self.assertEqual(self.answer("yes", original, headers=self.admin).status_code, 409)
        self.assertEqual(self.answer("yes", original).status_code, 200)
        self.assertEqual(self.answer("no", original).status_code, 409)
        self.assertEqual(self.fixture.native.reviews, [])

    def test_repeat_viewings_count_once_and_disputes_remain_reviewable(self):
        self.fixture.native.events.append(self.fixture.native.event("repeat"))
        self.fixture.workflow.tick()
        data = self.review()
        self.assertEqual(len(data["movie"]["check_ins"]), 2)
        self.assertEqual(data["pending_movie_count"], 1)
        self.assertEqual(self.answer("no", data).status_code, 200)
        data = self.review()
        self.assertFalse(data["movie"]["check_ins"][0]["disputed"])
        self.assertEqual(len(data["movie"]["check_ins"]), 1)
        self.assertEqual(data["pending_movie_count"], 1)
        second = data["movie"]["check_ins"][0]
        response = self.client.post("/api/viewing/answer", headers=self.standard, json={
            "media_key": data["movie"]["key"], "decision_id": second["decision_id"],
            "expected_version": second["version"], "action": "no"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_movie_count"], 0)
        self.assertEqual(self.review()["movie_count"], 0)
        self.assertEqual(self.review(self.admin)["movie_count"], 1)

    def test_first_yes_clears_other_users_before_optional_rating_and_blocks_stale_no(self):
        original_admin = self.review(self.admin)
        self.assertEqual(self.answer("yes").status_code, 200)
        self.assertEqual(self.review(self.admin)["movie_count"], 0)
        self.assertEqual(self.client.get("/api/viewing/summary", headers=self.admin).json()["pending_movie_count"], 0)
        self.assertEqual(self.answer("no", original_admin, headers=self.admin).status_code, 409)
        current = self.fixture.pending()
        with self.assertRaisesRegex(ValueError, "already confirmed"):
            self.fixture.workflow.respond(f"view:{current.decision_id}:no", user_id=1, chat_id=-100,
                                          message_id=current.context["delivery"]["message_id"])
        self.assertTrue(self.fixture.pending().context["confirmed"])
        self.assertEqual(self.fixture.pending().context["confirmed_by"], 2)
        self.assertEqual(self.answer("decline").status_code, 200)

    def test_one_no_waits_for_other_user_then_yes_preserves_shared_history(self):
        self.assertEqual(self.answer("no").status_code, 200)
        self.assertEqual(self.review()["movie_count"], 0)
        self.assertEqual(self.review(self.admin)["movie_count"], 1)
        self.assertFalse(self.fixture.pending().context["declined_all"])
        self.assertEqual(self.answer("yes", headers=self.admin).status_code, 200)
        self.assertEqual(self.fixture.pending().context["responses"]["2"]["answer"], "no")
        self.assertTrue(self.fixture.pending().context["confirmed"])
        self.assertEqual(len(self.fixture.native.events), 2)
        self.assertEqual(self.fixture.native.reviews, [])

    def test_all_no_moves_to_admin_and_can_be_confirmed_without_deleting_history(self):
        self.assertEqual(self.answer("no").status_code, 200)
        self.assertEqual(self.answer("no", headers=self.admin).status_code, 200)
        self.assertEqual(self.review()["movie_count"], 0)
        self.assertEqual(self.review(self.admin)["movie_count"], 0)
        self.assertEqual(self.client.get("/api/maintenance/viewing/declined", headers=self.standard).status_code, 403)
        declined = self.client.get("/api/maintenance/viewing/declined", headers=self.admin).json()["movies"]
        self.assertEqual(len(declined), 1)
        question = declined[0]["question"]
        body = {"decision_id": question["decision_id"], "media_key": declined[0]["media_key"],
                "expected_version": question["version"], "action": "confirm_declined"}
        self.assertEqual(self.client.post("/api/maintenance/viewing/confirm-declined", headers=self.standard, json=body).status_code, 403)
        result = self.client.post("/api/maintenance/viewing/confirm-declined", headers=self.admin, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.get("/api/maintenance/viewing/declined", headers=self.admin).json()["movies"], [])
        self.assertEqual(self.review(self.admin)["movie_count"], 1)
        self.assertEqual(self.review()["movie_count"], 0)
        self.assertEqual(len(self.fixture.native.events), 2)
        self.assertEqual(self.fixture.native.reviews, [])

    def test_attribution_uses_signed_name_and_original_text_survives_uncertain_retry(self):
        self.assertEqual(self.answer("yes").status_code, 200)
        self.assertEqual(self.answer("pick4").status_code, 200)
        self.fixture.native.fail_after_create = True
        self.assertEqual(self.answer("confirm4").status_code, 503)
        original_text = self.fixture.native.reviews[0]["textOriginal"]
        self.assertIn("Rated by Test through Curatarr.", original_text)
        self.assertEqual(self.fixture.pending().context["rated_by"]["user_id"], 2)
        with patch("test_miniapp.TOKEN", "fake-token"):
            fields = miniapp_fixtures.signed_data(2, user='{"id":2,"first_name":"Different name"}')
        changed = {"Authorization": "tma " + fields}
        self.assertEqual(self.answer("r4", headers=changed).status_code, 200)
        self.assertEqual(len(self.fixture.native.reviews), 1)
        self.assertEqual(self.fixture.native.reviews[0]["textOriginal"], original_text)

    def test_legacy_dispute_has_no_unanimous_decline_and_can_be_reconsidered(self):
        from copy import deepcopy
        original = self.fixture.pending()
        context = deepcopy(original.context)
        context.pop("participants"); context.pop("responses")
        context["disputed"] = True
        self.fixture.workflow.store.update(original, context, actor="test", action="legacy_fixture")
        data = self.review()
        self.assertEqual(data["pending_movie_count"], 1)
        self.assertEqual({a["action"] for a in data["movie"]["check_ins"][0]["actions"]}, {"yes", "no"})
        self.assertEqual(self.answer("yes", data).status_code, 200)
        self.assertTrue(self.fixture.pending().context["legacy_disputed"])
        self.assertEqual(self.review(self.admin)["movie_count"], 0)

    def test_participant_snapshot_survives_restart_and_configuration_changes(self):
        self.assertEqual(self.answer("no").status_code, 200)
        restarted = Storage(self.path)
        try:
            workflow = ViewingWorkflow(restarted, self.fixture.adapter, self.fixture.client, participants=(1, 2, 3))
            decision = restarted.viewing_followups()[0]
            workflow.respond(f"view:{decision.decision_id}:no", user_id=1, chat_id=-100,
                             message_id=decision.context["delivery"]["message_id"], user_name="Admin")
            self.assertTrue(restarted.viewing_followups()[0].context["declined_all"])
            self.assertEqual(restarted.viewing_followups()[0].context["participants"], [1, 2])
        finally:
            restarted.close()

    def test_repeated_no_never_counts_as_another_household_member(self):
        self.assertEqual(self.answer("no").status_code, 200)
        decision = self.fixture.pending()
        self.fixture.workflow.respond(f"view:{decision.decision_id}:no", user_id=2, chat_id=-100,
                                      message_id=decision.context["delivery"]["message_id"])
        current = self.fixture.pending()
        self.assertFalse(current.context["declined_all"])
        self.assertEqual(list(current.context["responses"]), ["2"])
        with self.assertRaisesRegex(ValueError, "not a respondent"):
            self.fixture.workflow.respond(f"view:{decision.decision_id}:no", user_id=3, chat_id=-100,
                                          message_id=decision.context["delivery"]["message_id"])

    def test_legacy_confirmed_question_adopts_recorded_owner_without_rewriting_operation(self):
        from copy import deepcopy
        original = self.fixture.pending()
        context = deepcopy(original.context)
        context.pop("participants"); context.pop("responses")
        self.fixture.workflow.store.update(original, context, actor="test", action="legacy_fixture")
        legacy = ViewingWorkflow(self.fixture.storage, self.fixture.adapter, self.fixture.client)
        legacy.respond(f"view:{original.decision_id}:yes", user_id=1, chat_id=-100,
                       message_id=original.context["delivery"]["message_id"])
        self.fixture.workflow.adopt_household_policy()
        current = self.fixture.pending()
        self.assertEqual(current.context["confirmed_by"], 1)
        self.assertEqual(current.context["participants"], [1, 2])
        self.assertEqual(self.review()["movie_count"], 0)
        self.assertEqual(self.review(self.admin)["movie_count"], 1)

    def test_removed_prompt_can_be_answered_and_reopened_by_standard_user(self):
        decision = self.fixture.pending()
        self.fixture.workflow.store.update_delivery(decision, {"state": "removed"}, actor="test", action="cleanup")
        data = self.review()
        response = self.client.post("/api/viewing/reopen", headers=self.standard, json={
            "decision_id": decision.decision_id, "media_key": decision.media_key})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.answer("yes").status_code, 200)
        self.assertEqual(self.answer("decline").status_code, 200)
        self.assertEqual(self.fixture.storage.viewing_followups(), ())

    def stale_body(self):
        movies = self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()["movies"]
        self.assertEqual(len(movies), 1)
        question = movies[0]["question"]
        return {"decision_id": question["decision_id"], "media_key": movies[0]["media_key"],
                "expected_version": question["version"], "action": "clear_stale"}

    def test_normal_mode_retains_detection_but_blocks_all_technical_repair_routes(self):
        decision = self.fixture.pending()
        original = self.fixture.native.events.pop()
        item = self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()["movies"][0]
        self.assertEqual(item["question"]["actions"], [])
        body = {"decision_id": decision.decision_id, "media_key": decision.media_key,
                "expected_version": item["question"]["version"], "action": "clear_stale"}
        for action in ("clear_stale", "clear_history"):
            result=self.client.post("/api/maintenance/viewing/confirm-declined",headers=self.admin,json={**body,"action":action})
            self.assertEqual(result.status_code,403)
        self.assertEqual(self.client.post("/api/maintenance/viewing/reset-preview",headers=self.admin,
                                         json={**body,"action":"clear_history"}).status_code,403)
        self.assertEqual(self.client.post("/api/maintenance/correct",headers=self.admin,
                                         json={"media_key":"radarr:1","finding_id":1,"confirm":True}).status_code,403)
        self.assertEqual(self.fixture.pending().context,decision.context)
        self.assertNotIn(original,self.fixture.native.events)
        self.assertEqual(self.client.get("/api/viewing/summary",headers=self.standard).json()["pending_movie_count"],0)

    def test_normal_decline_review_offers_only_household_confirmation_and_review_again(self):
        self.assertEqual(self.answer("no").status_code,200)
        self.assertEqual(self.answer("no",headers=self.admin).status_code,200)
        item=self.client.get("/api/maintenance/viewing/declined",headers=self.admin).json()["movies"][0]
        self.assertEqual([a["action"] for a in item["question"]["actions"]],["confirm_declined","review_again"])
        body={"decision_id":item["question"]["decision_id"],"media_key":item["media_key"],
              "expected_version":item["question"]["version"],"action":"review_again"}
        self.assertEqual(self.client.post("/api/maintenance/viewing/confirm-declined",headers=self.admin,json=body).status_code,200)
        self.assertEqual(self.fixture.pending().context["responses"],{})

    def test_viewing_sync_group_is_admin_only_and_restored_event_returns_to_inbox(self):
        original = self.fixture.native.events.pop()
        self.assertEqual(self.client.get("/api/maintenance/viewing/issues", headers=self.standard).status_code, 403)
        data = self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()
        self.assertTrue(data["complete"])
        self.assertEqual(len(data["movies"]), 1)
        self.assertTrue(data["movies"][0]["sync_issue"])
        self.assertEqual(self.client.get("/api/viewing/summary", headers=self.standard).json()["pending_movie_count"], 0)
        self.fixture.native.events.append(original)
        self.assertEqual(self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()["movies"], [])
        self.assertEqual(self.client.get("/api/viewing/summary", headers=self.standard).json()["pending_movie_count"], 1)
        self.assertEqual(self.fixture.pending().status, "open")

    def test_changed_completion_is_sync_issue_but_cannot_be_cleared_as_missing(self):
        self.enable_diagnostics()
        self.fixture.native.events[-1]["state"] = "IN_PROGRESS"
        data = self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()
        self.assertTrue(data["complete"])
        self.assertEqual(len(data["movies"]), 1)
        self.assertIn("no longer completed", data["movies"][0]["notice"])
        actions = data["movies"][0]["question"]["actions"]
        self.assertFalse(next(a for a in actions if a["action"] == "clear_stale")["enabled"])
        self.assertEqual(self.client.get("/api/viewing/summary", headers=self.standard).json()["pending_movie_count"], 0)
        self.assertEqual(self.client.post("/api/maintenance/viewing/confirm-declined", headers=self.admin,
                                        json=self.stale_body()).status_code, 503)
        self.assertEqual(self.fixture.pending().status, "open")

    def test_missing_repeat_does_not_hide_another_valid_watch_of_the_same_movie(self):
        self.fixture.native.events.pop()
        self.fixture.native.events.append(self.fixture.native.event("replacement-watch"))
        self.fixture.workflow.tick()
        issues = self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()
        self.assertEqual(len(issues["movies"]), 1)
        data = self.review()
        self.assertEqual(data["pending_movie_count"], 1)
        self.assertEqual(len(data["movie"]["check_ins"]), 1)
        self.assertNotEqual(data["movie"]["check_ins"][0]["decision_id"], issues["movies"][0]["question"]["decision_id"])

    def test_outage_does_not_invent_a_stale_request_or_hide_an_unverified_question(self):
        self.fixture.native.unavailable = True
        data = self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()
        self.assertFalse(data["complete"])
        self.assertEqual(data["movies"], [])
        self.assertEqual(self.client.get("/api/viewing/summary", headers=self.standard).json()["pending_movie_count"], 1)
        self.assertEqual(self.fixture.pending().status, "open")

    def test_missing_native_viewing_can_only_be_cleared_by_admin_without_claiming_confirmation(self):
        self.enable_diagnostics()
        self.fixture.native.events.pop()
        data = self.review()
        self.assertEqual(data["pending_movie_count"], 0)
        self.assertIsNone(data["movie"])
        self.assertEqual(self.client.get("/api/maintenance/viewing/declined", headers=self.admin).json()["movies"], [])
        body = self.stale_body()
        path = "/api/maintenance/viewing/confirm-declined"
        self.assertEqual(self.client.post(path, headers=self.standard, json=body).status_code, 403)
        result = self.client.post(path, headers=self.admin, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        decision = self.fixture.storage.get_pending_decision(body["decision_id"])
        self.assertEqual(decision.status, "resolved")
        self.assertFalse(decision.context["confirmed"])
        self.assertIn("REQUEST CLOSED", self.fixture.workflow.prompt(decision).text)
        self.assertEqual(len(self.fixture.native.events), 1)
        self.assertEqual(self.fixture.native.reviews, [])

    def test_stale_clear_rechecks_identity_and_event_absence_before_closing(self):
        self.enable_diagnostics()
        original = self.fixture.native.events.pop()
        body = self.stale_body()
        path = "/api/maintenance/viewing/confirm-declined"
        self.fixture.native.metadata["identifier"] = "999"
        self.assertEqual(self.client.post(path, headers=self.admin, json=body).status_code, 503)
        self.fixture.native.metadata["identifier"] = "1"
        self.fixture.native.unavailable = True
        self.assertFalse(self.client.get("/api/maintenance/viewing/issues", headers=self.admin).json()["complete"])
        self.assertEqual(self.client.post(path, headers=self.admin, json=body).status_code, 503)
        self.fixture.native.unavailable = False
        self.fixture.native.events.append(original)
        self.assertEqual(self.client.post(path, headers=self.admin, json=body).status_code, 409)
        self.assertEqual(self.fixture.pending().status, "open")

    def test_incomplete_missing_history_or_pending_rating_is_never_cleared(self):
        self.enable_diagnostics()
        from copy import deepcopy
        self.fixture.native.events.pop()
        body = self.stale_body()
        original = deepcopy(self.fixture.native.events)
        self.fixture.native.events.append(deepcopy(original[0]))
        path = "/api/maintenance/viewing/confirm-declined"
        self.assertEqual(self.client.post(path, headers=self.admin, json=body).status_code, 503)
        self.fixture.native.events = original
        decision = self.fixture.pending()
        context = deepcopy(decision.context)
        context["operation"] = {"score": 4, "state": "started", "review_text": "Keep this original text"}
        self.fixture.workflow.store.update(decision, context, actor="test", action="pending_operation_fixture")
        body["expected_version"] = self.fixture.workflow.version(self.fixture.pending())
        self.assertEqual(self.client.post(path, headers=self.admin, json=body).status_code, 409)
        self.assertEqual(self.fixture.pending().context["operation"]["review_text"], "Keep this original text")
