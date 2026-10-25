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
    def test_movie_poster_uses_exact_id_and_caches_artwork(self):
        calls = []
        def transport(path, params):
            calls.append((path, params))
            return {"id": 558, "poster_path": "/real-poster.jpg"}
        adapter = TmdbMetadataAdapter(TmdbClient("test", transport=transport))
        for _ in range(2):
            self.assertEqual(adapter.movie_poster("558"), "https://image.tmdb.org/t/p/w500/real-poster.jpg")
        self.assertEqual(calls, [("/movie/558", {})])

    def test_movie_poster_rejects_wrong_identity_and_unsafe_paths(self):
        for raw in ({"id": 559, "poster_path": "/poster.jpg"}, {"id": "558"}, {"id": True}):
            with self.subTest(raw=raw), self.assertRaises(TmdbError):
                TmdbMetadataAdapter(_client_with_responses({"/movie/558": raw})).movie_poster("558")
        for path in (None, "https://other.example/poster.jpg", "/../private.jpg"):
            self.assertIsNone(TmdbMetadataAdapter(_client_with_responses({
                "/movie/558": {"id": 558, "poster_path": path}})).movie_poster("558"))

    def test_invalid_movie_id_never_reaches_transport(self):
        adapter = TmdbMetadataAdapter(_client_with_responses({}))
        for value in ("0", "-1", "bad", "558/other", "9" * 5000, "2147483648"):
            with self.subTest(value=value[:20]), self.assertRaises(TmdbError):
                adapter.movie_poster(value)

    def test_optional_year_is_sent_as_filter_and_numeric_title_is_preserved(self):
        calls = []
        def transport(path, params):
            calls.append(params)
            return {"results": [{"id": 1, "title": "Movie"}]}
        adapter = TmdbMetadataAdapter(TmdbClient("test", transport=transport))
        for query in ("spiderman 2014", "spiderman (2014)"):
            adapter.search(query, MediaType.MOVIE)
            self.assertEqual(calls[-1], {"query": "spiderman", "primary_release_year": "2014"})
        for query in ("1917", "2001: A Space Odyssey"):
            adapter.search(query, MediaType.MOVIE)
            self.assertEqual(calls[-1], {"query": query})
        adapter.search('"Class of 1984"', MediaType.MOVIE)
        self.assertEqual(calls[-1], {"query": "Class of 1984"})

    def test_empty_year_filtered_search_retries_literal_numeric_suffix_title(self):
        calls = []
        def transport(path, params):
            calls.append(params)
            return {"results": []}
        TmdbMetadataAdapter(TmdbClient("test", transport=transport)).search("Class of 1984", MediaType.MOVIE)
        self.assertEqual(calls, [{"query": "Class of", "primary_release_year": "1984"}, {"query": "Class of 1984"}])
    def test_search_preserves_poster_and_overview(self):
        client = _client_with_responses({"/search/movie": {"results": [
            {"id": 558, "title": "Spider-Man 2", "poster_path": "/validPoster.jpg", "overview": "A movie synopsis."},
            {"id": 559, "title": "Missing poster", "poster_path": None},
            {"id": 560, "title": "Invalid poster", "poster_path": "https://untrusted.example/image.jpg"},
        ]}})
        results = TmdbMetadataAdapter(client).search("movie", MediaType.MOVIE).value
        self.assertEqual(results[0].poster_url, "https://image.tmdb.org/t/p/w500/validPoster.jpg")
        self.assertEqual(results[0].overview, "A movie synopsis.")
        self.assertIsNone(results[1].poster_url)
        self.assertIsNone(results[2].poster_url)

    def test_search_public_rating_requires_valid_score_and_votes(self):
        for score, votes, expected in [(5.46, 20, 5.46), (0, 1, 0), (0, 0, None),
                                       (11, 1, None), (float('nan'), 1, None),
                                       (True, 5, None), ('7.1', 2, None), (8, True, None)]:
            with self.subTest(score=score, votes=votes):
                reader = TmdbMetadataAdapter(_client_with_responses({'/search/movie': {'results': [
                    {'id': 558, 'title': 'Movie', 'vote_average': score, 'vote_count': votes}]}}))
                self.assertEqual(reader.search('Movie', MediaType.MOVIE).value[0].tmdb_rating, expected)

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
