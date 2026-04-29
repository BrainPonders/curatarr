#!/usr/bin/env python3
"""Standalone Ryot cleanup helper for temporary collections and Radarr overlay."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional

import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning


USER_COLLECTIONS_LIST_QUERY = """
query UserCollectionsList {
  userCollectionsList {
    response {
      id
      name
      count
      creator {
        id
        name
      }
    }
  }
}
"""

USER_METADATA_LIST_QUERY = """
query UserMetadataList($input: UserMetadataListInput!) {
  userMetadataList(input: $input) {
    response {
      items
      details {
        totalItems
        nextPage
      }
    }
  }
}
"""

METADATA_DETAILS_QUERY = """
query MetadataDetails($metadataId: String!) {
  metadataDetails(metadataId: $metadataId) {
    response {
      id
      title
      identifier
      lot
      source
      publishYear
    }
  }
}
"""

USER_METADATA_DETAILS_QUERY = """
query UserMetadataDetails($metadataId: String!) {
  userMetadataDetails(metadataId: $metadataId) {
    response {
      collections {
        details {
          collectionName
        }
      }
    }
  }
}
"""

METADATA_SEARCH_QUERY = """
query MetadataSearch($input: MetadataSearchInput!) {
  metadataSearch(input: $input) {
    response {
      items
      details {
        totalItems
      }
    }
  }
}
"""

INPUT_FIELDS_QUERY = """
query InputFields($name: String!) {
  __type(name: $name) {
    inputFields {
      name
      type {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType {
                kind
                name
              }
            }
          }
        }
      }
    }
  }
}
"""

TYPE_FIELDS_QUERY = """
query TypeFields($name: String!) {
  __type(name: $name) {
    fields {
      name
      type {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType {
                kind
                name
              }
            }
          }
        }
      }
    }
  }
}
"""

MUTATION_ROOT_QUERY = """
query MutationRootShape {
  __schema {
    mutationType {
      name
    }
  }
}
"""

QUERY_ROOT_QUERY = """
query QueryRootShape {
  __schema {
    queryType {
      name
    }
  }
}
"""

ENUM_VALUES_QUERY = """
query EnumValues($name: String!) {
  __type(name: $name) {
    enumValues {
      name
    }
  }
}
"""

ADD_ENTITIES_MUTATION_SCALAR = """
mutation DeployAddEntitiesToCollectionJob($input: ChangeCollectionToEntitiesInput!) {
  deployAddEntitiesToCollectionJob(input: $input)
}
"""

ADD_ENTITIES_MUTATION_OBJECT = """
mutation DeployAddEntitiesToCollectionJob($input: ChangeCollectionToEntitiesInput!) {
  deployAddEntitiesToCollectionJob(input: $input) {
    __typename
  }
}
"""

REMOVE_ENTITIES_MUTATION_SCALAR = """
mutation DeployRemoveEntitiesFromCollectionJob($input: ChangeCollectionToEntitiesInput!) {
  deployRemoveEntitiesFromCollectionJob(input: $input)
}
"""

REMOVE_ENTITIES_MUTATION_OBJECT = """
mutation DeployRemoveEntitiesFromCollectionJob($input: ChangeCollectionToEntitiesInput!) {
  deployRemoveEntitiesFromCollectionJob(input: $input) {
    __typename
  }
}
"""


class IntegrationError(RuntimeError):
    """Raised when an upstream integration request fails."""


class HttpJsonClient:
    """Minimal JSON-over-HTTP helper."""

    def __init__(self, *, timeout_seconds: int = 15, verify_ssl: bool = True) -> None:
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl

    def _prepare_request(self) -> None:
        if self.verify_ssl is False:
            disable_warnings(InsecureRequestWarning)

    def get(self, url: str, *, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None):
        try:
            self._prepare_request()
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IntegrationError(f"GET request failed for {url}: {exc}") from exc
        return response.json()

    def post(self, url: str, *, headers: Optional[Dict[str, str]] = None, json_body: Optional[Dict[str, Any]] = None):
        try:
            self._prepare_request()
            response = requests.post(
                url,
                headers=headers,
                json=json_body,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IntegrationError(f"POST request failed for {url}: {exc}") from exc
        return response.json()


class RadarrClient:
    """Minimal Radarr client for movie inventory enumeration."""

    def __init__(self, url: str, api_key: str, *, timeout_seconds: int = 15, verify_ssl: bool = True) -> None:
        self.http = HttpJsonClient(timeout_seconds=timeout_seconds, verify_ssl=verify_ssl)
        self.base_url = url.rstrip("/")
        self.api_key = api_key

    def list_movies(self) -> List[dict]:
        return list(self.http.get(f"{self.base_url}/api/v3/movie", headers={"X-Api-Key": self.api_key}) or [])


class RyotClient:
    """Minimal Ryot GraphQL helper for collection cleanup and sync."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        graphql_path: str,
        verify_ssl: bool,
        timeout_seconds: int,
    ) -> None:
        self.http = HttpJsonClient(timeout_seconds=timeout_seconds, verify_ssl=verify_ssl)
        self.endpoint = f"{url.rstrip('/')}{graphql_path}"
        self.api_key = api_key

    def list_collections(self) -> List[dict]:
        payload = self._graphql(USER_COLLECTIONS_LIST_QUERY, {})
        return list(((payload.get("userCollectionsList") or {}).get("response")) or [])

    def list_user_movie_metadata(self, *, page_size: int) -> List[dict]:
        page = 1
        metadata_ids: List[str] = []
        while True:
            payload = self._graphql(
                USER_METADATA_LIST_QUERY,
                {
                    "input": {
                        "lot": "MOVIE",
                        "search": {
                            "page": page,
                            "take": page_size,
                        },
                    }
                },
            )
            response = ((payload.get("userMetadataList") or {}).get("response")) or {}
            items = list(response.get("items") or [])
            details = response.get("details") or {}
            metadata_ids.extend(items)
            next_page = details.get("nextPage")
            total_items = int(details.get("totalItems") or 0)
            if not items or next_page is None or len(metadata_ids) >= total_items:
                break
            page = int(next_page)

        movies = []
        for metadata_id in metadata_ids:
            details = self.metadata_details(metadata_id)
            if (details.get("lot") or "").upper() != "MOVIE":
                continue
            if (details.get("source") or "").upper() != "TMDB":
                continue
            try:
                tmdb_id = int(str(details.get("identifier")))
            except (TypeError, ValueError):
                continue
            user_details = self.user_metadata_details(metadata_id)
            collections = user_details.get("collections") or []
            collection_names = [
                (((item or {}).get("details") or {}).get("collectionName"))
                for item in collections
            ]
            movies.append(
                {
                    "metadata_id": metadata_id,
                    "tmdb_id": tmdb_id,
                    "title": details.get("title"),
                    "collection_names": [name for name in collection_names if name],
                }
            )
        return movies

    def metadata_search(self, *, query: str, page_size: int) -> List[str]:
        payload = self._graphql(
            METADATA_SEARCH_QUERY,
            {
                "input": {
                    "lot": "MOVIE",
                    "source": "TMDB",
                    "search": {
                        "query": query,
                        "page": 1,
                        "take": page_size,
                    },
                }
            },
        )
        return list((((payload.get("metadataSearch") or {}).get("response")) or {}).get("items") or [])

    def metadata_details(self, metadata_id: str) -> dict:
        payload = self._graphql(METADATA_DETAILS_QUERY, {"metadataId": metadata_id})
        return ((payload.get("metadataDetails") or {}).get("response")) or {}

    def user_metadata_details(self, metadata_id: str) -> dict:
        payload = self._graphql(USER_METADATA_DETAILS_QUERY, {"metadataId": metadata_id})
        return ((payload.get("userMetadataDetails") or {}).get("response")) or {}

    def get_input_object_fields(self, type_name: str) -> List[dict]:
        payload = self._graphql(INPUT_FIELDS_QUERY, {"name": type_name})
        fields = ((payload.get("__type") or {}).get("inputFields")) or []
        if not fields:
            raise RuntimeError(f"Could not inspect input fields for {type_name}.")
        return list(fields)

    def get_query_root_name(self) -> str:
        payload = self._graphql(QUERY_ROOT_QUERY, {})
        name = ((payload.get("__schema") or {}).get("queryType") or {}).get("name")
        if not name:
            raise RuntimeError("Could not resolve Ryot query root type.")
        return str(name)

    def get_field_type(self, parent_type_name: str, field_name: str) -> dict:
        payload = self._graphql(TYPE_FIELDS_QUERY, {"name": parent_type_name})
        fields = ((payload.get("__type") or {}).get("fields")) or []
        for field in fields:
            if field.get("name") == field_name:
                return field.get("type") or {}
        raise RuntimeError(f"Could not find field '{field_name}' on type '{parent_type_name}'.")

    def get_enum_values(self, enum_name: str) -> List[str]:
        payload = self._graphql(ENUM_VALUES_QUERY, {"name": enum_name})
        values = ((payload.get("__type") or {}).get("enumValues")) or []
        return [value.get("name") for value in values if value.get("name")]

    def collection_job_returns_object(self, field_name: str) -> bool:
        mutation_root_name = ((self._graphql(MUTATION_ROOT_QUERY, {}).get("__schema") or {}).get("mutationType") or {}).get("name")
        if not mutation_root_name:
            raise RuntimeError("Could not resolve Ryot mutation root type.")
        fields_payload = self._graphql(TYPE_FIELDS_QUERY, {"name": mutation_root_name})
        fields = ((fields_payload.get("__type") or {}).get("fields")) or []
        for field in fields:
            if field.get("name") == field_name:
                return _unwrap_type(field.get("type") or {}).get("kind") == "OBJECT"
        raise RuntimeError(f"Could not find {field_name} in Ryot schema.")

    def collection_contents_metadata_ids(self, *, collection_id: str, page_size: int) -> List[str]:
        input_fields = self.get_input_object_fields("CollectionContentsInput")
        field_names = {field.get("name") for field in input_fields}
        query_root_name = self.get_query_root_name()
        response_type = _unwrap_type(self.get_field_type(query_root_name, "collectionContents"))
        response_type_name = response_type.get("name")
        if not response_type_name:
            raise RuntimeError("Could not resolve collectionContents response type.")
        selection = self._build_selection_set(response_type_name, depth=4)
        page = 1
        metadata_ids: List[str] = []
        while True:
            input_payload: Dict[str, Any] = {}
            if "collectionId" in field_names:
                input_payload["collectionId"] = collection_id
            if "search" in field_names:
                input_payload["search"] = {"page": page, "take": page_size}
            query = (
                "query CollectionContents($input: CollectionContentsInput!) { "
                f"collectionContents(input: $input) {{ {selection} }} }}"
            )
            payload = self._graphql(query, {"input": input_payload})
            response = payload.get("collectionContents") or {}
            page_metadata_ids = _extract_metadata_ids(response)
            metadata_ids.extend(page_metadata_ids)
            next_page = _find_first_key(response, "nextPage")
            total_items = _find_first_key(response, "totalItems")
            metadata_ids = list(dict.fromkeys(metadata_ids))
            if "search" not in field_names:
                break
            if not next_page:
                break
            if isinstance(total_items, int) and len(metadata_ids) >= total_items:
                break
            page = int(next_page)
        return list(dict.fromkeys(metadata_ids))

    def _build_selection_set(self, type_name: str, *, depth: int, seen: Optional[set] = None) -> str:
        if depth <= 0:
            return "__typename"
        seen = set(seen or set())
        if type_name in seen:
            return "__typename"
        seen.add(type_name)
        payload = self._graphql(TYPE_FIELDS_QUERY, {"name": type_name})
        fields = ((payload.get("__type") or {}).get("fields")) or []
        parts = ["__typename"]
        for field in fields:
            field_name = field.get("name")
            if not field_name:
                continue
            field_type = _unwrap_type(field.get("type") or {})
            kind = field_type.get("kind")
            nested_name = field_type.get("name")
            if kind in {"SCALAR", "ENUM"}:
                parts.append(field_name)
                continue
            if kind == "OBJECT" and nested_name:
                nested_selection = self._build_selection_set(nested_name, depth=depth - 1, seen=seen)
                parts.append(f"{field_name} {{ {nested_selection} }}")
        return " ".join(parts)

    def add_entities_to_collection(
        self,
        *,
        creator_user_id: str,
        collection_name: str,
        entities: List[Any],
        returns_object: bool,
    ) -> dict:
        mutation = ADD_ENTITIES_MUTATION_OBJECT if returns_object else ADD_ENTITIES_MUTATION_SCALAR
        return self._graphql(
            mutation,
            {
                "input": {
                    "creatorUserId": creator_user_id,
                    "collectionName": collection_name,
                    "entities": entities,
                }
            },
        )

    def remove_entities_from_collection(
        self,
        *,
        creator_user_id: str,
        collection_name: str,
        entities: List[Any],
        returns_object: bool,
    ) -> dict:
        mutation = REMOVE_ENTITIES_MUTATION_OBJECT if returns_object else REMOVE_ENTITIES_MUTATION_SCALAR
        return self._graphql(
            mutation,
            {
                "input": {
                    "creatorUserId": creator_user_id,
                    "collectionName": collection_name,
                    "entities": entities,
                }
            },
        )

    def _graphql(self, query: str, variables: dict) -> dict:
        payload = self.http.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json_body={"query": query, "variables": variables},
        )
        errors = payload.get("errors") or []
        if errors:
            message = "; ".join(error.get("message", "Unknown GraphQL error") for error in errors)
            raise RuntimeError(f"Ryot GraphQL query failed: {message}")
        return payload.get("data") or {}


