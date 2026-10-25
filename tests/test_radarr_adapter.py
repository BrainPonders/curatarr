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
    def test_imdb_rating_requires_native_imdb_identity_and_positive_votes(self):
        raw = dict(_movie_payload(monitored=True, has_file=False),
                   ratings={'imdb': {'value': 7.2, 'votes': 100}})
        def score():
            return RadarrReadAdapter(_client_with_movies([raw])).lookup_movie(MediaIdentity('tmdb', '558')).value.imdb_rating
        self.assertEqual(score(), 7.2)
        raw['ratings']['imdb']['votes'] = 0
        self.assertIsNone(score())
        raw['ratings']['imdb']['votes'] = 100
        raw.pop('imdbId')
        self.assertIsNone(score())

    def test_snapshot_reads_the_list_once_and_requires_complete_runtime_evidence(self):
        calls = []
        def transport(path, params):
            calls.append(path)
            return [_movie_payload(monitored=True, has_file=False)]
        reader = RadarrReadAdapter(RadarrClient("http://radarr", "secret", transport))
        self.assertEqual(len(reader.scan_movies().value), 1)
        self.assertEqual(calls, ["/api/v3/movie"])
        for field in ("id", "monitored", "hasFile"):
            raw = _movie_payload(monitored=False, has_file=False)
            raw.pop(field)
            with self.subTest(field=field), self.assertRaises(RadarrError):
                RadarrReadAdapter(_client_with_movies([raw])).scan_movies()

    def test_partial_or_duplicate_snapshot_cannot_become_absence(self):
        raw = _movie_payload(monitored=True, has_file=True)
        for payload in ([raw, None], [raw, raw]):
            with self.subTest(payload=payload), self.assertRaises(RadarrError):
                RadarrReadAdapter(_client_with_movies(payload)).scan_movies()

    def test_profile_identifier_is_presentation_only_and_requires_positive_integer(self):
        for value, expected in ((3, 3), (True, None), (0, None), ("3", None)):
            raw = dict(_movie_payload(monitored=True, has_file=True), qualityProfileId=value)
            item = RadarrReadAdapter(_client_with_movies([raw])).lookup_movie(MediaIdentity("tmdb", "558")).value
            self.assertEqual(item.quality_profile_id, expected)

    def test_quality_profile_resolves_leaf_group_and_disabled_upgrades(self):
        for cutoff, allowed, expected in ((7, True, "Bluray-2160p"), (100, True, "Ultra HD"),
                                           (999, True, None), (7, False, "Bluray-2160p")):
            def transport(path, params):
                self.assertEqual(path, "/api/v3/qualityprofile/3")
                return {"id": 3, "name": "Movies > 4K", "upgradeAllowed": allowed, "cutoff": cutoff,
                        "items": [{"id": 100, "name": "Ultra HD", "items": [
                            {"quality": {"id": 7, "name": "Bluray-2160p"}}]}]}
            adapter = RadarrReadAdapter(RadarrClient("http://radarr", "secret", transport))
            self.assertEqual(adapter.quality_profile(3), {"name": "Movies > 4K",
                "upgrade_allowed": allowed, "cutoff_quality": expected, "score_target": None,
                "cutoff_qualities": [] if cutoff == 999 else [{"resolution": None, "modifier": None}],
                "language": None, "format_items": []})

    def test_file_details_preserve_negative_and_zero_scores_and_validate_ownership(self):
        for score in (-5000, 0, None, True):
            raw = {"id": 8, "movieId": 123, "customFormatScore": score, "customFormats": [],
                   "qualityCutoffNotMet": False, "languages": [{"id": 1}]}
            adapter = RadarrReadAdapter(RadarrClient("http://radarr", "secret", lambda path, params: raw))
            facts = adapter.file_details(8, 123)
            self.assertEqual(facts["score"], score if type(score) is int else None)
            self.assertEqual(facts["format_ids"], [])
            self.assertEqual(facts["language_ids"], [1])
            with self.assertRaises(RadarrError):
                adapter.file_details(8, 999)
            with self.assertRaises(RadarrError):
                adapter.file_details(9, 123)

    def test_missing_file_metadata_remains_unknown_and_does_not_leak_paths(self):
        raw = {"id": 8, "movieId": 123, "path": "/private/movie.mkv", "qualityCutoffNotMet": "false"}
        adapter = RadarrReadAdapter(RadarrClient("http://radarr", "secret", lambda path, params: raw))
        facts = adapter.file_details(8, 123)
        self.assertIsNone(facts["score"])
        self.assertIsNone(facts["format_ids"])
        self.assertIsNone(facts["language_ids"])
        self.assertIsNone(facts["quality_cutoff_not_met"])
        self.assertNotIn("path", facts)

    def test_core_formats_are_recognized_by_rules_not_names(self):
        specs = [{"name": "Arbitrary", "id": 5, "specifications": [{
            "implementation": "QualityModifierSpecification", "negate": False, "required": True,
            "fields": [{"name": "value", "value": 5}]}]}]
        adapter = RadarrReadAdapter(RadarrClient("http://radarr", "secret", lambda path, params: specs))
        self.assertEqual(adapter.core_formats(), [{"id": 5, "key": "remux", "label": "Remux", "category": "remux"}])
        specs[0]["specifications"][0]["negate"] = True
        self.assertEqual(adapter.core_formats(), [])
        specs[0]["specifications"][0].update(negate=False, implementation="ReleaseTitleSpecification")
        self.assertEqual(adapter.core_formats(), [])

    def test_quality_profile_rejects_mismatched_or_invalid_payload(self):
        for raw in ([], {"id": 4}, {"id": 3, "name": None}):
            adapter = RadarrReadAdapter(RadarrClient("http://radarr", "secret", lambda path, params: raw))
            with self.assertRaises(RadarrError):
                adapter.quality_profile(3)

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
