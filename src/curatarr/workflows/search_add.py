"""Search/add/reactivation decision planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from curatarr.domain import ArrRuntimeState, MatchConfidence, MediaItem
from curatarr.services.identity import IdentityMatcher, IdentityMatchResult


class WorkflowAction(str, Enum):
    """User-visible actions offered by the search/add planner."""

    ADD = "add"
    REACTIVATE = "reactivate"
    CONFIRM_REACTIVATE_ARCHIVED = "confirm_reactivate_archived"
    MANAGE_ACTIVE = "manage_active"
    SEARCH_NOW = "search_now"
    CHANGE_PROFILE = "change_profile"
    ARCHIVE_CANCEL = "archive_cancel"
    REMONITOR = "remonitor"
    OPEN_IN_JELLYFIN = "open_in_jellyfin"
    KEEP_AS_IS = "keep_as_is"
    KEEP_SEARCHING = "keep_searching"


class DecisionPath(str, Enum):
    """High-level branch selected by the planner."""

    ADD_NEW = "add_new"
    REACTIVATE = "reactivate"
    REACTIVATE_ARCHIVED = "reactivate_archived"
    MANAGE_ACTIVE = "manage_active"
    IDENTITY_BLOCKED = "identity_blocked"


@dataclass(frozen=True)
class DecisionSummary:
    """Pure search/add decision summary."""

    candidate: MediaItem
    path: DecisionPath
    allowed_actions: tuple[WorkflowAction, ...]
    warnings: tuple[str, ...] = ()
    ryot_item: MediaItem | None = None
    arr_item: MediaItem | None = None
    match_results: tuple[IdentityMatchResult, ...] = ()
    requires_identity_confirmation: bool = False
    destructive_actions_blocked: bool = False


@dataclass(frozen=True)
class SearchAddPlanner:
    """Plan search/add/reactivation choices from current domain state."""

    identity_matcher: IdentityMatcher = field(default_factory=IdentityMatcher)

    def plan(
        self,
        *,
        candidate: MediaItem,
        ryot_item: MediaItem | None = None,
        arr_item: MediaItem | None = None,
    ) -> DecisionSummary:
        match_results = _match_known_items(self.identity_matcher, candidate, ryot_item, arr_item)
        warnings = list(_warnings(ryot_item, match_results))
        if any(result.blocked for result in match_results):
            return DecisionSummary(
                candidate=candidate,
                path=DecisionPath.IDENTITY_BLOCKED,
                allowed_actions=(),
                warnings=tuple([*warnings, "identity_match_blocked"]),
                ryot_item=ryot_item,
                arr_item=arr_item,
                match_results=match_results,
                requires_identity_confirmation=True,
                destructive_actions_blocked=True,
            )

        requires_identity_confirmation = any(
            result.confidence in {MatchConfidence.MEDIUM, MatchConfidence.WEAK}
            for result in match_results
        )

        if arr_item is not None and arr_item.arr_runtime_state.arr_active:
            actions = _active_arr_actions(arr_item.arr_runtime_state)
            destructive_actions_blocked = False
            if requires_identity_confirmation and WorkflowAction.ARCHIVE_CANCEL in actions:
                actions = tuple(action for action in actions if action is not WorkflowAction.ARCHIVE_CANCEL)
                destructive_actions_blocked = True
            return DecisionSummary(
                candidate=candidate,
                path=DecisionPath.MANAGE_ACTIVE,
                allowed_actions=actions,
                warnings=tuple(warnings),
                ryot_item=ryot_item,
                arr_item=arr_item,
                match_results=match_results,
                requires_identity_confirmation=requires_identity_confirmation,
                destructive_actions_blocked=destructive_actions_blocked,
            )

        if ryot_item is not None:
            if ryot_item.is_archived:
                return DecisionSummary(
                    candidate=candidate,
                    path=DecisionPath.REACTIVATE_ARCHIVED,
                    allowed_actions=(WorkflowAction.CONFIRM_REACTIVATE_ARCHIVED,),
                    warnings=tuple([*warnings, "archived_title"]),
                    ryot_item=ryot_item,
                    arr_item=arr_item,
                    match_results=match_results,
                    requires_identity_confirmation=requires_identity_confirmation,
                )
            return DecisionSummary(
                candidate=candidate,
                path=DecisionPath.REACTIVATE,
                allowed_actions=(WorkflowAction.REACTIVATE,),
                warnings=tuple(warnings),
                ryot_item=ryot_item,
                arr_item=arr_item,
                match_results=match_results,
                requires_identity_confirmation=requires_identity_confirmation,
            )

        return DecisionSummary(
            candidate=candidate,
            path=DecisionPath.ADD_NEW,
            allowed_actions=(WorkflowAction.ADD,),
            warnings=tuple(warnings),
            ryot_item=ryot_item,
            arr_item=arr_item,
            match_results=match_results,
            requires_identity_confirmation=requires_identity_confirmation,
        )


def _match_known_items(
    matcher: IdentityMatcher,
    candidate: MediaItem,
    ryot_item: MediaItem | None,
    arr_item: MediaItem | None,
) -> tuple[IdentityMatchResult, ...]:
    results = []
    if ryot_item is not None:
        results.append(matcher.compare(candidate, ryot_item))
    if arr_item is not None:
        results.append(matcher.compare(candidate, arr_item))
    return tuple(results)


def _warnings(
    ryot_item: MediaItem | None,
    match_results: tuple[IdentityMatchResult, ...],
) -> tuple[str, ...]:
    warnings = []
    if ryot_item is not None and ryot_item.is_watched:
        warnings.append("already_watched")
    if any(result.confidence is MatchConfidence.MEDIUM for result in match_results):
        warnings.append("identity_match_needs_confirmation")
    if any(result.confidence is MatchConfidence.WEAK for result in match_results):
        warnings.append("weak_identity_match")
    return tuple(warnings)


def _active_arr_actions(runtime_state: ArrRuntimeState) -> tuple[WorkflowAction, ...]:
    if runtime_state is ArrRuntimeState.ACTIVE_MISSING_MONITORED:
        return (
            WorkflowAction.MANAGE_ACTIVE,
            WorkflowAction.SEARCH_NOW,
            WorkflowAction.CHANGE_PROFILE,
            WorkflowAction.ARCHIVE_CANCEL,
        )
    if runtime_state is ArrRuntimeState.ACTIVE_MISSING_UNMONITORED:
        return (
            WorkflowAction.MANAGE_ACTIVE,
            WorkflowAction.REMONITOR,
            WorkflowAction.CHANGE_PROFILE,
            WorkflowAction.ARCHIVE_CANCEL,
        )
    if runtime_state is ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED:
        return (
            WorkflowAction.MANAGE_ACTIVE,
            WorkflowAction.OPEN_IN_JELLYFIN,
            WorkflowAction.KEEP_AS_IS,
            WorkflowAction.KEEP_SEARCHING,
            WorkflowAction.CHANGE_PROFILE,
            WorkflowAction.ARCHIVE_CANCEL,
        )
    if runtime_state is ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED:
        return (
            WorkflowAction.MANAGE_ACTIVE,
            WorkflowAction.OPEN_IN_JELLYFIN,
            WorkflowAction.REMONITOR,
            WorkflowAction.CHANGE_PROFILE,
            WorkflowAction.ARCHIVE_CANCEL,
        )
    return (WorkflowAction.MANAGE_ACTIVE,)
