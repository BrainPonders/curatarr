#!/usr/bin/env python3
"""Standalone additive Ryot collection updater.

This helper is intentionally self-contained so it can be copied to a runtime
server and executed next to a legacy Python settings.py file without a full
repository checkout.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional

import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning


COLLECTIONS_QUERY = """
query UserCollectionsList {
  userCollectionsList {
    response {
      name
      creator {
        id
        name
      }
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

MUTATION_ROOT_QUERY = """
query MutationRootShape {
  __schema {
    mutationType {
      name
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

ENUM_VALUES_QUERY = """
query EnumValues($name: String!) {
  __type(name: $name) {
    enumValues {
      name
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


class StandaloneRyotClient:
    """Minimal Ryot GraphQL helper for collection updates."""

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
        payload = self._graphql(COLLECTIONS_QUERY, {})
        return list(((payload.get("userCollectionsList") or {}).get("response")) or [])

    def get_add_entities_field_type(self) -> dict:
        mutation_root_name = ((self._graphql(MUTATION_ROOT_QUERY, {}).get("__schema") or {}).get("mutationType") or {}).get("name")
        if not mutation_root_name:
            raise RuntimeError("Could not resolve Ryot mutation root type.")
        fields_payload = self._graphql(TYPE_FIELDS_QUERY, {"name": mutation_root_name})
        fields = ((fields_payload.get("__type") or {}).get("fields")) or []
        for field in fields:
            if field.get("name") == "deployAddEntitiesToCollectionJob":
                return field.get("type") or {}
        raise RuntimeError("Could not find deployAddEntitiesToCollectionJob in Ryot schema.")

    def get_change_collection_entities_field_type(self) -> dict:
        payload = self._graphql(INPUT_FIELDS_QUERY, {"name": "ChangeCollectionToEntitiesInput"})
        fields = ((payload.get("__type") or {}).get("inputFields")) or []
        for field in fields:
            if field.get("name") == "entities":
                return field.get("type") or {}
        raise RuntimeError("Could not find entities field on ChangeCollectionToEntitiesInput.")

    def get_input_object_fields(self, type_name: str) -> List[dict]:
        payload = self._graphql(INPUT_FIELDS_QUERY, {"name": type_name})
        fields = ((payload.get("__type") or {}).get("inputFields")) or []
        if not fields:
            raise RuntimeError(f"Could not inspect input fields for {type_name}.")
        return list(fields)

    def get_enum_values(self, enum_name: str) -> List[str]:
        payload = self._graphql(ENUM_VALUES_QUERY, {"name": enum_name})
        values = ((payload.get("__type") or {}).get("enumValues")) or []
        return [value.get("name") for value in values if value.get("name")]

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
        description="Additive Ryot collection updater for dry-run CSV outputs.",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        help="Path to the runtime settings.py to load. Falls back to CURATARR_SETTINGS_FILE, ./settings.py, or ./config/settings.py.",
    )
    parser.add_argument(
        "--owned-csv",
        default=None,
        help="CSV from owned_to_add.full.csv. Rows with metadata_id will be added to the Owned collection.",
    )
    parser.add_argument(
        "--reminders-csv",
        default=None,
        help="CSV from reminders_candidates.full.csv. Rows with metadata_id will be added to the Reminders collection.",
    )
    parser.add_argument(
        "--collection-input",
        action="append",
        default=[],
        help="Generic collection input in the form CollectionName=/path/to/file.csv. May be passed multiple times.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum number of entities per GraphQL mutation. Default: 100.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the additive collection updates. Without this flag the script only prints the plan.",
    )
    return parser.parse_args()


def _load_ryot_client(settings_path: Optional[str]):
    resolved_path, settings_module = load_settings(settings_path)
    timeout_seconds = int(getattr(settings_module, "requests_timeout_seconds", 15))
    if not getattr(settings_module, "ryot_enabled", False):
        raise RuntimeError("ryot_enabled must be True for this updater.")
    ryot_url = getattr(settings_module, "ryot_url", "")
    ryot_api_key = getattr(settings_module, "ryot_api_key", "")
    if not ryot_url or not ryot_api_key:
        raise RuntimeError("Ryot URL and API key are required in settings.py.")
    client = StandaloneRyotClient(
        url=ryot_url,
        api_key=ryot_api_key,
        graphql_path=getattr(settings_module, "ryot_graphql_path", "/backend/graphql"),
        verify_ssl=bool(getattr(settings_module, "ryot_verify_ssl", True)),
        timeout_seconds=timeout_seconds,
    )
    return resolved_path, client


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


def _load_csv_rows(path: Optional[str]) -> List[dict]:
    if not path:
        return []
    csv_path = Path(path).expanduser().resolve()
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _dedupe_entities(rows: List[dict]) -> tuple[List[dict], List[dict]]:
    seen = set()
    usable: List[dict] = []
    skipped: List[dict] = []
    for row in rows:
        metadata_id = (row.get("metadata_id") or row.get("resolved_metadata_id") or "").strip()
        if not metadata_id:
            skipped.append(row)
            continue
        row = dict(row)
        row["effective_metadata_id"] = metadata_id
        if metadata_id in seen:
            continue
        seen.add(metadata_id)
        usable.append(row)
    return usable, skipped


def _build_entity_payload_builder(client: StandaloneRyotClient):
    entities_type = _unwrap_type(client.get_change_collection_entities_field_type())
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
        preferred_order = ["METADATA", "MOVIE", "MEDIA", "SHOW"]
        for candidate in preferred_order:
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
            scalar_name = field_type.get("name")
            if field_name in {"id", "entityId", "metadataId"}:
                payload[field_name] = metadata_id
                continue
            if field_name in {"lot", "entityLot", "mediaLot"}:
                payload[field_name] = lot_value_for(field_name, field_type)
                continue
            if required:
                raise RuntimeError(
                    f"Unsupported required entities field '{field_name}' of type '{scalar_name}' on {name}."
                )
        return payload

    return builder


def _mutation_returns_object(client: StandaloneRyotClient) -> bool:
    return _unwrap_type(client.get_add_entities_field_type()).get("kind") == "OBJECT"


def _resolve_collection_owner_id(collections: List[dict], target_collection_name: str) -> str:
    for collection in collections:
        if (collection.get("name") or "").casefold() == target_collection_name.casefold():
            creator = collection.get("creator") or {}
            creator_id = creator.get("id")
            if creator_id:
                return str(creator_id)
            raise RuntimeError(f"Collection '{target_collection_name}' has no creator id.")
    raise RuntimeError(f"Collection '{target_collection_name}' was not found in Ryot.")


def _plan_for_collection(rows: List[dict], collection_name: str) -> dict:
    usable_rows, skipped_rows = _dedupe_entities(rows)
    return {
        "collection_name": collection_name,
        "usable_rows": usable_rows,
        "skipped_rows": skipped_rows,
        "usable_count": len(usable_rows),
        "skipped_count": len(skipped_rows),
    }


def _parse_collection_inputs(values: List[str]) -> List[tuple[str, str]]:
    parsed: List[tuple[str, str]] = []
    for value in values:
        name, separator, path = value.partition("=")
        collection_name = name.strip()
        csv_path = path.strip()
        if separator != "=" or not collection_name or not csv_path:
            raise RuntimeError(
                f"Invalid --collection-input value '{value}'. Expected format CollectionName=/path/to/file.csv."
            )
        parsed.append((collection_name, csv_path))
    return parsed


def main() -> int:
    args = _parse_args()
    if not args.owned_csv and not args.reminders_csv and not args.collection_input:
        raise RuntimeError("Provide at least one of --owned-csv, --reminders-csv, or --collection-input.")

    settings_path, ryot_client = _load_ryot_client(args.settings_file)
    collections = ryot_client.list_collections()
    entity_payload_builder = _build_entity_payload_builder(ryot_client)
    mutation_returns_object = _mutation_returns_object(ryot_client)

    plans = []
    if args.owned_csv:
        plans.append(_plan_for_collection(_load_csv_rows(args.owned_csv), "Owned"))
    if args.reminders_csv:
        plans.append(_plan_for_collection(_load_csv_rows(args.reminders_csv), "Reminders"))
    for collection_name, csv_path in _parse_collection_inputs(args.collection_input):
        plans.append(_plan_for_collection(_load_csv_rows(csv_path), collection_name))

    summary = {
        "settings_path": str(settings_path),
        "execute": bool(args.execute),
        "batch_size": args.batch_size,
        "collections": [],
    }

    for plan in plans:
        creator_user_id = _resolve_collection_owner_id(collections, plan["collection_name"])
        entities = [entity_payload_builder((row.get("effective_metadata_id") or "").strip()) for row in plan["usable_rows"]]
        collection_summary = {
            "collection_name": plan["collection_name"],
            "creator_user_id": creator_user_id,
            "usable_count": plan["usable_count"],
            "skipped_missing_metadata_id_count": plan["skipped_count"],
            "batch_count": len(list(_chunked(entities, args.batch_size))),
        }
        if plan["skipped_rows"]:
            collection_summary["skipped_titles_sample"] = [
                {
                    "tmdb_id": row.get("tmdb_id"),
                    "title": row.get("title"),
                    "year": row.get("year"),
                }
                for row in plan["skipped_rows"][:10]
            ]

        if args.execute and entities:
            for batch in _chunked(entities, args.batch_size):
                ryot_client.add_entities_to_collection(
                    creator_user_id=creator_user_id,
                    collection_name=plan["collection_name"],
                    entities=batch,
                    returns_object=mutation_returns_object,
                )
            collection_summary["executed"] = True
        else:
            collection_summary["executed"] = False

        summary["collections"].append(collection_summary)

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
