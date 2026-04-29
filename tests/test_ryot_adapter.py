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
        self.assertIn(DurableState.OWNED, result.value.durable_states)
        self.assertIn(DurableState.ARCHIVED, result.value.durable_states)
        self.assertIn(DurableState.RADARR, result.value.durable_states)

    def test_scan_collection_memberships_maps_configured_collections(self) -> None:
        client = _client_with_responses(
            [
                {
                    "data": {
                        "collectionContents": {
                            "response": {
                                "items": [
                                    {
                                        "id": "met_show",
                                        "title": "Example Show",
                                        "identifier": "121361",
                                        "source": "TVDB",
                                        "lot": "SHOW",
                                        "publishYear": "2011",
                                    }
                                ]
                            }
                        }
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
        self.assertIn(DurableState.LIBRARY, item.durable_states)
        self.assertIn(DurableState.SONARR, item.durable_states)

    def test_non_ryot_identity_is_resolved_before_details_are_loaded(self) -> None:
        seen_variables = []

        def transport(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            seen_variables.append(variables)
            if "metadataSearch" in query:
                return {
                    "data": {
                        "metadataSearch": {
                            "response": {
                                "items": [
                                    {
                                        "id": "met_1",
                                        "title": "Spider-Man 2",
                                        "identifier": "558",
                                        "source": "TMDB",
                                        "lot": "MOVIE",
                                    }
                                ]
                            }
                        }
                    }
                }
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
        self.assertEqual(seen_variables[0], {"query": "558"})
        self.assertEqual(seen_variables[1], {"metadataId": "met_1"})


if __name__ == "__main__":
    unittest.main()