def resolve_settings_path(explicit_path: Optional[str] = None) -> Path:
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_path = os.environ.get("CURATARR_SETTINGS_FILE")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([Path.cwd() / "settings.py", Path.cwd() / "config" / "settings.py"])
    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file():
            return resolved.resolve()
    raise FileNotFoundError(
        "Could not find settings.py. Provide --settings-file, set CURATARR_SETTINGS_FILE, or place settings.py/config/settings.py in the working directory."
    )


def load_settings(explicit_path: Optional[str] = None) -> tuple[Path, ModuleType]:
    path = resolve_settings_path(explicit_path)
    spec = importlib.util.spec_from_file_location("curatarr_settings", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to create import spec for settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean temporary Ryot collections and apply the Radarr overlay collection.",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        help="Path to the runtime settings.py to load. Falls back to CURATARR_SETTINGS_FILE, ./settings.py, or ./config/settings.py.",
    )
    parser.add_argument(
        "--temp-collection",
        action="append",
        default=["Watchlist", "Reminders"],
        help="Temporary Ryot collection to empty. May be passed multiple times. Default: Watchlist and Reminders.",
    )
    parser.add_argument(
        "--radarr-collection",
        default="Radarr",
        help="Ryot collection name that should mirror current Radarr presence. Default: Radarr.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size for Ryot list queries. Default: 100.",
    )
    parser.add_argument(
        "--search-page-size",
        type=int,
        default=25,
        help="Metadata search candidate count when resolving missing Ryot metadata ids for Radarr items. Default: 25.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum number of collection entity updates per GraphQL mutation. Default: 100.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the cleanup and collection updates. Without this flag the script only prints the plan.",
    )
    return parser.parse_args()


