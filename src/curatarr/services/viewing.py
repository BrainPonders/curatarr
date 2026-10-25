"""Read-only safeguard for genuine unanswered household viewing follow-ups."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

from curatarr.adapters import AdapterError, RyotAdapter
from curatarr.domain import MediaItem, MediaType, parse_identity
from curatarr.storage import Storage
from curatarr.storage.viewing import ViewingStore


QUESTIONS = {"watched": "Have you watched it?", "rating": "How do you rate it?",
             "retention": "Keep or Archive?"}


def visible_to(decision, user_id=None):
    context = decision.context
    if context.get("declined_all"):
        return False
    if user_id is None:
        return True
    if context.get("confirmed_by") is not None:
        return context["confirmed_by"] == user_id
    if context.get("participants") is not None and user_id not in context["participants"]:
        return False
    return context.get("responses", {}).get(str(user_id), {}).get("answer") != "no"


def pending_movie_count(storage, user_id=None, excluded=()):
    """Count unique movies still needing this respondent's input."""
    return len({d.media_key for d in storage.viewing_followups()
                if d.decision_id not in excluded and visible_to(d, user_id) and isinstance(d.context.get("questions"), list)
                and any(q in {"watched", "rating"} for q in d.context["questions"] if isinstance(q, str))})


def queue_version(storage, user_id, excluded=()):
    return hashlib.sha256(json.dumps([(d.decision_id, d.context) for d in storage.viewing_followups()
                                     if d.decision_id not in excluded and visible_to(d, user_id)], sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ViewingFollowupService:
    storage: Storage
    ryot: RyotAdapter
    bot_key: str | None = None
    excluded: frozenset = frozenset()

    def review(self, user_id: int, action="open", *, expected_cursor=None) -> dict:
        if action not in {"open", "next", "previous", "refresh"}:
            raise ValueError("Unknown viewing navigation action.")
        progress = self.storage.maintenance_progress(user_id, source="viewing_progress")
        if action in {"next", "previous"} and expected_cursor != progress["cursor"]:
            raise ValueError("Viewing position changed.")
        groups = {}
        for decision in self.storage.viewing_followups():
            if decision.decision_id in self.excluded or not visible_to(decision, user_id):
                continue
            groups.setdefault(decision.media_key, []).append(decision)
        # Previously postponed questions rejoin the single outstanding list.
        progress.update(skipped=[], parked=False)
        keys = sorted(groups)
        position = keys.index(progress["cursor"]) if progress["cursor"] in keys else 0
        if keys and action in {"next", "previous"} and progress["cursor"] in keys:
            position += 1 if action == "next" else -1
        key = keys[position % len(keys)] if keys else None
        progress["cursor"] = key
        self.storage.save_maintenance_progress(user_id, progress, source="viewing_progress")
        binding = ViewingStore(self.storage).binding()
        enabled = bool(binding and binding.get("enabled") and binding.get("bot_key") == self.bot_key)
        result = {"movie_count": len(groups), "pending_movie_count": pending_movie_count(self.storage, user_id, self.excluded),
                  "queue_version": queue_version(self.storage, user_id, self.excluded), "progress": progress,
                  "movie": None, "validated": False, "total": len(keys),
                  "position": keys.index(key) + 1 if key else 0, "read_only": True,
                  "delivery_enabled": enabled, "updates_enabled": False, "telegram_updates_enabled": enabled,
                  "notice": ("Confirm the recorded viewing, then add a rating here or in Telegram."
                             if enabled else "Viewing inboxs are not enabled yet.")}
        if key is None:
            result["empty_message"] = "You're all caught up."
            return result
        try:
            identity = parse_identity(key).identity
            local = all(d.context.get('direct_candidate') for d in groups[key])
            if identity.namespace not in ({'tmdb'} if local else {'ryot'}):
                raise ValueError("Viewing follow-up needs an exact movie identity.")
            item = self.ryot.get_item(identity).value
            if item is None and local:
                context = groups[key][0].context
                item = MediaItem(MediaType.MOVIE, context['title'], context.get('year'), frozenset({identity}))
            if item is None or item.media_type is not MediaType.MOVIE or identity not in item.identities:
                raise ValueError("Viewing identity could not be confirmed.")
            questions = set()
            for decision in groups[key]:
                requested = decision.context.get("questions") if isinstance(decision.context, dict) else None
                if (not isinstance(requested, list) or not requested
                        or any(not isinstance(q, str) or q not in QUESTIONS for q in requested)):
                    raise ValueError("Follow-up questions could not be confirmed.")
                questions.update(requested)
        except (AdapterError, ValueError):
            result["notice"] = "This viewing follow-up could not be validated. Nothing was changed; retry or review it outside the Mini App."
            return result
        result.update(validated=True, checked_at=datetime.now(timezone.utc).isoformat(), movie={
            "key": key, "title": item.title, "year": item.year,
            "identities": [str(i) for i in sorted(item.identities)],
            "watch_count": item.completed_watch_count, "watched": item.is_watched,
            "last_watched_date": item.last_watched_date,
            "questions": [label for question, label in QUESTIONS.items() if question in questions],
            "reopen_decision_id": next((d.decision_id for d in groups[key]
                if d.context.get("workflow") == "viewing_v1" and not d.context.get("declined_all")
                and d.context.get("delivery", {}).get("state") in {"removed", "uncertain_removal"}), None)})
        if any(d.context.get("disputed") for d in groups[key]):
            result["notice"] = "Previously flagged. Please answer for yourself; a No preserves household history."
        elif any(d.context.get("delivery", {}).get("state") in {"sending", "uncertain"} for d in groups[key]):
            result["notice"] = "Telegram delivery is unconfirmed. The question was kept; delivery will not be repeated automatically. Review externally."
        elif any(d.context.get("operation") and d.context["operation"].get("state") != "verified" for d in groups[key]):
            result["notice"] = "A rating update is unconfirmed. Retry the same Telegram score to reconcile, or review externally."
        return result
