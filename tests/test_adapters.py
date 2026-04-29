"""Tests for adapter interface boundaries."""

from __future__ import annotations

import unittest

from curatarr.adapters import AdapterResult, RadarrAdapter, RyotAdapter
from curatarr.domain import (
    ArrRuntimeState,
    DurableState,
    MediaIdentity,
    MediaItem,
    MediaType,
)


class _FakeRyotAdapter:
    def __init__(self, item: MediaItem | None = None) -> None:
        self.item = item

    def get_item(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        return AdapterResult(self.item, diagnostics={"source": "fake-ryot"})

    def scan_collection_memberships(
        self,
        states: frozenset[DurableState],
    ) -> AdapterResult[list[MediaItem]]:
        return AdapterResult([] if self.item is None else [self.item])

    def apply_state(self, identity: MediaIdentity, state: DurableState) -> AdapterResult[MediaItem]:
        assert self.item is not None
        return AdapterResult(self.item)

    def remove_state(self, identity: MediaIdentity, state: DurableState) -> AdapterResult[MediaItem]:
        assert self.item is not None
        return AdapterResult(self.item)


class _FakeRadarrAdapter:
    def lookup_movie(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        return AdapterResult(
            MediaItem(
                media_type=MediaType.MOVIE,
                title="Spider-Man 2",
                identities=frozenset({identity}),
                arr_runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED,
            )
        )

    def add_movie(
        self,
        item: MediaItem,
        *,
        profile: str,
        root_folder: str,
        monitored: bool,
        search: bool,
    ) -> AdapterResult[MediaItem]:
        return AdapterResult(item)

    def update_movie_runtime_state(
        self,
        identity: MediaIdentity,
        runtime_state: ArrRuntimeState,
    ) -> AdapterResult[MediaItem]:
        return AdapterResult(
            MediaItem(
                media_type=MediaType.MOVIE,
                title="Spider-Man 2",
                identities=frozenset({identity}),
                arr_runtime_state=runtime_state,
            )
        )

    def sync_exclusions(self, archived_items: list[MediaItem]) -> AdapterResult[list[MediaItem]]:
        return AdapterResult(archived_items)


class AdapterInterfaceTests(unittest.TestCase):
    def test_ryot_adapter_returns_domain_items_not_raw_payloads(self) -> None:
        identity = MediaIdentity("tmdb", "558")
        item = MediaItem(
            media_type=MediaType.MOVIE,
            title="Spider-Man 2",
            identities=frozenset({identity}),
            durable_states=frozenset({DurableState.LIBRARY, DurableState.OWNED}),
        )
        adapter: RyotAdapter = _FakeRyotAdapter(item)

        result = adapter.get_item(identity)

        self.assertIsInstance(result, AdapterResult)
        self.assertIsInstance(result.value, MediaItem)
        self.assertEqual(result.value.identities, frozenset({identity}))
        self.assertEqual(result.diagnostics["source"], "fake-ryot")

    def test_arr_adapter_returns_runtime_state_as_domain_facts(self) -> None:
        identity = MediaIdentity("tmdb", "558")
        adapter: RadarrAdapter = _FakeRadarrAdapter()

        result = adapter.lookup_movie(identity)

        self.assertIsInstance(result.value, MediaItem)
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)
        self.assertTrue(result.value.file_present)


if __name__ == "__main__":
    unittest.main()