def _unwrap_type(type_info: dict) -> dict:
    current = type_info or {}
    while current and current.get("kind") in {"NON_NULL", "LIST"}:
        current = current.get("ofType") or {}
    return current


def _field_required(type_info: dict) -> bool:
    return (type_info or {}).get("kind") == "NON_NULL"


def _chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _load_clients(settings_path: Optional[str]):
    resolved_path, settings_module = load_settings(settings_path)
    timeout_seconds = int(getattr(settings_module, "requests_timeout_seconds", 15))
    if not getattr(settings_module, "ryot_enabled", False):
        raise RuntimeError("ryot_enabled must be True for this helper.")
    if not getattr(settings_module, "radarr_enabled", False):
        raise RuntimeError("radarr_enabled must be True for this helper.")
    ryot_client = RyotClient(
        url=getattr(settings_module, "ryot_url", ""),
        api_key=getattr(settings_module, "ryot_api_key", ""),
        graphql_path=getattr(settings_module, "ryot_graphql_path", "/backend/graphql"),
        verify_ssl=bool(getattr(settings_module, "ryot_verify_ssl", True)),
        timeout_seconds=timeout_seconds,
    )
    radarr_client = RadarrClient(
        getattr(settings_module, "radarr_url", ""),
        getattr(settings_module, "radarr_api_key", ""),
        timeout_seconds=timeout_seconds,
        verify_ssl=bool(getattr(settings_module, "radarr_verify_ssl", True)),
    )
    return resolved_path, ryot_client, radarr_client


