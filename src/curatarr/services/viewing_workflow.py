"""Persisted shared viewing answers for Telegram and Mini App check-ins."""

from copy import deepcopy
import hashlib
import json
import time

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.viewing import SUPPORTED_REVIEW_SCALES, viewing_review_text, ViewingEventMissing
from curatarr.services.telegram import TelegramButton, TelegramPrompt
from curatarr.storage import StorageError
from curatarr.storage.viewing import ViewingStore
from curatarr.services.settings import InstallationSettings


class ViewingResponseError(ValueError):
    """Safe, user-facing rejection of an unavailable viewing action."""


def bot_key(token):
    return hashlib.sha256(token.encode()).hexdigest()[:24]


class ViewingWorkflow:
    def __init__(self, storage, adapter, client, metadata=None, *, participants=None, direct=None):
        self.storage, self.adapter, self.client = storage, adapter, client
        self.metadata = metadata
        self.direct = direct
        self.store = ViewingStore(storage)
        self.bot_key = bot_key(client.token)
        self.participants = tuple(sorted(set(participants))) if participants is not None else None

    def household_mode(self, context):
        return self.participants is not None or "participants" in context

    def adopt_household_policy(self):
        """Upgrade outstanding questions without inventing historical votes."""
        binding = self.binding()
        if self.participants is None or not binding:
            return
        for decision in self.storage.viewing_followups():
            if (decision.context.get("workflow") != "viewing_v1" or "participants" in decision.context
                    or decision.context.get("source") != binding["source"]):
                continue
            context = deepcopy(decision.context)
            context.update(participants=list(self.participants), responses={})
            if context.get("confirmed"):
                row = self.storage.connection.execute(
                    "SELECT actor FROM audit_log WHERE action='viewing_confirmed' "
                    "AND json_extract(details_json,'$.decision_id')=? ORDER BY id DESC LIMIT 1",
                    (decision.decision_id,)).fetchone()
                if row and row[0].rsplit(":", 1)[-1].isdigit():
                    context["confirmed_by"] = int(row[0].rsplit(":", 1)[-1])
            try:
                self.store.update(decision, context, actor="system", action="viewing_household_policy_adopted")
            except StorageError:
                continue  # Another response/migrator won; never overwrite it.

    def binding(self):
        binding = self.store.binding()
        return binding if binding and binding.get("bot_key") == self.bot_key else None

    def activate(self, chat_id, chat_type):
        current = self.binding()
        if current and current["chat_id"] != chat_id:
            raise AdapterError("A different destination is already bound. Review the migration before moving it.")
        chat = self.client.call("getChat", {"chat_id": chat_id})
        if not isinstance(chat, dict) or chat.get("id") != chat_id or chat.get("type") != chat_type:
            raise AdapterError("The Telegram destination could not be verified.")
        account, events = self.adapter.scan()
        if account["scale"] not in SUPPORTED_REVIEW_SCALES:
            raise AdapterError("Ryot rating scale must be verified before enabling viewing updates.")
        report = self.store.observe(account, events, participants=self.participants)
        self.store.bind({"bot_key": self.bot_key, "chat_id": chat_id, "chat_type": chat_type,
                         "source": account["source"], "enabled": True})
        self.storage.append_audit_log(actor="admin", action="viewing_destination_enabled",
                                      details={"chat_type": chat_type, "baseline_created": report["baseline_created"]})
        baseline_notice = ("Existing history was baselined without historical prompts. " if report["baseline_created"] else
                           "The existing baseline and outstanding questions were retained. ")
        return TelegramPrompt("Viewing follow-ups enabled here. " + baseline_notice +
                              f"New completed viewings will ask for {InstallationSettings(self.storage).display_name()} confirmation and a rating from 1 to 5. "
                              + ("This is a private test chat, not a shared channel. " if chat_type == "private" else "")
                              + "No watch records or ratings were changed. Keep/Archive execution is not enabled.")

    def tick(self):
        binding = self.binding()
        if not binding or not binding.get("enabled"):
            return
        self.adopt_household_policy()
        if self.direct is not None:
            self.direct.tick()
        self.refresh_web_prompts()
        account, events = self.adapter.scan()
        if account["source"] != binding["source"] or account["scale"] not in SUPPORTED_REVIEW_SCALES:
            raise AdapterError("Viewing account or rating scale changed.")
        self.store.observe(account, events, participants=self.participants)
        # Bound delivery volume prevents an import from flooding the test chat.
        delivered = 0
        for decision in self.storage.viewing_followups():
            if self.store.reset_pending(decision.media_key):
                continue
            context = decision.context
            if (context.get("workflow") == "viewing_v1" and context.get("source") == binding["source"]
                    and context.get("delivery", {}).get("state") == "sent"
                    and ((context.get("disputed") and not context.get("dispute_ui_updated"))
                         or context["delivery"].get("presentation_version") != (3 if self.household_mode(context) else 2))):
                delivery = context["delivery"]
                has_photo = delivery.get("has_photo", bool(context.get("poster_url")))
                try:
                    self.adapter.validate(context)
                    self.client.send_prompt(binding["chat_id"], self.prompt(decision),
                                            message_id=delivery["message_id"], current_has_photo=has_photo,
                                            replace_missing=False)
                except AdapterError:
                    continue  # Never replace a missing message or guess changed viewing facts.
                updated = deepcopy(context)
                updated["delivery"]["presentation_version"] = 3 if self.household_mode(context) else 2
                if context.get("disputed"):
                    updated["dispute_ui_updated"] = True
                self.store.update(decision, updated, actor="system", action="viewing_prompt_presented")
                delivered += 1
                if delivered >= 5:
                    break
                continue
            if (context.get("workflow") != "viewing_v1" or context.get("source") != binding["source"]
                    or context.get("delivery", {}).get("state") != "pending"):
                continue
            self.adapter.validate(context)
            next_context = deepcopy(context)
            if self.metadata is not None:
                tmdb_ids = [i.split(":", 1)[1] for i in context["identities"] if i.startswith("tmdb:")]
                if len(tmdb_ids) == 1:
                    try:
                        next_context["poster_url"] = self.metadata.movie_poster(tmdb_ids[0])
                    except AdapterError:
                        # Artwork is optional and cannot hold up a viewing question.
                        next_context["poster_url"] = None
            next_context["delivery"] = {"state": "sending", "chat_id": binding["chat_id"]}
            try:
                claimed = self.store.update(decision, next_context, actor="system", action="viewing_prompt_attempted")
            except StorageError:
                continue
            try:
                result = self.client.send_prompt(binding["chat_id"], self.prompt(claimed))
                if not isinstance(result, dict) or type(result.get("message_id")) is not int:
                    raise AdapterError("Message delivery was not confirmed.")
                next_context["delivery"].update(state="sent", message_id=result["message_id"],
                                                has_photo=bool(result.get("photo")),
                                                presentation_version=3 if self.household_mode(context) else 2,
                                                sent_at=result.get("date") if type(result.get("date")) is int else time.time())
                if not result.get("photo"):
                    next_context["poster_url"] = None
            except AdapterError:
                next_context["delivery"]["state"] = "uncertain"
                self.store.update(claimed, next_context, actor="system", action="viewing_prompt_unconfirmed")
                # Telegram sends are not idempotent. Do not blindly resend after a timeout.
                raise
            self.store.update(claimed, next_context, actor="system", action="viewing_prompt_delivered")
            delivered += 1
            if delivered >= 5:
                break

    def prompt(self, decision):
        context = decision.context
        title = str(context["title"])[:240]
        year = context.get("year")
        household = self.household_mode(context)
        disputed = context.get("declined_all") or (context.get("disputed") and not household)
        banner = ("VIEWING HISTORY CLEARED" if context.get("closed_reason") == "history_cleared" else "REQUEST CLOSED" if context.get("closed_reason") == "event_missing" else "✅ VIEWING CONFIRMED" if decision.status != "open" else "⚠️ VIEWING DECLINED" if context.get("declined_all") else "⚠️ VIEWING DISPUTED" if disputed
                  else "⭐ RATING REQUEST" if context.get("confirmed") else "👀 WATCH CONFIRMATION REQUEST")
        lines = [banner + "\n━━━━━━━━━━━━━━━━━━", f"{title}{f' ({year})' if year else ''}",
                 f"{InstallationSettings(self.storage).display_name()} viewing"]
        lines.append("Viewing date: " + (str(context["finished_on"]) if context.get("finished_on") else "Not recorded"))
        buttons = []
        scale = context.get("rating_scale", 10)
        if context.get("closed_reason") == "history_cleared":
            lines.append("An admin cleared this movie's viewing history in Ryot. Ratings and reviews were retained.")
        elif context.get("closed_reason") == "event_missing":
            lines.append("The original viewing is no longer in Ryot. An admin cleared this stale request; no watch or rating was created.")
        elif decision.status != "open":
            lines.append("Viewing confirmed; " + ("no rating requested." if context.get("rating_declined") else
                                                  f"rating {context.get('rating')}/{scale} verified in Ryot."))
        elif context.get("declined_all"):
            lines.extend(["Everyone answered No. This record is in Admin → Declined viewings.",
                          "Ryot history has been preserved for admin review."])
        elif disputed:
            lines.extend(["Viewing disputed - review required.",
                          "The viewing remains recorded in Ryot. Curatarr did not add a rating or remove history.",
                          "This item remains in Viewing inbox for review. Removing the Ryot viewing requires a separate explicit action."])
        elif context.get('direct_candidate') and context.get('confirmed_by') is not None and not context.get('native_linked'):
            lines.append("Your confirmation is saved. The Ryot update awaits verification; rating becomes available afterwards.")
            buttons = [TelegramButton("Check saved confirmation", f"view:{decision.decision_id}:retrywatch", 0)]
        elif not context.get("confirmed"):
            lines.extend(["Jellyfin reported viewing activity." if context.get('direct_candidate') else "Ryot recorded a completed viewing.", "Did you watch this?" if household else "Is this watched status correct?"])
            if household and context.get("responses"):
                lines.append("Awaiting the remaining household members. A No never removes shared history.")
            buttons = [TelegramButton("Yes, I watched this" if household else "Confirm watched", f"view:{decision.decision_id}:yes", 0),
                       TelegramButton("No, I did not watch this" if household else "Not correct", f"view:{decision.decision_id}:no", 1 if household else 0)]
        else:
            lines.extend(["Watched status confirmed.", f"How do you rate it? ({'1-5' if scale == 5 else '0-10'})"])
            if household:
                lines.append("Rating is optional and available to the person who confirmed the viewing.")
            score = context.get("selected_rating", 5)
            operation = context.get("operation")
            if operation:
                buttons = [TelegramButton(f"Retry rating ({operation['score']}/{scale})",
                                          f"view:{decision.decision_id}:r{operation['score']}", 0)]
            elif scale == 5:
                selected = context.get("selected_rating")
                buttons = [TelegramButton(f"[{value}]" if value == selected else str(value),
                                          f"view:{decision.decision_id}:pick{value}", 0) for value in range(1, 6)]
                if selected is not None:
                    buttons.append(TelegramButton(f"Confirm rating ({selected}/5)",
                                                  f"view:{decision.decision_id}:confirm{selected}", 1))
            else:
                buttons = [TelegramButton("-", f"view:{decision.decision_id}:s{max(0, score - 1)}", 0),
                           TelegramButton(f"{score} / 10", f"view:{decision.decision_id}:preview", 0),
                           TelegramButton("+", f"view:{decision.decision_id}:s{min(10, score + 1)}", 0),
                           TelegramButton(f"Save rating ({score}/10)", f"view:{decision.decision_id}:save{score}", 1)]
            if context.get("existing_rating") is not None:
                buttons.append(TelegramButton(f"Use recorded rating ({context['existing_rating']}/{scale})",
                                              f"view:{decision.decision_id}:existing", 2))
            if not operation:
                buttons.append(TelegramButton("No rating", f"view:{decision.decision_id}:decline", 3))
        return TelegramPrompt("\n\n".join(lines), tuple(buttons), decision.decision_id, context.get("poster_url"))

    def respond(self, data, *, user_id, chat_id, message_id, user_name=None):
        parts = data.split(":")
        if (len(parts) != 3 or parts[0] != "view" or not parts[1].isascii()
                or not parts[1].isdigit() or len(parts[1]) > 18):
            raise ViewingResponseError("Invalid viewing button.")
        decision = self.storage.get_pending_decision(int(parts[1]))
        binding = self.binding()
        if (decision is None or decision.decision_type != "viewing_followup" or decision.audience != "shared_household"
                or decision.context.get("workflow") != "viewing_v1" or not binding or not binding.get("enabled")
                or binding["chat_id"] != chat_id or binding["source"] != decision.context.get("source")):
            raise ViewingResponseError("This viewing question is not available in this chat.")
        context = deepcopy(decision.context)
        delivery = context.get("delivery", {})
        if delivery.get("chat_id") != chat_id or type(message_id) is not int or message_id <= 0:
            raise ViewingResponseError("This is not the recorded viewing prompt.")
        if delivery.get("message_id") is None and delivery.get("state") in {"sending", "uncertain"}:
            # A real Telegram callback proves that a previously uncertain send
            # arrived. Recover its message ID without sending another question.
            self.adapter.validate(context)
            delivery.update(state="sent", message_id=message_id, recovered_by_callback=True)
            decision = self.store.update(decision, context, actor=f"telegram:user:{user_id}",
                                         action="viewing_delivery_recovered")
        elif delivery.get("message_id") != message_id:
            raise ViewingResponseError("This is not the recorded viewing prompt.")
        return self._respond(decision, parts[2], actor=f"telegram:user:{user_id}", user_name=user_name)

    @staticmethod
    def version(decision):
        return hashlib.sha256(json.dumps([decision.status, decision.context], sort_keys=True).encode()).hexdigest()

    def web_question(self, decision, *, user_id=None, admin_review=False):
        """Expose the same validated actions without fabricating a Telegram callback."""
        self._web_available(decision)
        if self.store.reset_pending(decision.media_key):
            raise ViewingResponseError("A history reset is pending. Admin review is required.")
        self.adapter.validate(decision.context)
        prompt = self.prompt(decision)
        if admin_review:
            actions = [{"label": "Confirm household watched", "action": "confirm_declined", "row": 0}]
        else:
            from curatarr.services.viewing import visible_to
            if not visible_to(decision, user_id):
                raise ViewingResponseError("This viewing no longer needs your answer.")
            actions = [{"label": b.label, "action": b.callback_data.rsplit(":", 1)[1], "row": b.row}
                       for b in prompt.buttons]
        return {"decision_id": decision.decision_id, "version": self.version(decision),
                "date": decision.context.get("finished_on"), "disputed": bool(decision.context.get("declined_all") or
                    (decision.context.get("disputed") and not self.household_mode(decision.context))),
                "confirmed": bool(decision.context.get("confirmed")),
                **({'pending_viewing': True, 'notice': 'Your confirmation is saved. The Ryot update awaits verification; rating becomes available afterwards.'}
                   if decision.context.get('direct_candidate') and decision.context.get('confirmed_by') is not None
                   and not decision.context.get('native_linked') else {}),
                "actions": actions}

    def _web_available(self, decision):
        binding = self.binding()
        if (not decision or decision.status != "open" or decision.decision_type != "viewing_followup"
                or decision.audience != "shared_household" or decision.context.get("workflow") != "viewing_v1"
                or not binding or not binding.get("enabled") or binding["source"] != decision.context.get("source")):
            raise ViewingResponseError("This watch check-in is not available. Refresh before continuing.")

    def respond_web(self, decision_id, action, *, media_key, expected_version, user_id, user_name=None, admin_review=False):
        decision = self.storage.get_pending_decision(decision_id)
        self._web_available(decision)
        if decision.media_key != media_key or self.version(decision) != expected_version:
            raise ViewingResponseError("This check-in changed. Refresh before answering.")
        if admin_review and action == "clear_stale":
            if decision.context.get("operation"):
                raise ViewingResponseError("A rating operation needs investigation before this request can be cleared.")
            try:
                self.adapter.validate(decision.context)
            except ViewingEventMissing:
                context = deepcopy(decision.context)
                context.update(questions=[], closed_reason="event_missing")
                context.setdefault("delivery", {})["web_refresh"] = True
                updated = self.store.update(decision, context, actor=f"miniapp:user:{user_id}",
                                            action="viewing_stale_request_cleared", resolve=True)
                self.refresh_web_prompts(decision_id)
                return self.prompt(updated)
            raise ViewingResponseError("The viewing exists again. Refresh before reviewing it.")
        if admin_review and (not decision.context.get("declined_all") or action != "confirm_declined"):
            raise ViewingResponseError("This record is not awaiting declined-viewing review.")
        if not admin_review and action not in {b.callback_data.rsplit(":", 1)[1] for b in self.prompt(decision).buttons}:
            raise ViewingResponseError("This answer is no longer available. Refresh before answering.")
        result = self._respond(decision, "yes" if admin_review else action, actor=f"miniapp:user:{user_id}",
                               user_name=user_name, admin_review=admin_review)
        # A failed message edit cannot roll back an already verified answer.
        self.refresh_web_prompts(decision_id)
        return result

    def refresh_web_prompts(self, decision_id=None):
        """Retry fixed-message presentation updates, including resolved questions."""
        binding = self.binding()
        if not binding or not binding.get("enabled"):
            return
        rows = self.storage.connection.execute(
            "SELECT id FROM pending_decision WHERE decision_type='viewing_followup' "
            "AND audience='shared_household' AND status IN ('open','resolved') "
            "AND json_extract(context_json,'$.delivery.web_refresh')=1 "
            "AND json_extract(context_json,'$.delivery.state')='sent' "
            "AND json_extract(context_json,'$.source')=? ORDER BY id", (binding["source"],)).fetchall()
        for row in rows[:5] if decision_id is None else [r for r in rows if r[0] == decision_id]:
            decision = self.storage.get_pending_decision(row[0])
            delivery = deepcopy(decision.context.get("delivery", {}))
            if (decision.context.get("source") != binding["source"] or delivery.get("state") != "sent"
                    or delivery.get("chat_id") != binding["chat_id"] or type(delivery.get("message_id")) is not int):
                continue
            try:
                try:
                    self.adapter.validate(decision.context)
                except ViewingEventMissing:
                    if decision.context.get("closed_reason") not in {"event_missing", "history_cleared"}:
                        raise
                self.client.send_prompt(binding["chat_id"], self.prompt(decision), message_id=delivery["message_id"],
                                        current_has_photo=delivery.get("has_photo", False), replace_missing=False)
                delivery["web_refresh"] = False
                self.store.update_delivery(decision, delivery, actor="system", action="viewing_web_prompt_refreshed")
            except (AdapterError, StorageError):
                continue

    def _respond(self, decision, action, *, actor, user_name=None, admin_review=False):
        context = deepcopy(decision.context)
        if decision.status != "open":
            return self.prompt(decision)
        if self.store.reset_pending(decision.media_key):
            raise ViewingResponseError("A history reset is pending. Admin review is required.")
        if context.get('direct_candidate') and not context.get('native_linked'):
            if self.direct is None:
                raise ViewingResponseError('Direct viewing is unavailable. Your answer was retained.')
            return self.prompt(self.direct.answer(decision, action, actor=actor, user_name=user_name, admin_review=admin_review))
        household = self.household_mode(context)
        user_id = int(actor.rsplit(":", 1)[1])
        if context.get("disputed") and not household:
            return self.prompt(decision)
        if household:
            if context.get("declined_all") and not admin_review:
                raise ViewingResponseError("This viewing is awaiting admin review.")
            if context.get("confirmed_by") is not None and context["confirmed_by"] != user_id:
                raise ViewingResponseError("This viewing was already confirmed. It no longer needs your answer.")
            context.setdefault("participants", list(self.participants or ()))
            context.setdefault("responses", {})
            if user_id not in context["participants"] and not admin_review:
                raise ViewingResponseError("You are not a respondent for this viewing request.")
        if actor.startswith("miniapp:"):
            context.setdefault("delivery", {})["web_refresh"] = True
        scale = context.get("rating_scale", 10)
        factor = 20 if scale == 5 else 10
        account, event, reviews = self.adapter.validate(context)
        if account["scale"] not in SUPPORTED_REVIEW_SCALES:
            raise ViewingResponseError("Ryot's native rating scale changed; verify the conversion before answering.")
        if action in {"yes", "no"}:
            if context.get("confirmed"):
                return self.prompt(decision)
            if household:
                if context["responses"].get(str(user_id), {}).get("answer") == "no" and action == "no":
                    return self.prompt(decision)
                from datetime import datetime, timezone
                context["responses"][str(user_id)] = {"answer": action, "name": self.respondent_name(user_name, user_id),
                                                     "answered_at": datetime.now(timezone.utc).isoformat()}
                context["declined_all"] = action == "no" and bool(context["participants"]) and all(
                    context["responses"].get(str(p), {}).get("answer") == "no" for p in context["participants"])
                if context.get("disputed"):
                    context["legacy_disputed"] = True
                context["disputed"] = False
                context.setdefault("delivery", {})["web_refresh"] = True
            else:
                context["disputed"] = action == "no"
            if action == "yes":
                context.update(confirmed=True, questions=["rating"])
                if household:
                    context.update(confirmed_by=user_id, declined_all=False)
                existing = self._existing_rating(account, event, reviews)
                context["existing_rating"] = str(existing / factor) if existing is not None else None
            else:
                context["questions"] = ["watched"]
            updated = self.store.update(decision, context, actor=actor,
                                        action="viewing_confirmed" if action == "yes" else
                                        "viewing_declined_all" if context.get("declined_all") else
                                        "viewing_user_declined" if household else "viewing_disputed")
            return self.prompt(updated)
        if not context.get("confirmed"):
            raise ViewingResponseError("Confirm watched status before rating.")
        explicitly_confirmed = False
        if scale == 5:
            if action in {f"pick{score}" for score in range(1, 6)}:
                if context.get("operation"):
                    raise ViewingResponseError("A rating write is pending; retry its recorded score.")
                context["selected_rating"] = int(action[4:])
                return self.prompt(self.store.update(decision, context, actor=actor, action="viewing_rating_selected"))
            if action in {f"confirm{score}" for score in range(1, 6)}:
                score = int(action[7:])
                if score != context.get("selected_rating"):
                    raise ViewingResponseError("The selected rating changed. Review the current score before confirming.")
                explicitly_confirmed = True
                action = f"r{score}"
            if action.startswith(("s", "pick", "confirm")):
                raise ViewingResponseError("Invalid rating button for this question.")
        if action == "preview":
            return self.prompt(decision)
        if action in {f"s{score}" for score in range(11)}:
            if context.get("operation"):
                raise ViewingResponseError("A rating write is pending; retry its recorded score.")
            context["selected_rating"] = int(action[1:])
            return self.prompt(self.store.update(decision, context, actor=actor, action="viewing_rating_selected"))
        if action in {f"save{score}" for score in range(11)}:
            score = int(action[4:])
            if score != context.get("selected_rating", 5):
                raise ViewingResponseError("The selected rating changed. Review the current score before saving.")
            action = f"r{score}"
        if action == "decline":
            if context.get("operation"):
                raise ViewingResponseError("A rating write is pending; verify it before declining.")
            context.update(questions=[], rating_declined=True)
            return self.prompt(self.store.update(decision, context, actor=actor, action="viewing_rating_declined", resolve=True))
        if action == "existing":
            if context.get("operation"):
                raise ViewingResponseError("A rating write is pending; reconcile it first.")
            native_rating = self._existing_rating(account, event, reviews)
            if native_rating is None:
                raise ViewingResponseError("The recorded rating is no longer available.")
            context.update(rating=str(native_rating / factor), review_id=event["reviewId"], questions=[])
            return self.prompt(self.store.update(decision, context, actor=actor, action="viewing_existing_rating_verified", resolve=True))
        if action not in {f"r{score}" for score in range(11)}:
            raise ViewingResponseError("Invalid rating.")
        score = int(action[1:])
        if scale == 5 and (not 1 <= score <= 5 or (not explicitly_confirmed and not context.get("operation"))):
            raise ViewingResponseError("Select a rating from 1 to 5, then confirm it.")
        if account["scale"] not in SUPPORTED_REVIEW_SCALES:
            raise ViewingResponseError("Ryot's native rating scale changed; verify the conversion before writing.")
        existing_rating = self._existing_rating(account, event, reviews)
        if event.get("reviewId") and existing_rating != score * factor:
            raise ViewingResponseError("A review is already linked. Use its recorded rating or update that review outside Curatarr.")
        operation = context.get("operation")
        if operation and operation.get("score") != score:
            raise ViewingResponseError("Another rating attempt is pending. Verify that outcome before changing the score.")
        allow_create = operation is None
        if allow_create:
            if household:
                context["rated_by"] = {"user_id": user_id, "name": self.respondent_name(user_name, user_id)}
            context["operation"] = {"score": score, "state": "started", "review_text": viewing_review_text(
                context, InstallationSettings(self.storage).display_name())}
            decision = self.store.update(decision, context, actor=actor, action="viewing_rating_requested")

        def remember(review_id):
            current = self.storage.get_pending_decision(decision.decision_id)
            if current.status != "open":
                raise StorageError("The question was already answered.")
            update = deepcopy(current.context)
            update["review_id"] = review_id
            self.store.update(current, update, actor=actor, action="viewing_review_identified")

        try:
            review_id = self.adapter.rate(context, score * factor, allow_create=allow_create, remember_review=remember)
        except AdapterError:
            # The operation marker survives failures/restarts. A retry may recover
            # the existing marked review, but must not issue a second create.
            raise AdapterError("Rating outcome is unconfirmed. The question remains open; retry the same score to reconcile, not create again.") from None
        current = self.storage.get_pending_decision(decision.decision_id)
        if current.status != "open":
            return self.prompt(current)
        update = deepcopy(current.context)
        update.update(rating=score, review_id=review_id, questions=[], operation={"score": score, "state": "verified"})
        return self.prompt(self.store.update(current, update, actor=actor, action="viewing_rating_verified", resolve=True))

    @staticmethod
    def respondent_name(name, user_id):
        import unicodedata
        clean = " ".join("".join(c for c in str(name or "") if not unicodedata.category(c).startswith("C")).split())[:80]
        return clean or f"Telegram user {user_id}"

    def reopen(self, decision_id, media_key, *, user_id):
        from curatarr.services.viewing import visible_to
        decision = self.storage.get_pending_decision(decision_id)
        binding = self.binding()
        if (not decision or decision.status != "open" or decision.decision_type != "viewing_followup"
                or decision.audience != "shared_household" or decision.media_key != media_key
                or decision.context.get("workflow") != "viewing_v1" or decision.context.get("declined_all")
                or (decision.context.get("disputed") and not self.household_mode(decision.context))
                or not visible_to(decision, user_id)
                or decision.context.get("delivery", {}).get("state") not in {"removed", "uncertain_removal"}
                or not binding or not binding.get("enabled") or binding["source"] != decision.context.get("source")):
            raise ViewingResponseError("This question is not available for reopening.")
        self.adapter.validate(decision.context)
        self.store.update_delivery(decision, {"state": "pending", "reopened": True},
                                   actor=f"telegram:user:{user_id}", action="viewing_prompt_reopened")

    @staticmethod
    def _existing_rating(account, event, reviews):
        from curatarr.integrations.ryot.viewing import _rating
        matches = [r for r in reviews if r.get("id") == event.get("reviewId") and r["postedBy"].get("id") == account["id"]]
        value = _rating(matches[0].get("rating")) if len(matches) == 1 else None
        return value if value is not None and value.is_finite() and 0 <= value <= 100 else None
