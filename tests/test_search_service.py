"""Tests for read-only search decision orchestration."""

from __future__ import annotations

import unittest

from curatarr.adapters import AdapterResult
from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.services.identity import IdentityMatcher
from curatarr.services.search import SearchDecisionService
from curatarr.workflows.search_add import DecisionPath, WorkflowAction


class _FakeMetadataAdapter:
    def __init__(self, candidates: tuple[MediaItem, ...]) -> None:
        self.candidates = candidates

    def search(self, query: str, media_type: MediaType) -> AdapterResult[tuple[MediaItem, ...]]:
        return AdapterResult(self.candidates)

    def resolve_identities(self, identity: MediaIdentity) -> AdapterResult[frozenset[MediaIdentity]]:
        return AdapterResult(frozenset({identity}))


class _FakeRyotAdapter:
    def __init__(self, item: MediaItem | None) -> None:
        self.item = item
        self.lookups: list[MediaIdentity] = []

    def get_item(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        self.lookups.append(identity)
        return AdapterResult(self.item)

    def scan_collection_memberships(self, states):
        return AdapterResult(())

    def apply_state(self, identity, state):
        raise NotImplementedError

    def remove_state(self, identity, state):
        raise NotImplementedError


class _FakeRadarrAdapter:
    def __init__(self, item: MediaItem | None) -> None:
        self.item = item

    def lookup_movie(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        return AdapterResult(self.item)

    def add_movie(self, item, *, profile, root_folder, monitored, search):
        raise NotImplementedError

    def update_movie_runtime_state(self, identity, runtime_state):
        raise NotImplementedError

    def sync_exclusions(self, archived_items):
        return AdapterResult(())


def _movie(
    *,
    title: str = "Spider-Man 2",
    identities: set[MediaIdentity] | None = None,
    durable_states: set[DurableState] | None = None,
    runtime_state: ArrRuntimeState = ArrRuntimeState.NOT_ACTIVE,
) -> MediaItem:
    item_identities = {MediaIdentity("tmdb", "558")} if identities is None else identities
    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=2004,
        identities=frozenset(item_identities),
        durable_states=frozenset(durable_states or set()),
        arr_runtime_state=runtime_state,
    )


class SearchDecisionServiceTests(unittest.TestCase):
    def _service(
        self,
        *,
        candidate: MediaItem,
        ryot_item: MediaItem | None = None,
        arr_item: MediaItem | None = None,
    ) -> SearchDecisionService:
        metadata = _FakeMetadataAdapter((candidate,))
        return SearchDecisionService(
            metadata_adapter=metadata,
            ryot_adapter=_FakeRyotAdapter(ryot_item),
            radarr_adapter=_FakeRadarrAdapter(arr_item),
            identity_matcher=IdentityMatcher(metadata_adapter=metadata),
        )

    def test_unknown_candidate_plans_add(self) -> None:
        service = self._service(candidate=_movie())

        summary = service.plan_for_first_candidate("Spider-Man 2", MediaType.MOVIE)

        assert summary is not None
        self.assertEqual(summary.path, DecisionPath.ADD_NEW)
        self.assertEqual(summary.allowed_actions, (WorkflowAction.ADD,))

    def test_watched_ryot_item_adds_warning(self) -> None:
        service = self._service(
            candidate=_movie(),
            ryot_item=_movie(durable_states={DurableState.LIBRARY, DurableState.WATCHED}),
        )

        summary = service.plan_for_first_candidate("Spider-Man 2", MediaType.MOVIE)

        assert summary is not None
        self.assertIn("already_watched", summary.warnings)
        self.assertEqual(summary.path, DecisionPath.REACTIVATE)

    def test_archived_ryot_item_requires_archived_confirmation(self) -> None:
        service = self._service(
            candidate=_movie(),
            ryot_item=_movie(durable_states={DurableState.LIBRARY, DurableState.ARCHIVED}),
        )

        summary = service.plan_for_first_candidate("Spider-Man 2", MediaType.MOVIE)

        assert summary is not None
        self.assertEqual(summary.path, DecisionPath.REACTIVATE_ARCHIVED)
        self.assertEqual(summary.allowed_actions, (WorkflowAction.CONFIRM_REACTIVATE_ARCHIVED,))

    def test_active_arr_item_enters_management_path(self) -> None:
        service = self._service(
            candidate=_movie(),
            arr_item=_movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED),
        )

        summary = service.plan_for_first_candidate("Spider-Man 2", MediaType.MOVIE)

        assert summary is not None
        self.assertEqual(summary.path, DecisionPath.MANAGE_ACTIVE)
        self.assertNotIn(WorkflowAction.ADD, summary.allowed_actions)

    def test_weak_identity_match_requires_confirmation(self) -> None:
        candidate = _movie(identities={MediaIdentity("tmdb", "558")})
        arr = _movie(
            identities={MediaIdentity("imdb", "tt0316654")},
            runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED,
        )
        service = self._service(candidate=candidate, arr_item=arr)

        summary = service.plan_for_first_candidate("Spider-Man 2", MediaType.MOVIE)

        assert summary is not None
        self.assertTrue(summary.requires_identity_confirmation)
        self.assertTrue(summary.destructive_actions_blocked)


if __name__ == "__main__":
    unittest.main()
