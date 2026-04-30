"""Tests for the TMDB metadata adapter."""

from __future__ import annotations

import unittest
from typing import Any

from curatarr.domain import MediaIdentity, MediaItem, MediaType
from curatarr.integrations.tmdb import TmdbClient, TmdbError, TmdbMetadataAdapter


def _client_with_responses(responses: dict[str, dict[str, Any]]) -> TmdbClient:
    def transport(path: str, params: dict[str, str]) -> dict[str, Any]:
        if path not in responses:
            raise TmdbError(f"Unexpected path {path}")
        return responses[path]

    return TmdbClient(api_key="secret", transport=transport)


class TmdbMetadataAdapterTests(unittest.TestCase):
    def test_movie_search_maps_results_to_media_items(self) -> None:
        adapter = TmdbMetadataAdapter(
            _client_with_responses(
                {
                    "/search/movie": {
                        "results": [
                            {
                                "id": 558,
                                "title": "Spider-Man 2",
                                "release_date": "2004-06-25",
                            }
                        ]
                    }
                }
            )
        )

        result = adapter.search("Spider-Man 2", MediaType.MOVIE)

        self.assertEqual(len(result.value), 1)
        item = result.value[0]
        self.assertIsInstance(item, MediaItem)
        self.assertEqual(item.media_type, MediaType.MOVIE)
        self.assertEqual(item.year, 2004)
        self.assertEqual(item.identities, frozenset({MediaIdentity("tmdb", "558")}))
        self.assertEqual(result.diagnostics["source"], "tmdb")

    def test_series_search_maps_provider_ids_when_present(self) -> None:
        adapter = TmdbMetadataAdapter(
            _client_with_responses(
                {
                    "/search/tv": {
                        "results": [
                            {
                                "id": 1399,
                                "name": "Game of Thrones",
                                "first_air_date": "2011-04-17",
                                "tvdb_id": 121361,
                            }
                        ]
                    }
                }
            )
        )

        result = adapter.search("Game of Thrones", MediaType.SERIES)

        item = result.value[0]
        self.assertEqual(item.media_type, MediaType.SERIES)
        self.assertEqual(item.year, 2011)
        self.assertIn(MediaIdentity("tmdb", "1399"), item.identities)
        self.assertIn(MediaIdentity("tvdb", "121361"), item.identities)

    def test_tmdb_movie_identity_resolves_to_imdb_identity(self) -> None:
        adapter = TmdbMetadataAdapter(
            _client_with_responses(
                {
                    "/movie/558/external_ids": {
                        "id": 558,
                        "imdb_id": "tt0316654",
                    }
                }
            )
        )

        result = adapter.resolve_identities(MediaIdentity("tmdb", "558"))

        self.assertEqual(
            result.value,
            frozenset({MediaIdentity("tmdb", "558"), MediaIdentity("imdb", "tt0316654")}),
        )

    def test_tmdb_series_identity_resolves_to_tvdb_identity(self) -> None:
        adapter = TmdbMetadataAdapter(
            _client_with_responses(
                {
                    "/movie/1399/external_ids": {},
                    "/tv/1399/external_ids": {
                        "id": 1399,
                        "imdb_id": "tt0944947",
                        "tvdb_id": 121361,
                    },
                }
            )
        )

        result = adapter.resolve_identities(MediaIdentity("tmdb", "1399"))

        self.assertIn(MediaIdentity("tvdb", "121361"), result.value)
        self.assertIn(MediaIdentity("imdb", "tt0944947"), result.value)

    def test_invalid_search_payload_raises_typed_error(self) -> None:
        adapter = TmdbMetadataAdapter(_client_with_responses({"/search/movie": {"not_results": []}}))

        with self.assertRaises(TmdbError):
            adapter.search("Spider-Man 2", MediaType.MOVIE)


if __name__ == "__main__":
    unittest.main()
