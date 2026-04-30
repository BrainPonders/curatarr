"""Tests for persistent pending workflow decisions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.services import PendingDecisionService
from curatarr.storage import Storage
from curatarr.workflows.search_add import DecisionPath, SearchAddPlanner


def _movie(
    title: str = "Spider-Man 2",
    *,
    year: int = 2004,
    identities: set[MediaIdentity] | None = None,
    durable_states: set[DurableState] | None = None,
    runtime_state: ArrRuntimeState = ArrRuntimeState.NOT_ACTIVE,
) -> MediaItem:
    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=year,
        identities=frozenset({MediaIdentity("tmdb", "558")} if identities is None else identities),
        durable_states=frozenset(durable_states or set()),
        arr_runtime_state=runtime_state,
    )


class PendingDecisionServiceTests(unittest.TestCase):
    def _service(self) -> PendingDecisionService:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        storage = Storage(Path(temp_dir.name) / "curatarr.sqlite")
        self.addCleanup(storage.close)
        storage.initialize()
        return PendingDecisionService(storage)

    def test_search_add_summary_is_persisted_as_pending_decision(self) -> None:
        service = self._service()
        summary = SearchAddPlanner().plan(candidate=_movie())

        decision = service.persist_search_add_decision(
            summary,
            audience="telegram:user:1",
            state_fingerprint="state-1",
        )

        self.assertEqual(decision.media_key, "tmdb:558")
        self.assertEqual(decision.decision_type, "search_add")
        self.assertEqual(decision.status, "open")
        self.assertEqual(decision.context["path"], DecisionPath.ADD_NEW.value)
        self.assertEqual(decision.context["candidate"]["title"], "Spider-Man 2")
        self.assertEqual(decision.context["allowed_actions"], ["add"])

    def test_archived_reactivation_preserves_warning_context(self) -> None:
        service = self._service()
        ryot = _movie(durable_states={DurableState.LIBRARY, DurableState.ARCHIVED, DurableState.WATCHED})
        summary = SearchAddPlanner().plan(candidate=_movie(), ryot_item=ryot)

        decision = service.persist_search_add_decision(
            summary,
            audience="telegram:user:1",
            state_fingerprint="state-2",
        )

        self.assertEqual(decision.decision_type, "reactivate_archived")
        self.assertIn("already_watched", decision.context["warnings"])
        self.assertIn("archived_title", decision.context["warnings"])
        self.assertEqual(decision.context["ryot_item"]["durable_states"], ["archived", "library", "watched"])

    def test_weak_match_becomes_identity_confirmation_decision(self) -> None:
        service = self._service()
        candidate = _movie(identities=set())
        arr_item = _movie(identities=set(), runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)
        summary = SearchAddPlanner().plan(candidate=candidate, arr_item=arr_item)

        decision = service.persist_search_add_decision(
            summary,
            audience="telegram:user:1",
            state_fingerprint="state-3",
        )

        self.assertEqual(decision.decision_type, "identity_confirmation")
        self.assertTrue(decision.context["requires_identity_confirmation"])
        self.assertTrue(decision.context["destructive_actions_blocked"])
        self.assertEqual(decision.context["match_results"][0]["confidence"], "weak")

    def test_stale_decision_is_marked_superseded(self) -> None:
        service = self._service()
        summary = SearchAddPlanner().plan(candidate=_movie())
        decision = service.persist_search_add_decision(
            summary,
            audience="telegram:user:1",
            state_fingerprint="state-old",
        )

        updated = service.supersede_if_stale(decision.decision_id, "state-new")

        self.assertEqual(updated.status, "superseded")


if __name__ == "__main__":
    unittest.main()
