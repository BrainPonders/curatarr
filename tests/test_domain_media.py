"""Tests for Curatarr media domain primitives."""

from __future__ import annotations

import unittest

from curatarr.domain import (
    ArrRuntimeState,
    DurableState,
    MatchConfidence,
    MediaIdentity,
    MediaItem,
    MediaType,
    can_authorize_destructive_action,
    parse_identity,
)


class MediaDomainTests(unittest.TestCase):
    def test_parse_identity_requires_namespace(self) -> None:
        parsed = parse_identity("tmdb:558")

        self.assertEqual(parsed.identity, MediaIdentity("tmdb", "558"))
        self.assertEqual(str(parsed.identity), "tmdb:558")

        with self.assertRaises(ValueError):
            parse_identity("558")

    def test_identity_namespace_is_part_of_identity(self) -> None:
        self.assertNotEqual(MediaIdentity("imdb", "tt123456"), MediaIdentity("tvdb", "tt123456"))

    def test_only_strong_match_authorizes_destructive_action(self) -> None:
        self.assertTrue(can_authorize_destructive_action(MatchConfidence.STRONG))
        self.assertFalse(can_authorize_destructive_action(MatchConfidence.MEDIUM))
        self.assertFalse(can_authorize_destructive_action(MatchConfidence.WEAK))

    def test_owned_does_not_imply_file_present(self) -> None:
        item = MediaItem(
            media_type=MediaType.MOVIE,
            title="Spider-Man 2",
            identities=frozenset({MediaIdentity("tmdb", "558")}),
            durable_states=frozenset({DurableState.LIBRARY, DurableState.OWNED}),
            arr_runtime_state=ArrRuntimeState.NOT_ACTIVE,
        )

        self.assertTrue(item.was_owned)
        self.assertFalse(item.file_present)

    def test_watched_does_not_imply_archived(self) -> None:
        item = MediaItem(
            media_type=MediaType.SERIES,
            title="Example Show",
            durable_states=frozenset({DurableState.LIBRARY, DurableState.WATCHED}),
        )

        self.assertTrue(item.is_watched)
        self.assertFalse(item.is_archived)

    def test_missing_unmonitored_runtime_state_is_invalid(self) -> None:
        self.assertTrue(ArrRuntimeState.ACTIVE_MISSING_UNMONITORED.is_invalid)
        self.assertFalse(ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED.is_invalid)


if __name__ == "__main__":
    unittest.main()