def _build_entity_payload_builder(client: RyotClient):
    payload = client._graphql(INPUT_FIELDS_QUERY, {"name": "ChangeCollectionToEntitiesInput"})
    fields = ((payload.get("__type") or {}).get("inputFields")) or []
    entities_field = next((field for field in fields if field.get("name") == "entities"), None)
    if not entities_field:
        raise RuntimeError("Could not inspect ChangeCollectionToEntitiesInput.entities")
    entities_type = _unwrap_type(entities_field.get("type") or {})
    kind = entities_type.get("kind")
    name = entities_type.get("name")
    if kind == "SCALAR":
        return lambda metadata_id: metadata_id
    if kind != "INPUT_OBJECT" or not name:
        raise RuntimeError(f"Unsupported Ryot entities input type: {kind} {name}")

    fields = client.get_input_object_fields(name)
    field_specs = []
    for field in fields:
        field_name = field.get("name") or ""
        field_type = _unwrap_type(field.get("type") or {})
        field_specs.append((field_name, field_type, _field_required(field.get("type") or {})))

    lot_value_cache: Dict[str, str] = {}

    def lot_value_for(field_name: str, field_type: dict) -> str:
        enum_name = field_type.get("name") or ""
        cache_key = f"{field_name}:{enum_name}"
        cached = lot_value_cache.get(cache_key)
        if cached:
            return cached
        enum_values = client.get_enum_values(enum_name) if enum_name else []
        for candidate in ["METADATA", "MOVIE", "MEDIA", "SHOW"]:
            if candidate in enum_values:
                lot_value_cache[cache_key] = candidate
                return candidate
        if enum_values:
            lot_value_cache[cache_key] = enum_values[0]
            return enum_values[0]
        raise RuntimeError(
            f"Could not determine a valid enum value for lot field '{field_name}' with enum '{enum_name}'."
        )

    def builder(metadata_id: str) -> dict:
        payload: Dict[str, Any] = {}
        for field_name, field_type, required in field_specs:
            if field_name in {"id", "entityId", "metadataId"}:
                payload[field_name] = metadata_id
                continue
            if field_name in {"lot", "entityLot", "mediaLot"}:
                payload[field_name] = lot_value_for(field_name, field_type)
                continue
            if required:
                raise RuntimeError(
                    f"Unsupported required entities field '{field_name}' on {name}."
                )
        return payload

    return builder


