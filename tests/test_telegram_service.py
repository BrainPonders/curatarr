"""Tests for Telegram-facing workflow rendering."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from curatarr.adapters import AdapterResult
from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.services import PendingDecisionService, TelegramSearchCommand
from curatarr.services.identity import IdentityMatcher
from curatarr.services.search import SearchDecisionService
from curatarr.storage import Storage


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

    def get_item(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
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
    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=2004,
        identities=frozenset({MediaIdentity("tmdb", "558")} if identities is None else identities),
        durable_states=frozenset(durable_states or set()),
        arr_runtime_state=runtime_state,
    )


class TelegramSearchCommandTests(unittest.TestCase):
    def _command(
        self,
        *,
        candidates: tuple[MediaItem, ...],
        ryot_item: MediaItem | None = None,
        arr_item: MediaItem | None = None,
    ) -> TelegramSearchCommand:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        storage = Storage(Path(temp_dir.name) / "curatarr.sqlite")
        self.addCleanup(storage.close)
        storage.initialize()
        metadata = _FakeMetadataAdapter(candidates)
        search_service = SearchDecisionService(
            metadata_adapter=metadata,
            ryot_adapter=_FakeRyotAdapter(ryot_item),
            radarr_adapter=_FakeRadarrAdapter(arr_item),
            identity_matcher=IdentityMatcher(metadata_adapter=metadata),
            pending_decision_service=PendingDecisionService(storage),
        )
        return TelegramSearchCommand(search_decisions=search_service)

    def test_blank_search_returns_usage_prompt_without_pending_decision(self) -> None:
        command = self._command(candidates=())

        prompt = command.handle_search("   ", MediaType.MOVIE, audience="telegram:user:1")

        self.assertEqual(prompt.text, "Send a movie or series title to search.")
        self.assertIsNone(prompt.pending_decision_id)
        self.assertEqual(prompt.buttons, ())

    def test_no_results_returns_no_result_prompt(self) -> None:
        command = self._command(candidates=())

        prompt = command.handle_search("Unknown", MediaType.MOVIE, audience="telegram:user:1")

        self.assertEqual(prompt.text, 'No movie results found for "Unknown".')
        self.assertIsNone(prompt.pending_decision_id)

    def test_add_search_renders_pending_decision_button(self) -> None:
        command = self._command(candidates=(_movie(),))

        prompt = command.handle_search("Spider-Man 2", MediaType.MOVIE, audience="telegram:user:1")

        assert prompt.pending_decision_id is not None
        self.assertIn("Found a new title.", prompt.text)
        self.assertIn("Spider-Man 2 (2004, movie)", prompt.text)
        self.assertEqual(len(prompt.buttons), 1)
        self.assertEqual(prompt.buttons[0].label, "Add")
        self.assertEqual(prompt.buttons[0].callback_data, f"decision:{prompt.pending_decision_id}:add")

    def test_archived_known_title_renders_warning_and_confirmation_action(self) -> None:
        command = self._command(
            candidates=(_movie(),),
            ryot_item=_movie(durable_states={DurableState.LIBRARY, DurableState.ARCHIVED, DurableState.WATCHED}),
        )

        prompt = command.handle_search("Spider-Man 2", MediaType.MOVIE, audience="telegram:user:1")

        self.assertIn("This title is archived.", prompt.text)
        self.assertIn("already watched", prompt.text)
        self.assertIn("reactivation requires confirmation", prompt.text)
        self.assertEqual(prompt.buttons[0].label, "Reactivate archived")

    def test_weak_match_prompt_explains_blocked_destructive_actions(self) -> None:
        command = self._command(
            candidates=(_movie(identities={MediaIdentity("tmdb", "558")}),),
            arr_item=_movie(
                identities={MediaIdentity("imdb", "tt0316654")},
                runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED,
            ),
        )

        prompt = command.handle_search("Spider-Man 2", MediaType.MOVIE, audience="telegram:user:1")

        self.assertIn("Identity confirmation is required", prompt.text)
        self.assertIn("Destructive actions are blocked", prompt.text)
        self.assertFalse(any("archive_cancel" in button.callback_data for button in prompt.buttons))


if __name__ == "__main__":
    unittest.main()
