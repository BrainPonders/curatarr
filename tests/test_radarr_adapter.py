"""Tests for the read-only Radarr adapter."""

from __future__ import annotations

import unittest
from typing import Any

from curatarr.domain import ArrRuntimeState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.radarr import RadarrClient, RadarrError, RadarrReadAdapter


def _client_with_movies(movies: list[dict[str, Any]]) -> RadarrClient:
    def transport(path: str, params: dict[str, str]):
        if path != "/api/v3/movie":
            raise RadarrError(f"Unexpected path {path}")
        return movies

    return RadarrClient(url="http://radarr:7878", api_key="secret", transport=transport)


def _movie_payload(*, monitored: bool, has_file: bool) -> dict[str, Any]:
    return {
        "id": 123,
        "title": "Spider-Man 2",
        "year": 2004,
        "tmdbId": 558,
        "imdbId": "tt0316654",
        "monitored": monitored,
        "hasFile": has_file,
    }


class RadarrReadAdapterTests(unittest.TestCase):
    def test_missing_movie_returns_none(self) -> None:
        adapter = RadarrReadAdapter(_client_with_movies([]))

        result = adapter.lookup_movie(MediaIdentity("tmdb", "558"))

        self.assertIsNone(result.value)

    def test_monitored_missing_maps_to_active_missing_monitored(self) -> None:
        adapter = RadarrReadAdapter(_client_with_movies([_movie_payload(monitored=True, has_file=False)]))

        result = adapter.lookup_movie(MediaIdentity("tmdb", "558"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_MISSING_MONITORED)

    def test_unmonitored_missing_maps_to_active_missing_unmonitored(self) -> None:
        adapter = RadarrReadAdapter(_client_with_movies([_movie_payload(monitored=False, has_file=False)]))

        result = adapter.lookup_movie(MediaIdentity("tmdb", "558"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_MISSING_UNMONITORED)

    def test_monitored_with_file_maps_to_downloaded_monitored(self) -> None:
        adapter = RadarrReadAdapter(_client_with_movies([_movie_payload(monitored=True, has_file=True)]))

        result = adapter.lookup_movie(MediaIdentity("tmdb", "558"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)

    def test_unmonitored_with_file_maps_to_downloaded_unmonitored(self) -> None:
        adapter = RadarrReadAdapter(_client_with_movies([_movie_payload(monitored=False, has_file=True)]))

        result = adapter.lookup_movie(MediaIdentity("tmdb", "558"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED)

    def test_payload_maps_to_domain_item_with_typed_identities(self) -> None:
        adapter = RadarrReadAdapter(_client_with_movies([_movie_payload(monitored=True, has_file=True)]))

        result = adapter.lookup_movie(MediaIdentity("imdb", "tt0316654"))

        self.assertIsInstance(result.value, MediaItem)
        assert result.value is not None
        self.assertEqual(result.value.media_type, MediaType.MOVIE)
        self.assertEqual(result.value.title, "Spider-Man 2")
        self.assertEqual(result.value.year, 2004)
        self.assertIn(MediaIdentity("tmdb", "558"), result.value.identities)
        self.assertIn(MediaIdentity("imdb", "tt0316654"), result.value.identities)
        self.assertIn(MediaIdentity("radarr", "123"), result.value.identities)

    def test_invalid_movie_list_payload_raises_typed_error(self) -> None:
        def transport(path: str, params: dict[str, str]):
            return {"not": "a list"}

        adapter = RadarrReadAdapter(RadarrClient(url="http://radarr:7878", api_key="secret", transport=transport))

        with self.assertRaises(RadarrError):
            adapter.lookup_movie(MediaIdentity("tmdb", "558"))


if __name__ == "__main__":
    unittest.main()