def _resolve_collection_owner_id(collections: List[dict], target_collection_name: str) -> str:
    for collection in collections:
        if (collection.get("name") or "").casefold() == target_collection_name.casefold():
            creator = collection.get("creator") or {}
            creator_id = creator.get("id")
            if creator_id:
                return str(creator_id)
            raise RuntimeError(f"Collection '{target_collection_name}' has no creator id.")
    raise RuntimeError(f"Collection '{target_collection_name}' was not found in Ryot.")


def _resolve_collection_id(collections: List[dict], target_collection_name: str) -> str:
    for collection in collections:
        if (collection.get("name") or "").casefold() == target_collection_name.casefold():
            collection_id = collection.get("id")
            if collection_id:
                return str(collection_id)
            raise RuntimeError(f"Collection '{target_collection_name}' has no id.")
    raise RuntimeError(f"Collection '{target_collection_name}' was not found in Ryot.")


def _metadata_ids_in_collection(user_movies: List[dict], collection_name: str) -> List[str]:
    wanted = collection_name.casefold()
    metadata_ids = []
    for movie in user_movies:
        names = [name.casefold() for name in movie.get("collection_names") or []]
        if wanted in names:
            metadata_ids.append(movie["metadata_id"])
    return list(dict.fromkeys(metadata_ids))


