"""Tests for search/add/reactivation workflow planning."""

from __future__ import annotations

import unittest

from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.workflows.search_add import DecisionPath, SearchAddPlanner, WorkflowAction


def _movie(
    title: str = "Spider-Man 2",
    *,
    year: int = 2004,
    identities: set[MediaIdentity] | None = None,
    durable_states: set[DurableState] | None = None,
    runtime_state: ArrRuntimeState = ArrRuntimeState.NOT_ACTIVE,
) -> MediaItem:
    item_identities = {MediaIdentity("tmdb", "558")} if identities is None else identities
    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=year,
        identities=frozenset(item_identities),
        durable_states=frozenset(durable_states or set()),
        arr_runtime_state=runtime_state,
    )


class SearchAddPlannerTests(unittest.TestCase):
    def test_unknown_title_offers_add(self) -> None:
        summary = SearchAddPlanner().plan(candidate=_movie())

        self.assertEqual(summary.path, DecisionPath.ADD_NEW)
        self.assertEqual(summary.allowed_actions, (WorkflowAction.ADD,))
        self.assertFalse(summary.warnings)

    def test_known_non_archived_inactive_title_offers_reactivation(self) -> None:
        ryot = _movie(durable_states={DurableState.LIBRARY})

        summary = SearchAddPlanner().plan(candidate=_movie(), ryot_item=ryot)

        self.assertEqual(summary.path, DecisionPath.REACTIVATE)
        self.assertEqual(summary.allowed_actions, (WorkflowAction.REACTIVATE,))

    def test_archived_inactive_title_requires_archived_confirmation(self) -> None:
        ryot = _movie(durable_states={DurableState.LIBRARY, DurableState.ARCHIVED})

        summary = SearchAddPlanner().plan(candidate=_movie(), ryot_item=ryot)

        self.assertEqual(summary.path, DecisionPath.REACTIVATE_ARCHIVED)
        self.assertEqual(summary.allowed_actions, (WorkflowAction.CONFIRM_REACTIVATE_ARCHIVED,))
        self.assertIn("archived_title", summary.warnings)

    def test_watched_title_always_warns(self) -> None:
        ryot = _movie(durable_states={DurableState.LIBRARY, DurableState.WATCHED})

        summary = SearchAddPlanner().plan(candidate=_movie(), ryot_item=ryot)

        self.assertIn("already_watched", summary.warnings)

    def test_active_arr_title_enters_management_path_without_duplicate_add(self) -> None:
        arr = _movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)

        summary = SearchAddPlanner().plan(candidate=_movie(), arr_item=arr)

        self.assertEqual(summary.path, DecisionPath.MANAGE_ACTIVE)
        self.assertNotIn(WorkflowAction.ADD, summary.allowed_actions)
        self.assertIn(WorkflowAction.OPEN_IN_JELLYFIN, summary.allowed_actions)
        self.assertIn(WorkflowAction.ARCHIVE_CANCEL, summary.allowed_actions)

    def test_missing_unmonitored_title_offers_remonitor_or_archive_cancel(self) -> None:
        arr = _movie(runtime_state=ArrRuntimeState.ACTIVE_MISSING_UNMONITORED)

        summary = SearchAddPlanner().plan(candidate=_movie(), arr_item=arr)

        self.assertIn(WorkflowAction.REMONITOR, summary.allowed_actions)
        self.assertIn(WorkflowAction.ARCHIVE_CANCEL, summary.allowed_actions)

    def test_weak_match_requires_identity_confirmation_and_blocks_destructive_actions(self) -> None:
        candidate = _movie(identities=set())
        arr = _movie(identities=set(), runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)

        summary = SearchAddPlanner().plan(candidate=candidate, arr_item=arr)

        self.assertTrue(summary.requires_identity_confirmation)
        self.assertTrue(summary.destructive_actions_blocked)
        self.assertIn("weak_identity_match", summary.warnings)
        self.assertNotIn(WorkflowAction.ARCHIVE_CANCEL, summary.allowed_actions)

    def test_confirmed_negative_match_blocks_all_actions(self) -> None:
        from curatarr.storage import Storage
        from pathlib import Path
        import tempfile

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        storage = Storage(Path(temp_dir.name) / "curatarr.sqlite")
        self.addCleanup(storage.close)
        storage.initialize()
        candidate_id = MediaIdentity("tmdb", "558")
        arr_id = MediaIdentity("imdb", "tt9999999")
        storage.add_identity_mapping(candidate_id, arr_id, is_same=False, source="admin")

        candidate = _movie(identities={candidate_id})
        arr = _movie(identities={arr_id}, runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)
        from curatarr.services.identity import IdentityMatcher

        summary = SearchAddPlanner(identity_matcher=IdentityMatcher(storage=storage)).plan(
            candidate=candidate,
            arr_item=arr,
        )

        self.assertEqual(summary.path, DecisionPath.IDENTITY_BLOCKED)
        self.assertEqual(summary.allowed_actions, ())
        self.assertTrue(summary.destructive_actions_blocked)


if __name__ == "__main__":
    unittest.main()
