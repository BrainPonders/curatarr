#!/usr/bin/env python3
"""Standalone dry-run helper for movie Owned cleanup against Radarr state."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

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

QUERY_ROOT_QUERY = """
query QueryRootShape {
  __schema {
    queryType {
      name
    }
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

    def get(self, url: str, *, headers: Optional[Dict[str, str]] = None):
        try:
            self._prepare_request()
            response = requests.get(
                url,
                headers=headers,
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
    """Minimal Ryot GraphQL helper for Owned cleanup dry runs."""

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
            collection_names = [
                (((item or {}).get("details") or {}).get("collectionName"))
                for item in (user_details.get("collections") or [])
            ]
            movies.append(
                {
                    "metadata_id": metadata_id,
                    "tmdb_id": tmdb_id,
                    "title": details.get("title"),
                    "year": details.get("publishYear"),
                    "collection_names": [name for name in collection_names if name],
                }
            )
        return movies

    def metadata_details(self, metadata_id: str) -> dict:
        payload = self._graphql(METADATA_DETAILS_QUERY, {"metadataId": metadata_id})
        return ((payload.get("metadataDetails") or {}).get("response")) or {}

    def user_metadata_details(self, metadata_id: str) -> dict:
        payload = self._graphql(USER_METADATA_DETAILS_QUERY, {"metadataId": metadata_id})
        return ((payload.get("userMetadataDetails") or {}).get("response")) or {}

    def get_query_root_name(self) -> str:
        payload = self._graphql(QUERY_ROOT_QUERY, {})
        name = ((payload.get("__schema") or {}).get("queryType") or {}).get("name")
        if not name:
            raise RuntimeError("Could not resolve Ryot query root type.")
        return str(name)

    def get_input_object_fields(self, type_name: str) -> List[dict]:
        payload = self._graphql(INPUT_FIELDS_QUERY, {"name": type_name})
        fields = ((payload.get("__type") or {}).get("inputFields")) or []
        if not fields:
            raise RuntimeError(f"Could not inspect input fields for {type_name}.")
        return list(fields)

    def get_field_type(self, parent_type_name: str, field_name: str) -> dict:
        payload = self._graphql(TYPE_FIELDS_QUERY, {"name": parent_type_name})
        fields = ((payload.get("__type") or {}).get("fields")) or []
        for field in fields:
            if field.get("name") == field_name:
                return field.get("type") or {}
        raise RuntimeError(f"Could not find field '{field_name}' on type '{parent_type_name}'.")

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
            metadata_ids = list(dict.fromkeys(metadata_ids))
            next_page = _find_first_key(response, "nextPage")
            total_items = _find_first_key(response, "totalItems")
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
        description="Dry-run movie Owned cleanup against Completed and Radarr state.",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        help="Path to the runtime settings.py to load. Falls back to CURATARR_SETTINGS_FILE, ./settings.py, or ./config/settings.py.",
    )
    parser.add_argument(
        "--owned-collection",
        default="Owned",
        help="Ryot collection name to prune. Default: Owned.",
    )
    parser.add_argument(
        "--completed-collection",
        default="Completed",
        help="Ryot collection that counts as watched. Default: Completed.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where summary.json and review CSVs will be written.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size for Ryot list queries. Default: 100.",
    )
    return parser.parse_args()


def _unwrap_type(type_info: dict) -> dict:
    current = type_info or {}
    while current and current.get("kind") in {"NON_NULL", "LIST"}:
        current = current.get("ofType") or {}
    return current


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


def _resolve_collection(collections: List[dict], target_collection_name: str) -> dict:
    for collection in collections:
        if (collection.get("name") or "").casefold() == target_collection_name.casefold():
            return collection
    raise RuntimeError(f"Collection '{target_collection_name}' was not found in Ryot.")


def _write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    settings_path, ryot_client, radarr_client = _load_clients(args.settings_file)
    collections = ryot_client.list_collections()
    owned_collection = _resolve_collection(collections, args.owned_collection)
    completed_collection = _resolve_collection(collections, args.completed_collection)

    owned_metadata_ids = set(
        ryot_client.collection_contents_metadata_ids(
            collection_id=str(owned_collection.get("id")),
            page_size=args.page_size,
        )
    )
    completed_metadata_ids = set(
        ryot_client.collection_contents_metadata_ids(
            collection_id=str(completed_collection.get("id")),
            page_size=args.page_size,
        )
    )

    user_movies = ryot_client.list_user_movie_metadata(page_size=args.page_size)
    user_movie_by_metadata_id = {item["metadata_id"]: item for item in user_movies}

    radarr_by_tmdb: Dict[int, dict] = {}
    for movie in radarr_client.list_movies():
        try:
            tmdb_id = int(movie.get("tmdbId"))
        except (TypeError, ValueError):
            continue
        radarr_by_tmdb[tmdb_id] = {
            "radarr_movie_id": movie.get("id"),
            "radarr_present": bool(movie.get("hasFile") or movie.get("movieFile")),
            "radarr_monitored": bool(movie.get("monitored")),
        }

    owned_movie_rows = []
    owned_keep_rows = []
    owned_remove_rows = []

    for metadata_id in sorted(owned_metadata_ids):
        movie = user_movie_by_metadata_id.get(metadata_id)
        if not movie:
            continue
        tmdb_id = movie["tmdb_id"]
        radarr_item = radarr_by_tmdb.get(tmdb_id) or {}
        in_completed = metadata_id in completed_metadata_ids
        radarr_present = bool(radarr_item.get("radarr_present"))
        radarr_unmonitored = bool(radarr_item) and not bool(radarr_item.get("radarr_monitored"))
        keep_owned = in_completed or radarr_present or radarr_unmonitored
        row = {
            "metadata_id": metadata_id,
            "tmdb_id": tmdb_id,
            "title": movie.get("title"),
            "year": movie.get("year"),
            "in_completed": in_completed,
            "radarr_present": radarr_present,
            "radarr_unmonitored": radarr_unmonitored,
            "radarr_movie_id": radarr_item.get("radarr_movie_id"),
            "keep_owned": keep_owned,
        }
        owned_movie_rows.append(row)
        if keep_owned:
            owned_keep_rows.append(row)
        else:
            owned_remove_rows.append(row)

    summary = {
        "settings_path": str(settings_path),
        "owned_collection": {
            "name": owned_collection.get("name"),
            "id": owned_collection.get("id"),
            "ui_count": owned_collection.get("count"),
        },
        "completed_collection": {
            "name": completed_collection.get("name"),
            "id": completed_collection.get("id"),
            "ui_count": completed_collection.get("count"),
        },
        "counts": {
            "owned_movie_members_total": len(owned_movie_rows),
            "owned_keep_total": len(owned_keep_rows),
            "owned_remove_total": len(owned_remove_rows),
            "completed_members_total": len(completed_metadata_ids),
        },
        "rule": {
            "keep_if_completed": True,
            "keep_if_radarr_present": True,
            "keep_if_radarr_unmonitored": True,
        },
    }

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output_dir / "owned_keep.full.csv", owned_keep_rows)
    _write_csv(output_dir / "owned_remove.full.csv", owned_remove_rows)
    _write_csv(output_dir / "owned_keep.sample.csv", owned_keep_rows[:25])
    _write_csv(output_dir / "owned_remove.sample.csv", owned_remove_rows[:25])

    print(f"Settings: {settings_path}")
    print(f"Output:   {output_dir}")
    print(f"Owned movie members: {len(owned_movie_rows)}")
    print(f"Owned keep: {len(owned_keep_rows)}")
    print(f"Owned remove: {len(owned_remove_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
