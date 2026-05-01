"""Read-only search decision orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from curatarr.adapters import MetadataAdapter, RadarrAdapter, RyotAdapter, SonarrAdapter
from curatarr.domain import MediaIdentity, MediaItem, MediaType
from curatarr.services.decisions import (
    PendingDecisionService,
    PersistedDecisionSummary,
    search_add_state_fingerprint,
)
from curatarr.services.identity import IdentityMatcher
from curatarr.workflows.search_add import DecisionSummary, SearchAddPlanner


@dataclass(frozen=True)
class SearchDecisionService:
    """Orchestrates metadata, Ryot, Arr, and workflow planning for one candidate."""

    metadata_adapter: MetadataAdapter
    ryot_adapter: RyotAdapter
    identity_matcher: IdentityMatcher
    radarr_adapter: RadarrAdapter | None = None
    sonarr_adapter: SonarrAdapter | None = None
    pending_decision_service: PendingDecisionService | None = None

    def search(self, query: str, media_type: MediaType) -> tuple[MediaItem, ...]:
        """Search metadata and return Curatarr domain candidates."""

        return tuple(self.metadata_adapter.search(query, media_type).value)

    def plan_for_first_candidate(self, query: str, media_type: MediaType) -> DecisionSummary | None:
        """Search metadata and plan against the first returned candidate."""

        candidates = self.search(query, media_type)
        if not candidates:
            return None
        return self.plan_for_candidate(candidates[0])

    def plan_for_candidate(self, candidate: MediaItem) -> DecisionSummary:
        """Build a read-only decision summary for a selected metadata candidate."""

        ryot_item = self._lookup_ryot(candidate)
        arr_item = self._lookup_arr(candidate)
        planner = SearchAddPlanner(identity_matcher=self.identity_matcher)
        return planner.plan(candidate=candidate, ryot_item=ryot_item, arr_item=arr_item)

    def plan_and_persist_for_first_candidate(
        self,
        query: str,
        media_type: MediaType,
        *,
        audience: str,
    ) -> PersistedDecisionSummary | None:
        """Search metadata, plan the first candidate, and persist its pending decision."""

        candidates = self.search(query, media_type)
        if not candidates:
            return None
        return self.plan_and_persist_for_candidate(candidates[0], audience=audience)

    def plan_and_persist_for_candidate(
        self,
        candidate: MediaItem,
        *,
        audience: str,
    ) -> PersistedDecisionSummary:
        """Plan a selected candidate and persist a revalidatable pending decision."""

        if self.pending_decision_service is None:
            raise ValueError("Pending decision persistence is not configured.")
        summary = self.plan_for_candidate(candidate)
        pending_decision = self.pending_decision_service.persist_search_add_decision(
            summary,
            audience=audience,
            state_fingerprint=search_add_state_fingerprint(summary),
        )
        return PersistedDecisionSummary(summary=summary, pending_decision=pending_decision)

    def _lookup_ryot(self, candidate: MediaItem) -> MediaItem | None:
        for identity in _identity_priority(candidate):
            result = self.ryot_adapter.get_item(identity)
            if result.value is not None:
                return result.value
        return None

    def _lookup_arr(self, candidate: MediaItem) -> MediaItem | None:
        if candidate.media_type is MediaType.MOVIE and self.radarr_adapter is not None:
            for identity in _identity_priority(candidate):
                result = self.radarr_adapter.lookup_movie(identity)
                if result.value is not None:
                    return result.value
        if candidate.media_type is MediaType.SERIES and self.sonarr_adapter is not None:
            for identity in _identity_priority(candidate):
                result = self.sonarr_adapter.lookup_series(identity)
                if result.value is not None:
                    return result.value
        return None


def _identity_priority(item: MediaItem) -> tuple[MediaIdentity, ...]:
    preferred = (
        ("tmdb", "imdb")
        if item.media_type is MediaType.MOVIE
        else ("tvdb", "tmdb", "imdb")
    )
    identities = set(item.identities)
    ordered = []
    for namespace in preferred:
        ordered.extend(sorted(identity for identity in identities if identity.namespace == namespace))
    ordered.extend(sorted(identity for identity in identities if identity not in ordered))
    return tuple(ordered)
