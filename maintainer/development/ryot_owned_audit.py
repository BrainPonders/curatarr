#!/usr/bin/env python3
"""Standalone Ryot audit helper for Owned collection vs movie-view inventory."""

from __future__ import annotations

import argparse
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


class RyotAuditClient:
    """Minimal Ryot GraphQL helper for Owned vs movie-view audits."""

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
        description="Audit Ryot Owned collection against the movie-view inventory.",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        help="Path to the runtime settings.py to load. Falls back to CURATARR_SETTINGS_FILE, ./settings.py, or ./config/settings.py.",
    )
    parser.add_argument(
        "--owned-collection",
        default="Owned",
        help="Ryot collection name to audit. Default: Owned.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where summary.json and audit CSVs will be written.",
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


def _load_ryot_client(settings_path: Optional[str]):
    resolved_path, settings_module = load_settings(settings_path)
    timeout_seconds = int(getattr(settings_module, "requests_timeout_seconds", 15))
    if not getattr(settings_module, "ryot_enabled", False):
        raise RuntimeError("ryot_enabled must be True for this helper.")
    client = RyotAuditClient(
        url=getattr(settings_module, "ryot_url", ""),
        api_key=getattr(settings_module, "ryot_api_key", ""),
        graphql_path=getattr(settings_module, "ryot_graphql_path", "/backend/graphql"),
        verify_ssl=bool(getattr(settings_module, "ryot_verify_ssl", True)),
        timeout_seconds=timeout_seconds,
    )
    return resolved_path, client


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
        writer = json if False else None
        import csv
        dict_writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        dict_writer.writeheader()
        dict_writer.writerows(rows)


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    settings_path, ryot_client = _load_ryot_client(args.settings_file)
    collections = ryot_client.list_collections()
    owned_collection = _resolve_collection(collections, args.owned_collection)
    user_movies = ryot_client.list_user_movie_metadata(page_size=args.page_size)
    user_movies_by_metadata_id = {item["metadata_id"]: item for item in user_movies}
    user_movies_by_tmdb_id = {item["tmdb_id"]: item for item in user_movies}
    owned_metadata_ids = ryot_client.collection_contents_metadata_ids(
        collection_id=str(owned_collection.get("id")),
        page_size=args.page_size,
    )

    owned_rows = []
    owned_missing_from_movie_view = []
    duplicate_tmdb_rows = []
    seen_tmdb_counts: Dict[int, List[str]] = {}

    for metadata_id in owned_metadata_ids:
        details = ryot_client.metadata_details(metadata_id)
        try:
            tmdb_id = int(str(details.get("identifier")))
        except (TypeError, ValueError):
            tmdb_id = None
        row = {
            "metadata_id": metadata_id,
            "tmdb_id": tmdb_id,
            "title": details.get("title"),
            "year": details.get("publishYear"),
            "lot": details.get("lot"),
            "source": details.get("source"),
            "in_movie_view": metadata_id in user_movies_by_metadata_id,
        }
        owned_rows.append(row)
        if not row["in_movie_view"]:
            owned_missing_from_movie_view.append(row)
        if tmdb_id is not None:
            seen_tmdb_counts.setdefault(tmdb_id, []).append(metadata_id)

    for tmdb_id, metadata_ids in seen_tmdb_counts.items():
        if len(metadata_ids) <= 1:
            continue
        movie_view_row = user_movies_by_tmdb_id.get(tmdb_id) or {}
        duplicate_tmdb_rows.append(
            {
                "tmdb_id": tmdb_id,
                "metadata_ids": " | ".join(metadata_ids),
                "duplicate_count": len(metadata_ids),
                "movie_view_metadata_id": movie_view_row.get("metadata_id"),
                "movie_view_title": movie_view_row.get("title"),
                "movie_view_year": movie_view_row.get("year"),
            }
        )

    summary = {
        "settings_path": str(settings_path),
        "owned_collection": {
            "name": owned_collection.get("name"),
            "id": owned_collection.get("id"),
            "ui_count": owned_collection.get("count"),
        },
        "movie_view": {
            "movie_count": len(user_movies),
            "distinct_tmdb_count": len({item["tmdb_id"] for item in user_movies}),
        },
        "owned_audit": {
            "owned_metadata_count": len(owned_metadata_ids),
            "owned_distinct_tmdb_count": len({row["tmdb_id"] for row in owned_rows if row["tmdb_id"] is not None}),
            "owned_missing_from_movie_view_count": len(owned_missing_from_movie_view),
            "duplicate_tmdb_rows_count": len(duplicate_tmdb_rows),
        },
    }

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output_dir / "owned_missing_from_movie_view.full.csv", owned_missing_from_movie_view)
    _write_csv(output_dir / "owned_missing_from_movie_view.sample.csv", owned_missing_from_movie_view[:25])
    _write_csv(output_dir / "owned_duplicate_tmdb.full.csv", duplicate_tmdb_rows)
    _write_csv(output_dir / "owned_duplicate_tmdb.sample.csv", duplicate_tmdb_rows[:25])

    print(f"Settings: {settings_path}")
    print(f"Output:   {output_dir}")
    print(f"Owned collection UI count: {owned_collection.get('count')}")
    print(f"Movie view count: {len(user_movies)}")
    print(f"Owned metadata ids: {len(owned_metadata_ids)}")
    print(f"Owned missing from movie view: {len(owned_missing_from_movie_view)}")
    print(f"Owned duplicate tmdb rows: {len(duplicate_tmdb_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
