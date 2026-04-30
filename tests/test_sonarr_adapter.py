"""Tests for the read-only Sonarr adapter."""

from __future__ import annotations

import unittest
from typing import Any

from curatarr.domain import ArrRuntimeState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.sonarr import SonarrClient, SonarrError, SonarrReadAdapter


def _client_with_series(series: list[dict[str, Any]]) -> SonarrClient:
    def transport(path: str, params: dict[str, str]):
        if path != "/api/v3/series":
            raise SonarrError(f"Unexpected path {path}")
        return series

    return SonarrClient(url="http://sonarr:8989", api_key="secret", transport=transport)


def _series_payload(*, monitored: bool, file_count: int) -> dict[str, Any]:
    return {
        "id": 456,
        "title": "Game of Thrones",
        "year": 2011,
        "tvdbId": 121361,
        "tmdbId": 1399,
        "imdbId": "tt0944947",
        "monitored": monitored,
        "statistics": {
            "episodeFileCount": file_count,
            "sizeOnDisk": 1024 if file_count else 0,
        },
    }


class SonarrReadAdapterTests(unittest.TestCase):
    def test_missing_series_returns_none(self) -> None:
        adapter = SonarrReadAdapter(_client_with_series([]))

        result = adapter.lookup_series(MediaIdentity("tvdb", "121361"))

        self.assertIsNone(result.value)

    def test_monitored_missing_maps_to_active_missing_monitored(self) -> None:
        adapter = SonarrReadAdapter(_client_with_series([_series_payload(monitored=True, file_count=0)]))

        result = adapter.lookup_series(MediaIdentity("tvdb", "121361"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_MISSING_MONITORED)

    def test_unmonitored_missing_maps_to_active_missing_unmonitored(self) -> None:
        adapter = SonarrReadAdapter(_client_with_series([_series_payload(monitored=False, file_count=0)]))

        result = adapter.lookup_series(MediaIdentity("tvdb", "121361"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_MISSING_UNMONITORED)

    def test_monitored_with_files_maps_to_downloaded_monitored(self) -> None:
        adapter = SonarrReadAdapter(_client_with_series([_series_payload(monitored=True, file_count=4)]))

        result = adapter.lookup_series(MediaIdentity("tvdb", "121361"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED)

    def test_unmonitored_with_files_maps_to_downloaded_unmonitored(self) -> None:
        adapter = SonarrReadAdapter(_client_with_series([_series_payload(monitored=False, file_count=4)]))

        result = adapter.lookup_series(MediaIdentity("tvdb", "121361"))

        assert result.value is not None
        self.assertEqual(result.value.arr_runtime_state, ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED)

    def test_payload_maps_to_domain_item_with_typed_identities(self) -> None:
        adapter = SonarrReadAdapter(_client_with_series([_series_payload(monitored=True, file_count=4)]))

        result = adapter.lookup_series(MediaIdentity("tmdb", "1399"))

        self.assertIsInstance(result.value, MediaItem)
        assert result.value is not None
        self.assertEqual(result.value.media_type, MediaType.SERIES)
        self.assertEqual(result.value.title, "Game of Thrones")
        self.assertEqual(result.value.year, 2011)
        self.assertIn(MediaIdentity("tvdb", "121361"), result.value.identities)
        self.assertIn(MediaIdentity("tmdb", "1399"), result.value.identities)
        self.assertIn(MediaIdentity("imdb", "tt0944947"), result.value.identities)
        self.assertIn(MediaIdentity("sonarr", "456"), result.value.identities)

    def test_invalid_series_list_payload_raises_typed_error(self) -> None:
        def transport(path: str, params: dict[str, str]):
            return {"not": "a list"}

        adapter = SonarrReadAdapter(SonarrClient(url="http://sonarr:8989", api_key="secret", transport=transport))

        with self.assertRaises(SonarrError):
            adapter.lookup_series(MediaIdentity("tvdb", "121361"))


if __name__ == "__main__":
    unittest.main()
