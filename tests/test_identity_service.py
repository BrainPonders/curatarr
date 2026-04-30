"""Tests for media identity matching service."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from curatarr.adapters import AdapterResult
from curatarr.domain import (
    MatchConfidence,
    MediaIdentity,
    MediaItem,
    MediaType,
    can_authorize_destructive_action,
)
from curatarr.services.identity import IdentityMatcher
from curatarr.storage import Storage


class _MetadataResolver:
    def __init__(self, mappings: dict[MediaIdentity, frozenset[MediaIdentity]]) -> None:
        self.mappings = mappings

    def search(self, query: str, media_type: MediaType):
        return AdapterResult(())

    def resolve_identities(self, identity: MediaIdentity) -> AdapterResult[frozenset[MediaIdentity]]:
        return AdapterResult(self.mappings.get(identity, frozenset()))


def _movie(title: str, *, year: int | None = None, identities: set[MediaIdentity] | None = None) -> MediaItem:
    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=year,
        identities=frozenset(identities or set()),
    )


class IdentityMatcherTests(unittest.TestCase):
    def _storage(self) -> Storage:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        storage = Storage(Path(temp_dir.name) / "curatarr.sqlite")
        self.addCleanup(storage.close)
        storage.initialize()
        return storage

    def test_exact_shared_typed_identity_is_strong(self) -> None:
        identity = MediaIdentity("tmdb", "558")
        matcher = IdentityMatcher()

        result = matcher.compare(
            _movie("Spider-Man 2", identities={identity}),
            _movie("Spider-Man 2", identities={identity}),
        )

        self.assertEqual(result.confidence, MatchConfidence.STRONG)
        self.assertTrue(result.is_match)
        self.assertEqual(result.matched_identities, frozenset({identity}))

    def test_confirmed_local_mapping_is_strong(self) -> None:
        storage = self._storage()
        tmdb = MediaIdentity("tmdb", "558")
        imdb = MediaIdentity("imdb", "tt0316654")
        storage.add_identity_mapping(tmdb, imdb, is_same=True, source="admin")

        result = IdentityMatcher(storage=storage).compare(
            _movie("Spider-Man 2", identities={tmdb}),
            _movie("Spider-Man 2", identities={imdb}),
        )

        self.assertEqual(result.confidence, MatchConfidence.STRONG)
        self.assertTrue(result.is_match)

    def test_confirmed_negative_mapping_blocks_match(self) -> None:
        storage = self._storage()
        tmdb = MediaIdentity("tmdb", "558")
        imdb = MediaIdentity("imdb", "tt9999999")
        storage.add_identity_mapping(tmdb, imdb, is_same=False, source="admin")

        result = IdentityMatcher(storage=storage).compare(
            _movie("Spider-Man 2", year=2004, identities={tmdb}),
            _movie("Spider-Man 2", year=2004, identities={imdb}),
        )

        self.assertIsNone(result.confidence)
        self.assertTrue(result.blocked)
        self.assertFalse(result.is_match)

    def test_metadata_cross_resolution_is_medium(self) -> None:
        tmdb = MediaIdentity("tmdb", "558")
        imdb = MediaIdentity("imdb", "tt0316654")
        resolver = _MetadataResolver(
            {
                tmdb: frozenset({tmdb, imdb}),
                imdb: frozenset({tmdb, imdb}),
            }
        )

        result = IdentityMatcher(metadata_adapter=resolver).compare(
            _movie("Spider-Man 2", identities={tmdb}),
            _movie("Spider-Man 2", identities={imdb}),
        )

        self.assertEqual(result.confidence, MatchConfidence.MEDIUM)
        self.assertEqual(result.matched_identities, frozenset({tmdb, imdb}))

    def test_title_year_only_is_weak(self) -> None:
        result = IdentityMatcher().compare(
            _movie("Spider-Man 2", year=2004),
            _movie(" spider-man   2 ", year=2004),
        )

        self.assertEqual(result.confidence, MatchConfidence.WEAK)
        self.assertTrue(result.is_match)

    def test_medium_and_weak_do_not_authorize_destructive_action(self) -> None:
        self.assertFalse(can_authorize_destructive_action(MatchConfidence.MEDIUM))
        self.assertFalse(can_authorize_destructive_action(MatchConfidence.WEAK))


if __name__ == "__main__":
    unittest.main()