def _extract_metadata_ids(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, str):
        if value.startswith("met_"):
            found.append(value)
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(_extract_metadata_ids(item))
        return found
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_extract_metadata_ids(item))
        return found
    return found


def _find_first_key(value: Any, target_key: str) -> Optional[Any]:
    if isinstance(value, dict):
        if target_key in value:
            return value[target_key]
        for item in value.values():
            found = _find_first_key(item, target_key)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_first_key(item, target_key)
            if found is not None:
                return found
    return None


def _candidate_queries(title: str, year: Optional[int]) -> List[str]:
    queries = [title.strip()]
    if title and year:
        queries.append(f"{title.strip()} {year}")
    deduped = []
    seen = set()
    for query in queries:
        if not query or query in seen:
            continue
        seen.add(query)
        deduped.append(query)
    return deduped


def _resolve_radarr_movie_metadata_ids(
    *,
    ryot_client: RyotClient,
    radarr_movies: List[dict],
    existing_by_tmdb: Dict[int, str],
    search_page_size: int,
) -> tuple[Dict[int, str], List[dict]]:
    resolved = dict(existing_by_tmdb)
    unresolved: List[dict] = []

    for movie in radarr_movies:
        try:
            tmdb_id = int(movie.get("tmdbId"))
        except (TypeError, ValueError):
            continue
        if tmdb_id in resolved:
            continue
        title = str(movie.get("title") or "").strip()
        year = movie.get("year")
        matched_metadata_id = None
        for query in _candidate_queries(title, year):
            for metadata_id in ryot_client.metadata_search(query=query, page_size=search_page_size):
                details = ryot_client.metadata_details(metadata_id)
                if (details.get("lot") or "").upper() != "MOVIE":
                    continue
                if (details.get("source") or "").upper() != "TMDB":
                    continue
                try:
                    identifier = int(str(details.get("identifier")))
                except (TypeError, ValueError):
                    continue
                if identifier == tmdb_id:
                    matched_metadata_id = metadata_id
                    break
            if matched_metadata_id:
                break
        if matched_metadata_id:
            resolved[tmdb_id] = matched_metadata_id
        else:
            unresolved.append(
                {
                    "tmdb_id": tmdb_id,
                    "title": title,
                    "year": year,
                    "radarr_movie_id": movie.get("id"),
                    "monitored": bool(movie.get("monitored")),
                    "has_file": bool(movie.get("hasFile") or movie.get("movieFile")),
                }
            )
    return resolved, unresolved


