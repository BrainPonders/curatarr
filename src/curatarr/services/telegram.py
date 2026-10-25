"""Telegram-facing command rendering for Curatarr workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from curatarr.domain import MediaIdentity, MediaItem, MediaType
from curatarr.adapters import AdapterError
from curatarr.services.decisions import PersistedDecisionSummary, search_add_state_fingerprint
from curatarr.services.search import SearchDecisionService
from curatarr.storage import PendingDecision, Storage, StorageError
from curatarr.workflows.search_add import DecisionPath, WorkflowAction


@dataclass(frozen=True)
class TelegramButton:
    """Transport-neutral Telegram inline button metadata."""

    label: str
    callback_data: str
    row: int | None = None


@dataclass(frozen=True)
class TelegramPrompt:
    """Transport-neutral Telegram message payload."""

    text: str
    buttons: tuple[TelegramButton, ...] = ()
    pending_decision_id: int | None = None
    photo_url: str | None = None


@dataclass(frozen=True)
class TelegramSearchCommand:
    """Handle Telegram search/add commands without depending on a bot library."""

    search_decisions: SearchDecisionService

    def handle_search(self, query: str, media_type: MediaType, *, audience: str) -> TelegramPrompt:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            return TelegramPrompt(text="Send a movie or series title to search.")

        persisted = self.search_decisions.plan_and_persist_for_first_candidate(
            normalized_query,
            media_type,
            audience=audience,
        )
        if persisted is None:
            return TelegramPrompt(text=f"No {media_type.value} results found for \"{normalized_query}\".")
        return _prompt_from_persisted_decision(persisted)

    def search_choices(self, query: str, media_type: MediaType, *, audience: str) -> TelegramPrompt:
        """Persist the candidate list so choosing a title survives process restarts."""

        query = " ".join(query.split())
        if not query:
            return TelegramPrompt("Send a movie title to search.")
        persistence = self.search_decisions.pending_decision_service
        if persistence is None:
            raise ValueError("Pending decision persistence is not configured.")
        candidates = self.search_decisions.search(query, media_type)[:10]
        if not candidates:
            return TelegramPrompt(f'No {media_type.value} results found for "{query[:200]}".')
        decision = persistence.storage.create_pending_decision(
            media_key=f"search:{media_type.value}",
            decision_type="search_selection",
            audience=audience,
            state_fingerprint="selection",
            context={"candidates": [_candidate_context(item) for item in candidates]},
        )
        return _search_results_prompt(decision, index=0)


@dataclass(frozen=True)
class TelegramCallbackResult:
    """A callback notice and an optional replacement for the existing prompt."""

    notice: str
    prompt: TelegramPrompt | None = None


@dataclass(frozen=True)
class TelegramDecisionCallbacks:
    search_decisions: SearchDecisionService
    storage: Storage
    acquisition: object | None = None

    def handle(self, data: str, *, audience: str) -> TelegramCallbackResult:
        if self.acquisition and data.startswith(("acq:", "acqop:")):
            try:
                return TelegramCallbackResult("", self.acquisition.handle(data, audience))
            except (AdapterError, StorageError, ValueError):
                return TelegramCallbackResult("The request could not be verified. Check its saved status before starting another request.")
        parts = data.split(":")
        if (len(parts) != 3 or parts[0] not in {"select", "decision", "browse", "list"}
                or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 18):
            return TelegramCallbackResult("Invalid button.")
        decision = self.storage.get_pending_decision(int(parts[1]))
        if decision is None or decision.audience != audience:
            return TelegramCallbackResult("This decision is not available to you.")
        if decision.status != "open":
            # A queued old callback may refer to a message already updated with a new decision.
            return TelegramCallbackResult("This decision is already closed.")
        # Legacy decisions have no expiry; require a fresh search rather than trusting old buttons.
        if (decision.expires_at is None or
                datetime.fromisoformat(decision.expires_at).replace(tzinfo=timezone.utc)
                <= datetime.now(timezone.utc)):
            self.storage.close_open_decision(decision.decision_id, "expired")
            return TelegramCallbackResult("This decision has expired.",
                                          TelegramPrompt("This decision has expired. Search again."))
        if parts[0] in {"browse", "list"}:
            candidates = decision.context.get("candidates", [])
            if (decision.decision_type != "search_selection" or not parts[2].isascii()
                    or not parts[2].isdigit() or len(parts[2]) > 2 or int(parts[2]) >= len(candidates)):
                return TelegramCallbackResult("Invalid results page.")
            return TelegramCallbackResult("", _search_results_prompt(
                decision, index=int(parts[2]), list_view=parts[0] == "list",
            ))
        if parts[0] == "select":
            return self._select(decision, parts[2], audience)
        if decision.decision_type == "search_selection":
            return TelegramCallbackResult("Invalid action for this decision.")
        try:
            action = WorkflowAction(parts[2])
        except ValueError:
            return TelegramCallbackResult("Invalid action.")
        if action.value not in decision.context.get("allowed_actions", []):
            return TelegramCallbackResult("This action was not offered.")

        candidate = _candidate_from_context(decision.context["candidate"])
        summary = self.search_decisions.plan_for_candidate(candidate)
        if search_add_state_fingerprint(summary) != decision.state_fingerprint:
            fresh = self.search_decisions.plan_and_persist_for_candidate(candidate, audience=audience)
            self.storage.close_open_decision(decision.decision_id, "superseded")
            prompt = _prompt_from_persisted_decision(fresh)
            return TelegramCallbackResult("State changed. Review the updated decision.", prompt)
        if (summary.requires_identity_confirmation or
                any(not match.is_match for match in summary.match_results)):
            return TelegramCallbackResult("Identity review is required. No changes were made.")
        if action not in summary.allowed_actions:
            return TelegramCallbackResult("This action is no longer available.")
        if self.acquisition and action in {WorkflowAction.ADD, WorkflowAction.REACTIVATE, WorkflowAction.CONFIRM_REACTIVATE_ARCHIVED}:
            try:
                return TelegramCallbackResult("", self.acquisition.open(decision, audience))
            except (AdapterError, StorageError, ValueError):
                return TelegramCallbackResult("The acquisition choices could not be verified. Refresh before continuing.")
        if action is WorkflowAction.MANAGE_ACTIVE:
            return TelegramCallbackResult("Current state refreshed.", _prompt_from_persisted_decision(
                PersistedDecisionSummary(summary, decision)))
        if action is not WorkflowAction.KEEP_AS_IS:
            return TelegramCallbackResult("This action is not available yet. No changes were made.")
        if not self.storage.close_open_decision(decision.decision_id, "resolved"):
            return TelegramCallbackResult("This decision is already closed.")
        self.storage.append_audit_log(
            actor=audience, action="keep_as_is", media_key=decision.media_key,
            details={"decision_id": decision.decision_id},
        )
        return TelegramCallbackResult("Kept as is.", TelegramPrompt("Kept as is. No media settings changed."))

    def _select(self, decision: PendingDecision, index_text: str, audience: str) -> TelegramCallbackResult:
        candidates = decision.context.get("candidates", [])
        if (decision.decision_type != "search_selection" or not index_text.isascii()
                or not index_text.isdigit() or len(index_text) > 2
                or int(index_text) >= len(candidates)):
            return TelegramCallbackResult("Invalid title selection.")
        candidate = _candidate_from_context(candidates[int(index_text)])
        persisted = self.search_decisions.plan_and_persist_for_candidate(candidate, audience=audience)
        if not self.storage.close_open_decision(decision.decision_id, "resolved"):
            self.storage.close_open_decision(persisted.pending_decision.decision_id, "superseded")
            return TelegramCallbackResult("This selection is already closed.")
        return TelegramCallbackResult("Title selected.", _prompt_from_persisted_decision(persisted))


def _candidate_context(item: MediaItem) -> dict[str, object]:
    return {"media_type": item.media_type.value, "title": item.title, "year": item.year,
            "identities": [[identity.namespace, identity.value] for identity in sorted(item.identities)],
            "poster_url": item.poster_url, "overview": item.overview}


def _candidate_from_context(context: dict) -> MediaItem:
    # Search decisions store string IDs; selection lists store namespace/value pairs.
    identities = context["identities"]
    return MediaItem(
        media_type=MediaType(context["media_type"]), title=context["title"], year=context["year"],
        identities=frozenset(MediaIdentity(*value.split(":", 1)) if isinstance(value, str)
                             else MediaIdentity(*value) for value in identities),
        poster_url=context.get("poster_url"), overview=context.get("overview"),
    )


def _search_results_prompt(decision: PendingDecision, *, index: int, list_view: bool = False) -> TelegramPrompt:
    candidates = decision.context["candidates"]
    decision_id = decision.decision_id
    if list_view:
        return TelegramPrompt(
            text="Select a title.",
            buttons=tuple(
                TelegramButton(_item_line(candidate)[:100], f"select:{decision_id}:{i}")
                for i, candidate in enumerate(candidates)
            ) + (TelegramButton("Poster view", f"browse:{decision_id}:{index}"),),
            pending_decision_id=decision_id,
        )
    candidate = candidates[index]
    lines = [_item_line(candidate)[:220], f"Result {index + 1} of {len(candidates)}"]
    overview = candidate.get("overview")
    if isinstance(overview, str) and overview.strip():
        lines.extend(("", " ".join(overview.split())[:650]))
    photo_url = candidate.get("poster_url")
    if not photo_url:
        lines.extend(("", "Poster unavailable."))
    buttons = []
    if index > 0:
        buttons.append(TelegramButton("< Previous", f"browse:{decision_id}:{index - 1}", row=0))
    if index + 1 < len(candidates):
        buttons.append(TelegramButton("Next >", f"browse:{decision_id}:{index + 1}", row=0))
    buttons.extend((TelegramButton("Select movie", f"select:{decision_id}:{index}", row=1),
                    TelegramButton("List view", f"list:{decision_id}:{index}", row=2)))
    return TelegramPrompt("\n".join(lines), tuple(buttons), decision_id, photo_url=photo_url)


def _prompt_from_persisted_decision(persisted: PersistedDecisionSummary) -> TelegramPrompt:
    summary = persisted.summary
    decision = persisted.pending_decision
    lines = [
        _decision_heading(summary.path),
        _item_line(decision.context["candidate"]),
        f"Decision #{decision.decision_id}",
    ]
    warnings = decision.context.get("warnings", [])
    if warnings:
        lines.extend(_WARNING_LABELS.get(str(warning), "Media identity needs review.") for warning in warnings)
    if summary.ryot_item is not None:
        states = ", ".join(sorted(state.value.replace("_", " ").title() for state in summary.ryot_item.durable_states))
        lines.append(f"History: {states or 'Known to Ryot; no Curatarr memberships'}.")
    if summary.arr_item is not None:
        lines.append(f"Current state: {summary.arr_item.arr_runtime_state.value.replace('_', ' ')}.")
    if summary.requires_identity_confirmation:
        lines.append("Identity confirmation is required before sensitive actions.")
    if summary.destructive_actions_blocked:
        lines.append("Destructive actions are blocked until the match is confirmed.")
    return TelegramPrompt(
        text="\n".join(lines)[:4000],
        buttons=tuple(_button_for_action(decision.decision_id, action) for action in summary.allowed_actions),
        pending_decision_id=decision.decision_id,
    )


def _decision_heading(path: DecisionPath) -> str:
    if path is DecisionPath.ADD_NEW:
        return "Found a new title."
    if path is DecisionPath.REACTIVATE:
        return "This title is already known."
    if path is DecisionPath.REACTIVATE_ARCHIVED:
        return "This title is archived."
    if path is DecisionPath.MANAGE_ACTIVE:
        return "This title is already active."
    if path is DecisionPath.IDENTITY_BLOCKED:
        return "This match needs identity review."
    return "Review this title."


def _item_line(candidate_context: object) -> str:
    if not isinstance(candidate_context, dict):
        return "Unknown title"
    title = str(candidate_context.get("title") or "Unknown title")
    year = candidate_context.get("year")
    media_type = str(candidate_context.get("media_type") or "media")
    if year is None:
        return f"{title} ({media_type})"
    return f"{title} ({year}, {media_type})"


def _button_for_action(decision_id: int, action: WorkflowAction) -> TelegramButton:
    return TelegramButton(
        label=_ACTION_LABELS.get(action, action.value.replace("_", " ").title()),
        callback_data=f"decision:{decision_id}:{action.value}",
    )


_ACTION_LABELS = {
    WorkflowAction.ADD: "Add",
    WorkflowAction.REACTIVATE: "Reactivate",
    WorkflowAction.CONFIRM_REACTIVATE_ARCHIVED: "Reactivate archived",
    WorkflowAction.MANAGE_ACTIVE: "Manage active",
    WorkflowAction.SEARCH_NOW: "Search now",
    WorkflowAction.CHANGE_PROFILE: "Change profile",
    WorkflowAction.ARCHIVE_CANCEL: "Archive/cancel",
    WorkflowAction.REMONITOR: "Monitor again",
    WorkflowAction.OPEN_IN_JELLYFIN: "Open in Jellyfin",
    WorkflowAction.KEEP_AS_IS: "Keep as is",
    WorkflowAction.KEEP_SEARCHING: "Keep searching",
}

_WARNING_LABELS = {
    "already_watched": "You have already watched this title.",
    "archived_title": "This title was archived; reactivation requires confirmation.",
    "identity_match_needs_confirmation": "The cross-system identity match needs confirmation.",
    "weak_identity_match": "Only a tentative title/year match was found.",
    "identity_match_blocked": "A previously rejected identity match blocks this decision.",
}
