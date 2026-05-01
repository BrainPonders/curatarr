"""Persistence boundary for user-facing workflow decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from curatarr.domain import MediaIdentity, MediaItem
from curatarr.storage import PendingDecision, Storage
from curatarr.workflows.search_add import DecisionPath, DecisionSummary


@dataclass(frozen=True)
class PersistedDecisionSummary:
    """Search decision summary paired with its durable pending decision."""

    summary: DecisionSummary
    pending_decision: PendingDecision


@dataclass(frozen=True)
class PendingDecisionService:
    """Persist and revalidate decisions produced by pure workflow planners."""

    storage: Storage

    def persist_search_add_decision(
        self,
        summary: DecisionSummary,
        *,
        audience: str,
        state_fingerprint: str,
    ) -> PendingDecision:
        return self.storage.create_pending_decision(
            media_key=_media_key(summary.candidate),
            decision_type=_decision_type(summary),
            audience=audience,
            state_fingerprint=state_fingerprint,
            context=_decision_context(summary),
        )

    def mark_superseded(self, decision_id: int) -> PendingDecision:
        return self.storage.update_pending_decision_status(decision_id, "superseded")

    def supersede_if_stale(self, decision_id: int, current_state_fingerprint: str) -> PendingDecision:
        decision = self.storage.get_pending_decision(decision_id)
        if decision is None:
            raise ValueError(f"Pending decision {decision_id} does not exist.")
        if decision.state_fingerprint != current_state_fingerprint:
            return self.mark_superseded(decision_id)
        return decision


def search_add_state_fingerprint(summary: DecisionSummary) -> str:
    payload = _decision_context(summary)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decision_type(summary: DecisionSummary) -> str:
    if summary.path is DecisionPath.IDENTITY_BLOCKED or summary.requires_identity_confirmation:
        return "identity_confirmation"
    if summary.path is DecisionPath.ADD_NEW:
        return "search_add"
    if summary.path is DecisionPath.REACTIVATE:
        return "reactivate"
    if summary.path is DecisionPath.REACTIVATE_ARCHIVED:
        return "reactivate_archived"
    if summary.path is DecisionPath.MANAGE_ACTIVE:
        return "manage_active"
    return summary.path.value


def _decision_context(summary: DecisionSummary) -> dict[str, object]:
    return {
        "candidate": _item_context(summary.candidate),
        "path": summary.path.value,
        "allowed_actions": [action.value for action in summary.allowed_actions],
        "warnings": list(summary.warnings),
        "requires_identity_confirmation": summary.requires_identity_confirmation,
        "destructive_actions_blocked": summary.destructive_actions_blocked,
        "ryot_item": _item_context(summary.ryot_item) if summary.ryot_item is not None else None,
        "arr_item": _item_context(summary.arr_item) if summary.arr_item is not None else None,
        "match_results": [
            {
                "confidence": result.confidence.value if result.confidence is not None else None,
                "reason": result.reason,
                "blocked": result.blocked,
                "matched_identities": _identity_values(result.matched_identities),
            }
            for result in summary.match_results
        ],
    }


def _item_context(item: MediaItem) -> dict[str, object]:
    return {
        "media_type": item.media_type.value,
        "title": item.title,
        "year": item.year,
        "identities": _identity_values(item.identities),
        "durable_states": sorted(state.value for state in item.durable_states),
        "arr_runtime_state": item.arr_runtime_state.value,
    }


def _media_key(item: MediaItem) -> str:
    identities = _preferred_identities(item)
    if identities:
        return str(identities[0])
    year = item.year if item.year is not None else "unknown"
    title_key = "-".join(item.title.casefold().split()) or "untitled"
    return f"{item.media_type.value}:{title_key}:{year}"


def _preferred_identities(item: MediaItem) -> tuple[MediaIdentity, ...]:
    preferred_namespaces = (
        ("tmdb", "imdb")
        if item.media_type.value == "movie"
        else ("tvdb", "tmdb", "imdb")
    )
    identities = set(item.identities)
    ordered = []
    for namespace in preferred_namespaces:
        ordered.extend(sorted(identity for identity in identities if identity.namespace == namespace))
    ordered.extend(sorted(identity for identity in identities if identity not in ordered))
    return tuple(ordered)


def _identity_values(identities: frozenset[MediaIdentity]) -> list[str]:
    return [str(identity) for identity in sorted(identities)]
