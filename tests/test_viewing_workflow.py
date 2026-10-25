"""Offline native-API simulation for event detection, replies and safe rating retries."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.adapter import RyotReadAdapter
from curatarr.integrations.ryot.client import RyotClient, RyotError
from curatarr.integrations.ryot.viewing import RyotViewingAdapter, viewing_review_text
from curatarr.integrations.telegram.bot import TelegramBot
from curatarr.integrations.telegram.client import TelegramClient, TelegramError
from curatarr.services.viewing import ViewingFollowupService
from curatarr.services.viewing_workflow import ViewingWorkflow
from curatarr.storage import Storage, StorageError
from curatarr.storage.viewing import ViewingStore
from unittest.mock import Mock


class NativeRyot:
    def __init__(self):
        self.events = [self.event("old")]
        self.reviews = []
        self.calls = []
        self.scale = "OUT_OF_HUNDRED"
        self.unavailable = False
        self.fail_after_create = False
        self.fail_after_link = False
        self.link_other_after_create = False
        self.scale_before_create = None
        self.reset_collections = {n: {"collectionId": "col-" + n, "collectionName": n, "rank": None, "information": None}
                                  for n in ("Library", "Completed")}
        self.reset_jobs = []
        self.defer_reset_flags = False
        self.fail_after_delete = False
        self.fail_flag_response = False
        self.reset_collection_owner = "household"
        self.metadata = {"id": "movie", "title": "Example", "publishYear": 2026,
                         "lot": "MOVIE", "source": "TMDB", "identifier": "1"}

    @staticmethod
    def event(identifier):
        return {"id": identifier, "userId": "household", "metadataId": "movie", "state": "COMPLETED",
                "progress": "100", "finishedOn": "2026-10-22T10:00:00Z", "reviewId": None}

    def user(self):
        return {"hasInteracted": True, "collections": ["Library"],
                "history": deepcopy(self.events), "reviews": deepcopy(self.reviews)}

    def transport(self, query, variables):
        self.calls.append((query, deepcopy(variables)))
        if self.unavailable:
            raise RyotError("PRIVATE TOKEN")
        if "CuratarrViewingAccount" in query:
            return {"data": {"userDetails": {"id": "household", "preferences": {"general": {"reviewScale": self.scale}}}}}
        if "CuratarrViewingResetSnapshot" in query:
            user = self.user()
            user["collections"] = [{"details": deepcopy(d)} for d in self.reset_collections.values()]
            return {"data": {"metadataDetails": {"response": deepcopy(self.metadata)},
                             "userMetadataDetails": {"response": user},
                             "userCollectionsList": {"response": [{"id": "col-" + n, "name": n,
                                 "isDefault": n != "Library", "creator": {"id": self.reset_collection_owner}}
                                 for n in ("Library", "Completed", "In Progress", "Monitoring", "Watchlist")]}}}
        if "CuratarrClearViewingFlag" in query:
            self.reset_jobs.append(deepcopy(variables["input"]))
            if not self.defer_reset_flags:
                self.reset_collections.pop(variables["input"]["collectionName"], None)
            if self.fail_flag_response:
                self.fail_flag_response = False
                raise RyotError("Flag response lost")
            return {"data": {"deployRemoveEntitiesFromCollectionJob": True}}
        if "CuratarrClearViewing" in query:
            identifier = variables["id"]
            event = next((e for e in self.events if e["id"] == identifier), None)
            if event is None:
                raise RyotError("Missing viewing")
            self.events = [e for e in self.events if e["id"] != identifier]
            self.reset_collections.pop("Watchlist", None)
            if event["state"] == "IN_PROGRESS":
                for name in ("In Progress", "Monitoring"):
                    self.reset_collections[name] = {"collectionId": "col-" + name, "collectionName": name, "rank": None, "information": None}
            else:
                self.reset_collections["Completed"] = {"collectionId": "col-Completed", "collectionName": "Completed", "rank": None, "information": None}
                for name in ("In Progress", "Monitoring"):
                    self.reset_collections.pop(name, None)
            if self.fail_after_delete:
                self.fail_after_delete = False
                raise RyotError("Reply lost after delete")
            return {"data": {"deleteSeenItem": {"id": identifier}}}
        if "CuratarrUserMetadataList" in query:
            return {"data": {"userMetadataList": {"response": {"items": ["movie"], "details": {"nextPage": None}}}}}
        if "CuratarrLibraryDetails" in query:
            return {"data": {"m0": {"response": deepcopy(self.metadata)}, "u0": {"response": self.user()}}}
        if "CuratarrViewingEvent" in query or "CuratarrMetadataById" in query:
            return {"data": {"metadataDetails": {"response": deepcopy(self.metadata)},
                             "userMetadataDetails": {"response": self.user()}}}
        if "CuratarrViewingRating" in query:
            payload = variables["input"]
            if self.scale_before_create:
                self.scale, self.scale_before_create = self.scale_before_create, None
            # Ryot v10.5.0 normalizes inputs; reads always return 0-100 ratings.
            factor = {"OUT_OF_FIVE": 20, "OUT_OF_TEN": 10}.get(self.scale, 1)
            rating = str(Decimal(payload["rating"]) * factor)
            identifier = f"review-{len(self.reviews) + 1}"
            self.reviews.append({"id": identifier, "rating": rating, "textOriginal": payload["text"],
                                 "visibility": payload["visibility"], "isSpoiler": False, "postedBy": {"id": "household"}})
            if self.link_other_after_create:
                self.reviews.append({"id": "manual", "rating": "90", "textOriginal": "Existing manual review",
                                     "visibility": "PRIVATE", "postedBy": {"id": "household"}})
                self.events[-1]["reviewId"] = "manual"
            if self.fail_after_create:
                self.fail_after_create = False
                raise RyotError("Reply lost after review creation")
            return {"data": {"createOrUpdateReview": {"id": identifier}}}
        if "CuratarrLinkViewingRating" in query:
            payload = variables["input"]
            event = next(e for e in self.events if e["id"] == payload["seenId"])
            event["reviewId"] = payload["reviewId"]
            if self.fail_after_link:
                self.fail_after_link = False
                raise RyotError("Reply lost after link")
            return {"data": {"updateSeenItem": True}}
        raise AssertionError(query)


class ViewingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.storage.initialize()
        self.addCleanup(self.storage.close)
        self.native = NativeRyot()
        self.reader = RyotReadAdapter(RyotClient("https://ryot.invalid/graphql", "secret", self.native.transport))
        self.adapter = RyotViewingAdapter(self.reader)
        self.sent = []
        self.fail_send = False

        def telegram(method, payload):
            if method == "getMe":
                return {"ok": True, "result": {"id": 77, "username": "curatarr_test"}}
            if method == "getChat":
                return {"ok": True, "result": {"id": payload["chat_id"], "type": "private" if payload["chat_id"] > 0 else "supergroup"}}
            if method == "getUpdates":
                return {"ok": True, "result": []}
            if method in {"sendMessage", "sendPhoto"}:
                self.sent.append(deepcopy(payload))
                if self.fail_send:
                    raise TelegramError("Send outcome unknown")
                return {"ok": True, "result": {"message_id": len(self.sent), "chat": {"id": payload["chat_id"]},
                                               **({"photo": [{"file_id": "poster-file"}]} if method == "sendPhoto" else {})}}
            return {"ok": True, "result": True}

        self.client = TelegramClient("fake-token", telegram)
        self.workflow = ViewingWorkflow(self.storage, self.adapter, self.client)

    def pending(self):
        return self.storage.viewing_followups()[0]

    def create_question(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("new"))
        self.workflow.tick()
        # Preserve coverage of legacy 0-10 prompts alongside the new 1-5 tests.
        decision = self.pending()
        context = deepcopy(decision.context)
        context["rating_scale"] = 10
        return self.workflow.store.update(decision, context, actor="test", action="legacy_fixture")

    def answer(self, action, *, decision=None, **overrides):
        decision = decision or self.pending()
        args = {"user_id": 1, "chat_id": -100, "message_id": decision.context["delivery"].get("message_id")}
        args.update(overrides)
        return self.workflow.respond(f"view:{decision.decision_id}:{action}", **args)

    def web_answer(self, action, decision=None):
        decision = decision or self.pending()
        return self.workflow.respond_web(decision.decision_id, action, media_key=decision.media_key,
                                         expected_version=self.workflow.version(decision), user_id=2)

    def test_web_and_telegram_share_question_and_rating_without_duplicate_event(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("new"))
        self.workflow.tick()
        original = self.pending()
        self.web_answer("yes")
        self.assertTrue(self.pending().context["confirmed"])
        self.answer("pick4")
        self.web_answer("confirm4")
        self.assertEqual(len(self.native.events), 2)
        self.assertEqual(len(self.native.reviews), 1)
        self.assertEqual(self.native.reviews[0]["rating"], "80")
        self.assertEqual(self.storage.get_pending_decision(original.decision_id).status, "resolved")
        self.assertIn("VIEWING CONFIRMED", self.answer("yes", decision=original).text)
        self.assertEqual(self.storage.viewing_followups(), ())

    def test_web_rejects_stale_other_movie_forged_and_disabled_actions(self):
        original = self.create_question()
        self.answer("yes")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.web_answer("no", original)
        current = self.pending()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.workflow.respond_web(current.decision_id, "decline", media_key="ryot:other",
                                      expected_version=self.workflow.version(current), user_id=2)
        with self.assertRaisesRegex(ValueError, "no longer available"):
            self.web_answer("r8")
        binding = self.workflow.binding()
        self.workflow.store.bind({**binding, "enabled": False})
        with self.assertRaisesRegex(ValueError, "not available"):
            self.web_answer("decline")
        self.assertEqual(self.native.reviews, [])

    def test_web_answer_after_message_cleanup_and_dispute_preserves_native_history(self):
        original = self.create_question()
        self.workflow.store.update_delivery(original, {"state": "removed"}, actor="test", action="cleanup")
        question = self.workflow.web_question(self.pending())
        self.assertEqual({a["action"] for a in question["actions"]}, {"yes", "no"})
        self.web_answer("no")
        self.assertTrue(self.pending().context["disputed"])
        self.assertEqual(self.workflow.web_question(self.pending())["actions"], [])
        self.assertEqual(len(self.native.events), 2)
        self.assertEqual(self.native.reviews, [])

    def test_web_resolved_telegram_edit_failure_is_persisted_and_retried(self):
        self.create_question()
        self.web_answer("yes")
        from unittest.mock import patch
        with patch.object(TelegramClient, "send_prompt", side_effect=AdapterError("unavailable")):
            self.web_answer("decline")
        decision = self.storage.get_pending_decision(1)
        self.assertEqual(decision.status, "resolved")
        self.assertTrue(decision.context["delivery"]["web_refresh"])
        with patch.object(TelegramClient, "send_prompt", return_value={"message_id": 1}) as edit:
            self.workflow.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.storage.get_pending_decision(1).context["delivery"]["web_refresh"])
        self.assertEqual(edit.call_args.kwargs["message_id"], 1)
        self.assertFalse(edit.call_args.kwargs["replace_missing"])

    def test_web_uncertain_rating_retries_original_score_only(self):
        self.create_question()
        self.web_answer("yes")
        for score in (6, 7, 8):
            self.web_answer(f"s{score}")
        self.native.fail_after_create = True
        with self.assertRaises(AdapterError):
            self.web_answer("save8")
        with self.assertRaises(ValueError):
            self.web_answer("decline")
        self.assertEqual([a["action"] for a in self.workflow.web_question(self.pending())["actions"]], ["r8"])
        self.web_answer("r8")
        self.assertEqual(len(self.native.reviews), 1)
        self.assertEqual(self.storage.viewing_followups(), ())

    def test_inactive_does_not_read_providers_or_send(self):
        self.workflow.tick()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.sent, [])

    def test_baseline_no_historical_prompts_and_stable_id_dedup(self):
        self.workflow.activate(-100, "supergroup")
        self.workflow.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.storage.viewing_followups(), ())
        self.native.events.append(self.native.event("new"))
        self.workflow.tick()
        self.workflow.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.storage.viewing_followups()), 1)
        self.assertEqual(self.pending().context["questions"], ["watched", "rating"])
        self.assertIn("Confirm watched", str(self.sent[0]))

    def test_failed_initial_snapshot_does_not_bind_or_advance_baseline(self):
        self.native.unavailable = True
        with self.assertRaises(AdapterError):
            self.workflow.activate(-100, "supergroup")
        self.assertIsNone(self.workflow.binding())
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM viewing_event").fetchone()[0], 0)

    def test_duplicate_snapshot_rejected_without_partial_baseline(self):
        self.native.events.append(self.native.event("old"))
        with self.assertRaises(AdapterError):
            self.workflow.activate(-100, "supergroup")
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM viewing_event").fetchone()[0], 0)

    def test_confirmation_does_not_create_watch_and_rating_converts_0_10_to_0_100(self):
        question = self.create_question()
        dates = [e["finishedOn"] for e in self.native.events]
        prompt = self.answer("yes")
        self.assertIn("0-10", prompt.text)
        self.assertEqual([b.label for b in prompt.buttons[:4]], ["-", "5 / 10", "+", "Save rating (5/10)"])
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))
        result = self.answer("r8")
        self.assertIn("8/10", result.text)
        self.assertEqual(len(self.native.events), 2)
        self.assertEqual([e["finishedOn"] for e in self.native.events], dates)
        create = [v["input"] for q, v in self.native.calls if "mutation CuratarrViewingRating" in q]
        self.assertEqual(create[0]["rating"], "80")
        self.assertEqual(create[0]["visibility"], "PRIVATE")
        link = [v["input"] for q, v in self.native.calls if "mutation CuratarrLinkViewingRating" in q]
        self.assertEqual(set(link[0]), {"seenId", "reviewId"})
        self.assertEqual(self.storage.get_pending_decision(question.decision_id).status, "resolved")
        self.assertEqual(self.storage.viewing_followups(), ())
        self.answer("r8", decision=question)
        self.assertEqual(len(self.native.reviews), 1)

    def test_rating_selection_is_local_until_explicit_save(self):
        self.create_question()
        self.answer("yes")
        for score in (0, 10, 8):
            prompt = self.answer(f"s{score}")
            self.assertIn(f"Save rating ({score}/10)", str(prompt))
        self.answer("preview")
        self.assertEqual(self.native.reviews, [])
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))
        with self.assertRaises(ValueError):
            self.answer("save5")
        self.answer("save8")
        self.assertEqual(self.native.reviews[0]["rating"], "80")

    def test_new_five_point_scale_requires_selection_and_confirmation(self):
        self.workflow.activate(-100, "supergroup")
        for score in range(1, 6):
            self.native.events.append(self.native.event(f"five-{score}"))
            self.workflow.tick()
            question = self.pending()
            self.assertEqual(question.context["rating_scale"], 5)
            prompt = self.answer("yes")
            self.assertEqual([b.label for b in prompt.buttons], ["1", "2", "3", "4", "5", "No rating"])
            for invalid in ("r4", "confirm4", "s4", "save4", "pick0", "pick6"):
                with self.assertRaises(ValueError):
                    self.answer(invalid)
            before = len(self.native.reviews)
            prompt = self.answer(f"pick{score}")
            self.assertIn(f"[{score}]", [b.label for b in prompt.buttons])
            self.assertIn(f"Confirm rating ({score}/5)", [b.label for b in prompt.buttons])
            self.assertEqual(len(self.native.reviews), before)
            other = 1 if score != 1 else 2
            with self.assertRaises(ValueError):
                self.answer(f"confirm{other}")
            prompt = self.answer(f"confirm{score}")
            self.assertIn(f"rating {score}/5 verified", prompt.text)
            self.assertEqual(self.native.reviews[-1]["rating"], str(score * 20))
            self.assertEqual(self.storage.get_pending_decision(question.decision_id).status, "resolved")

    def test_five_point_uncertain_write_retries_only_the_same_score(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("five"))
        self.workflow.tick()
        self.answer("yes")
        self.answer("pick4")
        self.native.fail_after_create = True
        with self.assertRaises(AdapterError):
            self.answer("confirm4")
        prompt = self.workflow.prompt(self.pending())
        self.assertEqual([b.label for b in prompt.buttons], ["Retry rating (4/5)"])
        with self.assertRaises(ValueError):
            self.answer("pick3")
        self.answer("r4")
        self.assertEqual(len(self.native.reviews), 1)
        self.assertEqual(self.native.reviews[0]["rating"], "80")

    def test_disputed_prompt_is_retired_but_remains_in_maintenance(self):
        question = self.create_question()
        prompt = self.answer("no")
        self.assertEqual(prompt.buttons, ())
        self.assertIn("Viewing disputed - review required", prompt.text)
        self.assertEqual(self.pending().context["questions"], ["watched"])
        data = ViewingFollowupService(self.storage, self.reader, self.workflow.bot_key).review(1)
        self.assertEqual(data["movie"]["questions"], ["Have you watched it?"])
        self.assertEqual(data["movie_count"], 1)
        self.workflow.client = Mock(wraps=self.client)
        self.workflow.tick()
        args, kwargs = self.workflow.client.send_prompt.call_args
        self.assertEqual(args[1].buttons, ())
        self.assertEqual(kwargs["message_id"], question.context["delivery"]["message_id"])
        self.assertFalse(kwargs["replace_missing"])
        self.workflow.client.send_prompt.reset_mock()
        self.workflow.tick()
        self.workflow.client.send_prompt.assert_not_called()
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))
        self.assertEqual(self.native.reviews, [])

    def test_native_five_point_input_and_normalized_readback_for_all_buttons(self):
        self.native.scale = "OUT_OF_FIVE"
        self.workflow.activate(-100, "supergroup")
        for score in range(1, 6):
            with self.subTest(score=score):
                self.native.events.append(self.native.event(f"native-five-{score}"))
                self.workflow.tick()
                self.answer("yes")
                self.answer(f"pick{score}")
                self.answer(f"confirm{score}")
                inputs = [v["input"] for q, v in self.native.calls if "mutation CuratarrViewingRating" in q]
                self.assertEqual(inputs[-1]["rating"], str(score))
                self.assertEqual(self.native.reviews[-1]["rating"], str(score * 20))
                self.assertEqual(self.storage.viewing_followups(), ())

    def test_legacy_ten_point_questions_send_exact_fractional_five_point_inputs(self):
        self.native.scale = "OUT_OF_FIVE"
        self.create_question()
        for score in (0, 1, 9, 10):
            with self.subTest(score=score):
                if score:
                    self.native.events.append(self.native.event(f"legacy-{score}"))
                    self.workflow.tick()
                    decision = self.pending()
                    context = deepcopy(decision.context)
                    context.pop("rating_scale")  # Original legacy questions had no scale marker.
                    self.workflow.store.update(decision, context, actor="test", action="legacy_fixture")
                self.answer("yes")
                self.answer(f"r{score}")
                inputs = [v["input"] for q, v in self.native.calls if "mutation CuratarrViewingRating" in q]
                self.assertEqual(Decimal(inputs[-1]["rating"]), Decimal(score) / 2)
                self.assertEqual(Decimal(self.native.reviews[-1]["rating"]), score * 10)
                self.assertEqual(self.storage.viewing_followups(), ())

    def test_supported_scale_change_preserves_baseline_and_existing_question(self):
        question = self.create_question()
        ledger_before = self.storage.connection.execute("SELECT count(*) FROM viewing_event").fetchone()[0]
        self.native.scale = "OUT_OF_FIVE"
        self.workflow.tick()
        self.assertEqual(self.pending().decision_id, question.decision_id)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM viewing_event").fetchone()[0], ledger_before)
        self.answer("yes")
        self.answer("r8")
        self.assertEqual(self.native.reviews[0]["rating"], "80")
        self.assertEqual(self.workflow.binding()["chat_id"], -100)

    def test_uncertain_creation_survives_native_scale_changes_in_both_directions(self):
        self.workflow.activate(-100, "supergroup")
        for first, second in (("OUT_OF_HUNDRED", "OUT_OF_FIVE"), ("OUT_OF_FIVE", "OUT_OF_HUNDRED")):
            with self.subTest(first=first):
                self.native.scale = first
                self.native.events.append(self.native.event(first))
                self.workflow.tick()
                self.answer("yes")
                self.answer("pick4")
                self.native.fail_after_create = True
                with self.assertRaises(AdapterError):
                    self.answer("confirm4")
                count = len(self.native.reviews)
                # Even changed presentation facts cannot change a retry's saved text.
                decision = self.pending()
                context = deepcopy(decision.context)
                context.update(title="Updated title", finished_on=None)
                self.workflow.store.update(decision, context, actor="test", action="changed_presentation")
                self.native.scale = second
                self.answer("r4")
                self.assertEqual(len(self.native.reviews), count)
                self.assertEqual(self.native.reviews[-1]["rating"], "80")
                self.assertIn("Movie: Example (2026)", self.native.reviews[-1]["textOriginal"])
                self.assertEqual(self.storage.viewing_followups(), ())

    def test_uncertain_legacy_marker_is_recovered_after_scale_change(self):
        self.create_question()
        self.answer("yes")
        decision = self.pending()
        context = deepcopy(decision.context)
        context["operation"] = {"score": 8, "state": "started"}
        self.workflow.store.update(decision, context, actor="test", action="old_operation")
        marker = f"Curatarr viewing {context['source']}:{context['event_id']}"
        self.native.reviews.append({"id": "legacy-review", "rating": "80", "textOriginal": marker,
                                    "visibility": "PRIVATE", "postedBy": {"id": "household"}})
        self.native.scale = "OUT_OF_FIVE"
        self.answer("r8")
        self.assertEqual(len(self.native.reviews), 1)
        self.assertEqual(self.native.reviews[0]["textOriginal"], marker)
        self.assertEqual(self.native.events[-1]["reviewId"], "legacy-review")
        self.assertFalse(any("mutation CuratarrViewingRating" in q for q, _ in self.native.calls))

    def test_new_review_is_readable_and_reference_is_specific_to_each_viewing(self):
        self.native.scale = "OUT_OF_FIVE"
        self.workflow.activate(-100, "supergroup")
        for event_id in ("first", "second"):
            self.native.events.append(self.native.event(event_id))
            self.workflow.tick()
            self.answer("yes")
            self.answer("pick4")
            self.answer("confirm4")
        texts = [r["textOriginal"] for r in self.native.reviews]
        self.assertNotEqual(texts[0], texts[1])
        for text in texts:
            self.assertIn("Household viewing confirmed through Curatarr.", text)
            self.assertIn("Movie: Example (2026)", text)
            self.assertIn("Viewing date: 2026-10-22", text)
            self.assertRegex(text, r"Reference: CV-[a-f0-9]{24}$")
            self.assertNotIn(self.workflow.binding()["source"], text)

    def test_native_scale_change_between_validation_and_write_is_not_falsely_verified(self):
        self.native.scale = "OUT_OF_FIVE"
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("race"))
        self.workflow.tick()
        self.answer("yes")
        self.answer("pick4")
        self.native.scale_before_create = "OUT_OF_HUNDRED"
        with self.assertRaises(AdapterError):
            self.answer("confirm4")
        self.assertIsNone(self.native.events[-1]["reviewId"])
        self.assertEqual(self.pending().context["questions"], ["rating"])
        with self.assertRaises(AdapterError):
            self.answer("r4")
        self.assertEqual(len(self.native.reviews), 1)

    def test_five_point_account_accepts_existing_normalized_review_without_overwrite(self):
        self.native.scale = "OUT_OF_FIVE"
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("existing"))
        self.workflow.tick()
        self.native.reviews.append({"id": "manual", "rating": "85", "textOriginal": "Keep this review",
                                    "postedBy": {"id": "household"}})
        self.native.events[-1]["reviewId"] = "manual"
        prompt = self.answer("yes")
        self.assertIn("4.25/5", str(prompt))
        self.answer("existing")
        self.assertEqual(self.native.reviews[0]["textOriginal"], "Keep this review")
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))

    def test_readable_review_does_not_invent_a_missing_viewing_date(self):
        self.assertIn("Viewing date: Not recorded", viewing_review_text({
            "title": "Example", "source": "household", "event_id": "new", "finished_on": None}))

    def test_multiple_watches_have_independent_questions_and_reviews(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.extend([self.native.event("first"), self.native.event("second")])
        self.workflow.tick()
        first, second = self.storage.viewing_followups()
        self.assertNotEqual(first.context["event_id"], second.context["event_id"])
        self.assertNotEqual(first.context["delivery"]["message_id"], second.context["delivery"]["message_id"])
        for question, score in ((first, 4), (second, 3)):
            self.answer("yes", decision=question)
            self.answer(f"pick{score}", decision=question)
        self.assertEqual(self.native.reviews, [])
        self.answer("confirm4", decision=first)
        self.assertEqual(len(self.storage.viewing_followups()), 1)
        self.answer("confirm3", decision=second)
        linked = {e["id"]: e["reviewId"] for e in self.native.events if e["id"] != "old"}
        self.assertNotEqual(linked["first"], linked["second"])
        self.assertEqual(len(self.native.events), 3)
        self.assertEqual(self.storage.viewing_followups(), ())

    def test_poster_uses_exact_tmdb_id_and_survives_prompt_updates(self):
        metadata = Mock()
        metadata.movie_poster.return_value = "https://image.tmdb.org/t/p/w500/example.jpg"
        self.workflow.metadata = metadata
        question = self.create_question()
        metadata.movie_poster.assert_called_once_with("1")
        self.assertEqual(self.sent[0]["photo"], metadata.movie_poster.return_value)
        self.assertIn("2026-10-22", self.sent[0]["caption"])
        self.assertEqual(self.answer("yes").photo_url, metadata.movie_poster.return_value)
        self.answer("decline")
        self.assertEqual(self.workflow.prompt(self.storage.get_pending_decision(question.decision_id)).photo_url,
                         metadata.movie_poster.return_value)

    def test_poster_lookup_failure_does_not_block_delivery(self):
        self.workflow.metadata = Mock()
        self.workflow.metadata.movie_poster.side_effect = AdapterError("Poster unavailable")
        self.create_question()
        self.assertIn("text", self.sent[0])
        self.assertEqual(self.pending().context["delivery"]["state"], "sent")

    def test_photo_callback_edits_original_media_without_replacing_message(self):
        self.workflow.metadata = Mock()
        self.workflow.metadata.movie_poster.return_value = "https://image.tmdb.org/t/p/w500/example.jpg"
        question = self.create_question()
        client = Mock(wraps=self.client)
        bot = TelegramBot(client, Mock(), Mock(), self.storage, frozenset({1}), followups=self.workflow)
        message = {"message_id": 1, "chat": {"id": -100, "type": "supergroup"},
                   "photo": [{"file_id": "telegram-cached-poster"}]}
        bot.handle_update({"callback_query": {"id": "cb", "from": {"id": 1},
                                              "data": f"view:{question.decision_id}:yes", "message": message}})
        args, kwargs = client.send_prompt.call_args
        self.assertEqual(args[1].photo_url, "telegram-cached-poster")
        self.assertTrue(kwargs["current_has_photo"])
        self.assertFalse(kwargs["replace_missing"])

    def test_text_fallback_callback_never_creates_a_replacement_photo(self):
        self.workflow.metadata = Mock()
        self.workflow.metadata.movie_poster.return_value = "https://image.tmdb.org/t/p/w500/example.jpg"
        question = self.create_question()
        client = Mock(wraps=self.client)
        bot = TelegramBot(client, Mock(), Mock(), self.storage, frozenset({1}), followups=self.workflow)
        message = {"message_id": 1, "chat": {"id": -100, "type": "supergroup"}}
        bot.handle_update({"callback_query": {"id": "cb", "from": {"id": 1},
                                              "data": f"view:{question.decision_id}:yes", "message": message}})
        args, kwargs = client.send_prompt.call_args
        self.assertIsNone(args[1].photo_url)
        self.assertFalse(kwargs["current_has_photo"])

    def test_zero_and_ten_ratings_are_not_treated_as_empty(self):
        for score in (0, 10):
            with self.subTest(score=score):
                question = self.create_question() if score == 0 else None
                if score == 10:
                    self.native.events.append(self.native.event("another"))
                    self.workflow.tick()
                    decision = self.pending()
                    context = deepcopy(decision.context)
                    context["rating_scale"] = 10
                    self.workflow.store.update(decision, context, actor="test", action="legacy_fixture")
                self.answer("yes")
                self.answer(f"r{score}")
                self.assertEqual(self.native.reviews[-1]["rating"], str(score * 10))

    def test_uncertain_review_create_recovered_without_second_create(self):
        self.create_question()
        self.answer("yes")
        self.native.fail_after_create = True
        with self.assertRaises(AdapterError):
            self.answer("r7")
        self.assertEqual(len(self.native.reviews), 1)
        self.assertEqual(self.pending().context["questions"], ["rating"])
        self.answer("r7")
        self.assertEqual(len(self.native.reviews), 1)
        self.assertEqual(self.storage.viewing_followups(), ())

    def test_uncertain_link_recovered_without_duplicate_review(self):
        self.create_question()
        self.answer("yes")
        self.native.fail_after_link = True
        with self.assertRaises(AdapterError):
            self.answer("r6")
        self.answer("r6")
        self.assertEqual(len(self.native.reviews), 1)

    def test_different_score_after_uncertain_write_is_blocked(self):
        self.create_question()
        self.answer("yes")
        self.native.fail_after_create = True
        with self.assertRaises(AdapterError):
            self.answer("r7")
        with self.assertRaises(ValueError):
            self.answer("r9")
        self.assertEqual(len(self.native.reviews), 1)

    def test_dispute_retains_question_and_never_deletes_watch(self):
        self.create_question()
        self.answer("no")
        self.assertTrue(self.pending().context["disputed"])
        self.assertEqual(len(self.native.events), 2)
        self.assertEqual(self.answer("r5").buttons, ())
        self.assertEqual(self.answer("yes").buttons, ())
        self.assertFalse(self.pending().context["confirmed"])
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))

    def test_wrong_chat_forwarded_message_and_rating_before_confirmation_rejected(self):
        self.create_question()
        for overrides in ({"chat_id": -200}, {"message_id": 999}):
            with self.assertRaises(ValueError):
                self.answer("yes", **overrides)
        with self.assertRaises(ValueError):
            self.answer("r8")

    def test_changed_identity_or_removed_event_blocks_answers(self):
        self.create_question()
        self.native.metadata["identifier"] = "999"
        with self.assertRaises(AdapterError):
            self.answer("yes")
        self.native.metadata["identifier"] = "1"
        self.native.events.pop()
        with self.assertRaises(AdapterError):
            self.answer("yes")

    def test_existing_review_is_preserved_and_can_be_accepted(self):
        self.create_question()
        self.native.reviews.append({"id": "existing", "rating": "85", "textOriginal": "Personal review",
                                    "postedBy": {"id": "household"}})
        self.native.events[-1]["reviewId"] = "existing"
        prompt = self.answer("yes")
        self.assertIn("8.5/10", str(prompt))
        with self.assertRaises(ValueError):
            self.answer("r9")
        self.answer("existing")
        self.assertEqual(self.native.reviews[0]["textOriginal"], "Personal review")
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))

    def test_review_added_concurrently_is_not_replaced_by_new_rating_link(self):
        self.create_question()
        self.answer("yes")
        self.native.link_other_after_create = True
        with self.assertRaises(AdapterError):
            self.answer("r8")
        self.assertEqual(self.native.events[-1]["reviewId"], "manual")
        self.assertFalse(any("mutation CuratarrLinkViewingRating" in q for q, _ in self.native.calls))

    def test_explicit_no_rating_resolves_without_provider_writes(self):
        self.create_question()
        self.answer("yes")
        self.answer("decline")
        self.assertEqual(self.storage.viewing_followups(), ())
        self.assertFalse(any("mutation" in q for q, _ in self.native.calls))

    def test_delivery_timeout_retains_question_without_automatic_resend(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("new"))
        self.fail_send = True
        with self.assertRaises(AdapterError):
            self.workflow.tick()
        self.fail_send = False
        self.workflow.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.pending().context["delivery"]["state"], "uncertain")

    def test_real_callback_recovers_uncertain_delivery_without_resend(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.append(self.native.event("new"))
        self.fail_send = True
        with self.assertRaises(AdapterError):
            self.workflow.tick()
        self.fail_send = False
        self.answer("yes", message_id=1)
        self.assertTrue(self.pending().context["confirmed"])
        self.assertTrue(self.pending().context["delivery"]["recovered_by_callback"])
        self.assertEqual(len(self.sent), 1)

    def test_transition_from_existing_in_progress_to_completed_opens_question(self):
        progressing = self.native.event("progress")
        progressing["state"] = "IN_PROGRESS"
        progressing["progress"] = "50"
        self.native.events.append(progressing)
        self.workflow.activate(-100, "supergroup")
        progressing.update(state="COMPLETED", progress="100")
        self.workflow.tick()
        self.assertEqual(self.pending().context["event_id"], "progress")

    def test_detected_delivery_volume_is_bounded_without_discarding_questions(self):
        self.workflow.activate(-100, "supergroup")
        self.native.events.extend(self.native.event(str(i)) for i in range(8))
        self.workflow.tick()
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(len(self.storage.viewing_followups()), 8)
        self.workflow.tick()
        self.assertEqual(len(self.sent), 8)

    def test_native_review_failure_does_not_allow_decline_to_hide_pending_write(self):
        self.create_question()
        self.answer("yes")
        self.native.fail_after_create = True
        with self.assertRaises(AdapterError):
            self.answer("r5")
        with self.assertRaises(ValueError):
            self.answer("decline")
        self.assertEqual(self.pending().status, "open")

    def test_cas_prevents_two_respondents_overwriting_each_other(self):
        decision = self.create_question()
        self.answer("yes", user_id=2)
        with self.assertRaises(StorageError):
            self.workflow.store.update(decision, decision.context, actor="other", action="stale")

    def test_group_callbacks_are_authorized_but_group_search_remains_disabled(self):
        question = self.create_question()
        search, callbacks = Mock(), Mock()
        bot = TelegramBot(self.client, search, callbacks, self.storage, frozenset({1, 2}),
                          admins=frozenset({1}), followups=self.workflow)
        message = {"message_id": 1, "chat": {"id": -100, "type": "supergroup"}}
        callback = {"callback_query": {"id": "cb", "from": {"id": 99}, "data": f"view:{question.decision_id}:yes", "message": message}}
        bot.handle_update(callback)
        self.assertFalse(self.pending().context["confirmed"])
        callback["callback_query"]["from"]["id"] = 2
        bot.handle_update(callback)
        self.assertTrue(self.pending().context["confirmed"])
        bot.handle_update({"message": {**message, "from": {"id": 1}, "text": "/movie test"}})
        search.search_choices.assert_not_called()

    def test_private_test_activation_is_explicit_and_admin_only(self):
        bot = TelegramBot(self.client, Mock(), Mock(), self.storage, frozenset({1, 2}),
                          admins=frozenset({1}), followups=self.workflow)
        bot.handle_update({"message": {"message_id": 10, "from": {"id": 2}, "chat": {"id": 2, "type": "private"}, "text": "/followups_here"}})
        self.assertIsNone(self.workflow.binding())
        bot.handle_update({"message": {"message_id": 11, "from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "text": "/followups_here"}})
        self.assertEqual(self.workflow.binding()["chat_id"], 1)
        self.assertIn("private test chat", self.sent[-1]["text"])
        self.assertEqual(self.storage.viewing_followups(), ())
        bot.handle_update({"message": {"message_id": 12, "from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "text": "/followups_stop"}})
        self.assertFalse(self.workflow.binding()["enabled"])

    def test_rebinding_never_silently_moves_to_another_chat(self):
        self.workflow.activate(-100, "supergroup")
        with self.assertRaises(AdapterError):
            self.workflow.activate(-200, "supergroup")
        self.assertEqual(self.workflow.binding()["chat_id"], -100)

    def test_group_command_mentions_must_name_this_bot(self):
        bot = TelegramBot(self.client, Mock(), Mock(), self.storage, frozenset({1}),
                          admins=frozenset({1}), followups=self.workflow)
        message = {"message_id": 10, "from": {"id": 1}, "chat": {"id": -100, "type": "supergroup"}}
        bot.handle_update({"message": {**message, "text": "/followups_here@another_bot"}})
        self.assertIsNone(self.workflow.binding())
        bot.handle_update({"message": {**message, "text": "/followups_here@curatarr_test"}})
        self.assertEqual(self.workflow.binding()["chat_id"], -100)

    def test_rating_scale_change_pauses_without_new_writes(self):
        self.create_question()
        self.answer("yes")
        self.native.scale = "OUT_OF_TEN"
        with self.assertRaises(ValueError):
            self.answer("r5")
        with self.assertRaises(AdapterError):
            self.workflow.tick()

    def test_viewing_capabilities_reflect_active_binding_and_partial_answers(self):
        self.create_question()
        service = ViewingFollowupService(self.storage, self.reader, self.workflow.bot_key)
        data = service.review(1)
        self.assertTrue(data["delivery_enabled"])
        self.answer("yes")
        self.assertEqual(service.review(1)["movie"]["questions"], ["How do you rate it?"])
        self.answer("r8")
        self.assertEqual(service.review(1)["movie_count"], 0)

    def test_persistent_baseline_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.sqlite"
            first = Storage(path)
            first.initialize()
            account, events = self.adapter.scan()
            ViewingStore(first).observe(account, events)
            first.close()
            second = Storage(path)
            second.initialize()
            self.addCleanup(second.close)
            self.native.events.append(self.native.event("new"))
            account, events = self.adapter.scan()
            report = ViewingStore(second).observe(account, events)
            self.assertFalse(report["baseline_created"])
            self.assertEqual(len(report["created"]), 1)
            self.assertEqual(ViewingStore(second).observe(account, events)["created"], [])
