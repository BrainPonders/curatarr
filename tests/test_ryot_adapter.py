"""Tests for the read-only Ryot adapter."""

from __future__ import annotations

import unittest
from typing import Any

from curatarr.domain import DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.ryot import RyotClient, RyotError, RyotReadAdapter, RyotStateCollections


def _client_with_responses(responses: list[dict[str, Any]]) -> RyotClient:
    remaining = list(responses)

    def transport(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not remaining:
            raise AssertionError("No fake Ryot response left.")
        return remaining.pop(0)

    return RyotClient(url="http://ryot/graphql", api_key="secret", transport=transport)


class RyotClientTests(unittest.TestCase):
    def test_graphql_errors_raise_typed_error(self) -> None:
        client = _client_with_responses([{"errors": [{"message": "bad query"}]}])

        with self.assertRaises(RyotError):
            client.execute("query", {})

    def test_missing_data_raises_typed_error(self) -> None:
        client = _client_with_responses([{"not_data": {}}])

        with self.assertRaises(RyotError):
            client.execute("query", {})


class RyotReadAdapterTests(unittest.TestCase):
    def test_movie_snapshot_includes_native_only_records_but_not_series(self):
        client = _client_with_responses([
            {"data": {"userMetadataList": {"response": {"items": ["met_movie", "met_show"], "details": {"nextPage": None}}}}},
            {"data": {
                "m0": {"response": {"id": "met_movie", "title": "Movie", "lot": "MOVIE"}},
                "u0": {"response": {"hasInteracted": True, "collections": ["Owned"], "history": []}},
                "m1": {"response": {"id": "met_show", "title": "Show", "lot": "SHOW"}},
                "u1": {"response": {"hasInteracted": True, "collections": ["Library"], "history": []}}}}])
        item, = RyotReadAdapter(client).scan_movies().value
        self.assertEqual(item.title, "Movie")
        self.assertEqual(item.durable_states, frozenset())

    def test_movie_snapshot_rejects_missing_type_or_incomplete_collections(self):
        for metadata, user in (({"id": "met_1", "title": "Movie"}, {"collections": []}),
                               ({"id": "met_1", "title": "Movie", "lot": "MOVIE"}, {})):
            client = _client_with_responses([
                {"data": {"userMetadataList": {"response": {"items": ["met_1"], "details": {"nextPage": None}}}}},
                {"data": {"m0": {"response": metadata}, "u0": {"response": user}}}])
            with self.assertRaises(RyotError):
                RyotReadAdapter(client).scan_movies()

    def test_duplicate_metadata_across_pages_fails_snapshot_instead_of_deduplicating(self):
        client = _client_with_responses([
            {"data": {"userMetadataList": {"response": {"items": ["met_1"], "details": {"nextPage": 2}}}}},
            {"data": {"m0": {"response": {"id": "met_1", "title": "Movie", "lot": "MOVIE"}},
                      "u0": {"response": {"collections": []}}}},
            {"data": {"userMetadataList": {"response": {"items": ["met_1"], "details": {"nextPage": None}}}}},
            {"data": {"m0": {"response": {"id": "met_1", "title": "Movie", "lot": "MOVIE"}},
                      "u0": {"response": {"collections": []}}}}])
        with self.assertRaises(RyotError):
            RyotReadAdapter(client).scan_movies()

    def test_collection_mutations_remain_disabled(self):
        adapter = RyotReadAdapter(_client_with_responses([]))
        for state in DurableState:
            for action in (adapter.apply_state, adapter.remove_state):
                with self.subTest(state=state, action=action.__name__), self.assertRaises(NotImplementedError):
                    action(MediaIdentity("ryot", "met_1"), state)

    def test_library_and_acquisition_require_their_own_explicit_memberships(self):
        from curatarr.integrations.ryot.adapter import _media_item_from_raw
        for names, expected in (([], set()), (["Owned"], set()), (["Custom"], set()),
                                (["Library"], {DurableState.LIBRARY}),
                                (["Previously acquired"], {DurableState.PREVIOUSLY_ACQUIRED}),
                                (["Deleted"], {DurableState.DELETED}),
                                (["Completed"], {DurableState.WATCHED})):
            with self.subTest(names=names):
                item = _media_item_from_raw({"id": "met_1", "title": "Movie", "collections": names,
                                             "history": []}, RyotStateCollections())
                self.assertEqual(item.durable_states, frozenset(expected))
                self.assertEqual(item.collection_names, tuple(names))

    def test_six_configured_collections_map_in_both_directions(self):
        mappings = {"library": DurableState.LIBRARY, "archived": DurableState.ARCHIVED,
                    "previously_acquired": DurableState.PREVIOUSLY_ACQUIRED, "deleted": DurableState.DELETED,
                    "radarr": DurableState.RADARR, "sonarr": DurableState.SONARR}
        collections = RyotStateCollections(**{key: "Household " + key for key in mappings})
        for key, state in mappings.items():
            name = "Household " + key
            self.assertEqual(collections.state_for_collection(name.upper()), state)
            self.assertEqual(collections.collection_for_state(state), name)
        self.assertIsNone(collections.state_for_collection("Owned"))
        self.assertIsNone(collections.collection_for_state(DurableState.WATCHED))

    def test_missing_or_malformed_collection_evidence_fails_instead_of_clearing_state(self):
        for user in ({"hasInteracted": True}, {"collections": None}, {"collections": [{}]}, {"collections": [42]}):
            with self.subTest(user=user):
                client = _client_with_responses([{"data": {
                    "metadataDetails": {"response": {"id": "met_1", "title": "Movie"}},
                    "userMetadataDetails": {"response": user}}}])
                with self.assertRaises(RyotError):
                    RyotReadAdapter(client).get_item(MediaIdentity("ryot", "met_1"))

    def test_default_scan_includes_all_custom_states_and_watched_not_owned_only(self):
        names = ["Library", "Archived", "Previously acquired", "Deleted", "Radarr", "Sonarr", "Completed", "Owned"]
        batch = {}
        for index, name in enumerate(names):
            batch[f"m{index}"] = {"response": {"id": f"met_{index}", "title": name}}
            batch[f"u{index}"] = {"response": {"hasInteracted": True, "collections": [name], "history": []}}
        client = _client_with_responses([
            {"data": {"userMetadataList": {"response": {"items": [f"met_{i}" for i in range(len(names))],
                                                       "details": {"nextPage": None}}}}},
            {"data": batch}])
        items = RyotReadAdapter(client).scan_collection_memberships(frozenset()).value
        self.assertEqual([item.title for item in items], names[:-1])

    def test_collection_completed_without_history_keeps_unknown_count(self):
        from curatarr.integrations.ryot.adapter import _media_item_from_raw
        item = _media_item_from_raw({"title": "Movie", "collections": ["Completed"]}, RyotStateCollections())
        self.assertTrue(item.is_watched)
        self.assertIsNone(item.completed_watch_count)
        self.assertIsNone(item.last_watched_date)

    def test_get_item_maps_completed_to_watched_and_collections_to_durable_states(self) -> None:
        client = _client_with_responses(
            [
                {
                    "data": {
                        "metadataDetails": {
                            "response": {
                                "id": "met_1",
                                "title": "Spider-Man 2",
                                "identifier": "558",
                                "source": "TMDB",
                                "lot": "MOVIE",
                                "publishYear": 2004,
                            }
                        },
                        "userMetadataDetails": {
                            "response": {
                                "seenByUserCount": 1,
                                "collections": [
                                    {"details": {"collectionName": "Owned"}},
                                    {"details": {"collectionName": "Library"}},
                                    {"details": {"collectionName": "Previously acquired"}},
                                    {"details": {"collectionName": "Deleted"}},
                                    {"details": {"collectionName": "Archived"}},
                                    {"details": {"collectionName": "Radarr"}},
                                ],
                            }
                        },
                    }
                }
            ]
        )
        adapter = RyotReadAdapter(client)

        result = adapter.get_item(MediaIdentity("ryot", "met_1"))

        self.assertIsInstance(result.value, MediaItem)
        assert result.value is not None
        self.assertEqual(result.value.media_type, MediaType.MOVIE)
        self.assertEqual(result.value.identities, frozenset({MediaIdentity("ryot", "met_1"), MediaIdentity("tmdb", "558")}))
        self.assertIn(DurableState.LIBRARY, result.value.durable_states)
        self.assertIn(DurableState.WATCHED, result.value.durable_states)
        self.assertIn(DurableState.PREVIOUSLY_ACQUIRED, result.value.durable_states)
        self.assertIn(DurableState.DELETED, result.value.durable_states)
        self.assertIn("Owned", result.value.collection_names)
        self.assertNotIn("owned", {state.value for state in result.value.durable_states})
        self.assertIn(DurableState.ARCHIVED, result.value.durable_states)
        self.assertIn(DurableState.RADARR, result.value.durable_states)

    def test_scan_collection_memberships_maps_configured_collections(self) -> None:
        client = _client_with_responses(
            [
                {
                    "data": {
                        "userMetadataList": {
                            "response": {
                                "items": ["met_show"], "details": {"nextPage": None},
                            }
                        }
                    }
                },
                {
                    "data": {
                        "m0": {"response": {
                                        "id": "met_show",
                                        "title": "Example Show",
                                        "identifier": "121361",
                                        "source": "TVDB",
                                        "lot": "SHOW",
                                        "publishYear": "2011",
                        }},
                        "u0": {"response": {"hasInteracted": True,
                                 "collections": [{"details": {"collectionName": "Active Shows"}}]}},
                    }
                }
            ]
        )
        adapter = RyotReadAdapter(
            client,
            collections=RyotStateCollections(sonarr="Active Shows"),
        )

        result = adapter.scan_collection_memberships(frozenset({DurableState.SONARR}))

        self.assertEqual(len(result.value), 1)
        item = result.value[0]
        self.assertEqual(item.media_type, MediaType.SERIES)
        self.assertIn(MediaIdentity("tvdb", "121361"), item.identities)
        self.assertNotIn(DurableState.LIBRARY, item.durable_states)
        self.assertIn(DurableState.SONARR, item.durable_states)

    def test_non_ryot_identity_is_resolved_before_details_are_loaded(self) -> None:
        seen_variables = []

        def transport(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            seen_variables.append(variables)
            if "userMetadataList" in query:
                return {
                    "data": {
                        "userMetadataList": {
                            "response": {
                                "items": ["met_1"], "details": {"nextPage": None},
                            }
                        }
                    }
                }
            if "CuratarrLibraryDetails" in query:
                return {"data": {"m0": {"response": {
                                        "id": "met_1",
                                        "title": "Spider-Man 2",
                                        "identifier": "558",
                                        "source": "TMDB",
                                        "lot": "MOVIE",
                }}}}
            return {
                "data": {
                    "metadataDetails": {
                        "response": {
                            "id": "met_1",
                            "title": "Spider-Man 2",
                            "identifier": "558",
                            "source": "TMDB",
                            "lot": "MOVIE",
                        }
                    },
                    "userMetadataDetails": {"response": {"collections": []}},
                }
            }

        adapter = RyotReadAdapter(RyotClient(url="http://ryot/graphql", api_key="secret", transport=transport))

        result = adapter.get_item(MediaIdentity("tmdb", "558"))

        self.assertIsNotNone(result.value)
        self.assertEqual(seen_variables[0], {"input": {"search": {"page": 1, "take": 50},
                                                      "sort": {"by": "TITLE", "order": "ASC"}}})
        self.assertEqual(seen_variables[1], {"id0": "met_1"})
        self.assertEqual(seen_variables[2], {"metadataId": "met_1"})

    def test_pagination_finds_exact_identity_on_later_page(self):
        def metadata(id, identifier):
            return {"id": id, "identifier": identifier, "source": "TMDB", "lot": "MOVIE", "title": "Same title"}
        target = metadata("met_2", "558")
        client = _client_with_responses([
            {"data": {"userMetadataList": {"response": {"items": ["met_1"], "details": {"nextPage": 2}}}}},
            {"data": {"m0": {"response": metadata("met_1", "999")}}},
            {"data": {"userMetadataList": {"response": {"items": ["met_2"], "details": {"nextPage": None}}}}},
            {"data": {"m0": {"response": target}}},
            {"data": {"metadataDetails": {"response": target}, "userMetadataDetails": {"response": {
                "hasInteracted": True, "history": [{"state": "COMPLETED", "progress": "100"}], "collections": []}}}},
        ])
        result = RyotReadAdapter(client).get_item(MediaIdentity("tmdb", "558"))
        self.assertIn(MediaIdentity("ryot", "met_2"), result.value.identities)
        self.assertTrue(result.value.is_watched)

    def test_incomplete_library_page_does_not_become_false_absence(self):
        for payload in ({}, {"items": []}, {"items": [], "details": {}}):
            with self.subTest(payload=payload):
                client = _client_with_responses([{"data": {"userMetadataList": {"response": payload}}}])
                with self.assertRaises(RyotError):
                    RyotReadAdapter(client).get_item(MediaIdentity("tmdb", "558"))

    def test_repeated_pagination_is_an_error(self):
        client = _client_with_responses([{"data": {"userMetadataList": {"response": {
            "items": [], "details": {"nextPage": 1}}}}}])
        with self.assertRaises(RyotError):
            RyotReadAdapter(client).get_item(MediaIdentity("tmdb", "558"))

    def test_unassociated_global_metadata_is_not_user_library(self):
        client = _client_with_responses([{"data": {
            "metadataDetails": {"response": {"id": "met_1", "title": "Movie"}},
            "userMetadataDetails": {"response": {"hasInteracted": False, "history": [], "collections": []}},
        }}])
        self.assertIsNone(RyotReadAdapter(client).get_item(MediaIdentity("ryot", "met_1")).value)

    def test_in_progress_history_does_not_count_as_completed(self):
        client = _client_with_responses([{"data": {
            "metadataDetails": {"response": {"id": "met_1", "title": "Movie"}},
            "userMetadataDetails": {"response": {"hasInteracted": True, "seenByUserCount": 1,
                "history": [{"state": "IN_PROGRESS", "progress": "20"}], "collections": []}},
        }}])
        self.assertFalse(RyotReadAdapter(client).get_item(MediaIdentity("ryot", "met_1")).value.is_watched)

    def test_detail_queries_select_objects_and_do_not_search_external_id_as_title(self):
        from curatarr.integrations.ryot.adapter import METADATA_BY_ID_QUERY, USER_METADATA_LIST_QUERY
        self.assertIn("response { id title", METADATA_BY_ID_QUERY)
        self.assertIn("history { state progress finishedOn }", METADATA_BY_ID_QUERY)
        self.assertIn("UserMetadataListInput!", USER_METADATA_LIST_QUERY)

    def test_completed_view_count_and_last_date_exclude_in_progress_and_edit_times(self):
        from curatarr.integrations.ryot.adapter import _media_item_from_raw
        raw = {"id": "1", "title": "Movie", "history": [
            {"state": "COMPLETED", "finishedOn": "2020-08-10T20:09:00Z"},
            {"state": "COMPLETED", "finishedOn": "2024-06-01T12:00:00Z"},
            {"state": "IN_PROGRESS", "lastUpdatedOn": "2026-10-01T12:00:00Z"},
        ]}
        item = _media_item_from_raw(raw, RyotStateCollections())
        self.assertEqual(item.completed_watch_count, 2)
        self.assertEqual(item.last_watched_date, "2024-06-01")
        raw["history"][1].pop("finishedOn")
        self.assertIsNone(_media_item_from_raw(raw, RyotStateCollections()).last_watched_date)

    def test_completed_collection_without_events_is_not_zero_viewings(self):
        from curatarr.integrations.ryot.adapter import _media_item_from_raw
        raw = {"id": "1", "title": "Movie", "history": [], "collections": ["Completed"]}
        item = _media_item_from_raw(raw, RyotStateCollections())
        self.assertTrue(item.is_watched)
        self.assertIsNone(item.completed_watch_count)
        raw["collections"] = []
        self.assertEqual(_media_item_from_raw(raw, RyotStateCollections()).completed_watch_count, 0)


if __name__ == "__main__":
    unittest.main()