def main() -> int:
    args = _parse_args()
    settings_path, ryot_client, radarr_client = _load_clients(args.settings_file)
    collections = ryot_client.list_collections()
    entity_payload_builder = _build_entity_payload_builder(ryot_client)
    add_returns_object = ryot_client.collection_job_returns_object("deployAddEntitiesToCollectionJob")
    remove_returns_object = ryot_client.collection_job_returns_object("deployRemoveEntitiesFromCollectionJob")

    user_movies = ryot_client.list_user_movie_metadata(page_size=args.page_size)
    ryot_by_tmdb = {item["tmdb_id"]: item["metadata_id"] for item in user_movies}
    radarr_movies = list(radarr_client.list_movies())
    resolved_by_tmdb, unresolved_radarr = _resolve_radarr_movie_metadata_ids(
        ryot_client=ryot_client,
        radarr_movies=radarr_movies,
        existing_by_tmdb=ryot_by_tmdb,
        search_page_size=args.search_page_size,
    )

    temp_plans = []
    for collection_name in args.temp_collection:
        creator_user_id = _resolve_collection_owner_id(collections, collection_name)
        collection_id = _resolve_collection_id(collections, collection_name)
        metadata_ids = ryot_client.collection_contents_metadata_ids(
            collection_id=collection_id,
            page_size=args.page_size,
        )
        temp_plans.append(
            {
                "collection_name": collection_name,
                "creator_user_id": creator_user_id,
                "collection_id": collection_id,
                "metadata_ids": metadata_ids,
            }
        )

    radarr_collection_owner_id = _resolve_collection_owner_id(collections, args.radarr_collection)
    radarr_collection_id = _resolve_collection_id(collections, args.radarr_collection)
    current_radarr_collection_ids = ryot_client.collection_contents_metadata_ids(
        collection_id=radarr_collection_id,
        page_size=args.page_size,
    )
    radarr_metadata_ids = []
    for movie in radarr_movies:
        try:
            tmdb_id = int(movie.get("tmdbId"))
        except (TypeError, ValueError):
            continue
        metadata_id = resolved_by_tmdb.get(tmdb_id)
        if metadata_id:
            radarr_metadata_ids.append(metadata_id)
    radarr_metadata_ids = list(dict.fromkeys(radarr_metadata_ids))
    radarr_target_ids = set(radarr_metadata_ids)
    radarr_remove_ids = [metadata_id for metadata_id in current_radarr_collection_ids if metadata_id not in radarr_target_ids]
    radarr_add_ids = [metadata_id for metadata_id in radarr_metadata_ids if metadata_id not in set(current_radarr_collection_ids)]

    summary = {
        "settings_path": str(settings_path),
        "execute": bool(args.execute),
        "temporary_collections": [],
        "radarr_overlay": {
            "collection_name": args.radarr_collection,
            "creator_user_id": radarr_collection_owner_id,
            "resolved_total": len(radarr_metadata_ids),
            "unresolved_total": len(unresolved_radarr),
            "current_members_total": len(current_radarr_collection_ids),
            "add_total": len(radarr_add_ids),
            "remove_total": len(radarr_remove_ids),
            "add_batch_count": len(list(_chunked(radarr_add_ids, args.batch_size))),
            "remove_batch_count": len(list(_chunked(radarr_remove_ids, args.batch_size))),
            "executed": False,
        },
    }
    if unresolved_radarr:
        summary["radarr_overlay"]["unresolved_sample"] = unresolved_radarr[:10]

    for plan in temp_plans:
        collection_summary = {
            "collection_name": plan["collection_name"],
            "creator_user_id": plan["creator_user_id"],
            "remove_total": len(plan["metadata_ids"]),
            "batch_count": len(list(_chunked(plan["metadata_ids"], args.batch_size))),
            "executed": False,
        }
        summary["temporary_collections"].append(collection_summary)

    if args.execute:
        for plan, collection_summary in zip(temp_plans, summary["temporary_collections"]):
            entities = [entity_payload_builder(metadata_id) for metadata_id in plan["metadata_ids"]]
            for batch in _chunked(entities, args.batch_size):
                if batch:
                    ryot_client.remove_entities_from_collection(
                        creator_user_id=plan["creator_user_id"],
                        collection_name=plan["collection_name"],
                        entities=batch,
                        returns_object=remove_returns_object,
                    )
            collection_summary["executed"] = True

        remove_entities = [entity_payload_builder(metadata_id) for metadata_id in radarr_remove_ids]
        for batch in _chunked(remove_entities, args.batch_size):
            if batch:
                ryot_client.remove_entities_from_collection(
                    creator_user_id=radarr_collection_owner_id,
                    collection_name=args.radarr_collection,
                    entities=batch,
                    returns_object=remove_returns_object,
                )

        add_entities = [entity_payload_builder(metadata_id) for metadata_id in radarr_add_ids]
        for batch in _chunked(add_entities, args.batch_size):
            if batch:
                ryot_client.add_entities_to_collection(
                    creator_user_id=radarr_collection_owner_id,
                    collection_name=args.radarr_collection,
                    entities=batch,
                    returns_object=add_returns_object,
                )
        summary["radarr_overlay"]["executed"] = True

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
