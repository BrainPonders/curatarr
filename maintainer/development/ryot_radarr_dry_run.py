#!/usr/bin/env python3
"""Standalone dry-run comparison between Ryot and Radarr movie sets.

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
    cacheId
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
      hasInteracted
      seenByUserCount
      history {
        state
        finishedOn
        lastUpdatedOn
      }
      collections {
        details {
          collectionName
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
        return list(self._get("movie") or [])

    def _get(self, endpoint: str, *, params: Optional[dict] = None):
        return self.http.get(
            f"{self.base_url}/api/v3/{endpoint}",
            headers={"X-Api-Key": self.api_key},
            params=params,
        )


class DryRunRyotClient:
    """Minimal Ryot GraphQL helper for bulk movie inspection."""

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

    def list_movies(self, *, page_size: int) -> List[dict]:
        metadata_ids = self._list_movie_metadata_ids(page_size=page_size)
        movies: List[dict] = []
        for metadata_id in metadata_ids:
            details = self._metadata_details(metadata_id)
            if (details.get("lot") or "").upper() != "MOVIE":
                continue
            identifier = details.get("identifier")
            try:
                tmdb_id = int(str(identifier))
            except (TypeError, ValueError):
                continue
            user_details = self._user_metadata_details(metadata_id)
            collections = user_details.get("collections") or []
            collection_names = [
                (((item or {}).get("details") or {}).get("collectionName"))
                for item in collections
            ]
            collection_names = [name for name in collection_names if name]
            history = user_details.get("history") or []
            seen_by_user_count = int(user_details.get("seenByUserCount") or 0)
            watched = any(item.get("state") == "COMPLETED" for item in history) or seen_by_user_count > 0
            owned = any(name.casefold() == "owned" for name in collection_names)
            movies.append(
                {
                    "metadata_id": metadata_id,
                    "tmdb_id": tmdb_id,
                    "title": details.get("title"),
                    "year": details.get("publishYear"),
                    "watched": watched,
                    "owned": owned,
                    "collection_names": collection_names,
                    "seen_by_user_count": seen_by_user_count,
                    "has_interacted": bool(user_details.get("hasInteracted")),
                }
            )
        return movies

    def _list_movie_metadata_ids(self, *, page_size: int) -> List[str]:
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
        return metadata_ids

    def _metadata_details(self, metadata_id: str) -> dict:
        payload = self._graphql(METADATA_DETAILS_QUERY, {"metadataId": metadata_id})
        return ((payload.get("metadataDetails") or {}).get("response")) or {}

    def _user_metadata_details(self, metadata_id: str) -> dict:
        payload = self._graphql(USER_METADATA_DETAILS_QUERY, {"metadataId": metadata_id})
        return ((payload.get("userMetadataDetails") or {}).get("response")) or {}

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
        description="Compare live Ryot and Radarr movie inventories without making changes.",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        help="Path to the runtime settings.py to load. Falls back to CURATARR_SETTINGS_FILE, ./settings.py, or ./config/settings.py.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where summary.json and review CSVs will be written.",
    )
    parser.add_argument(
        "--ryot-search-query",
        default="",
        help="Deprecated compatibility flag. Ryot movies are enumerated from userMetadataList and this value is ignored.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Ryot userMetadataList page size. Default: 100.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=25,
        help="Number of rows to include in sample CSVs. Default: 25.",
    )
    parser.add_argument(
        "--manual-watch-csv",
        default=None,
        help="Optional reviewed CSV with manual watched overrides. Expected columns: tmdb_id and watched manual override.",
    )
    return parser.parse_args()


def _load_clients(settings_path: Optional[str]):
    resolved_path, settings_module = load_settings(settings_path)
    timeout_seconds = int(getattr(settings_module, "requests_timeout_seconds", 15))

    if not getattr(settings_module, "radarr_enabled", False):
        raise RuntimeError("radarr_enabled must be True for this dry run.")
    if not getattr(settings_module, "ryot_enabled", False):
        raise RuntimeError("ryot_enabled must be True for this dry run.")

    radarr_url = getattr(settings_module, "radarr_url", "")
    radarr_api_key = getattr(settings_module, "radarr_api_key", "")
    ryot_url = getattr(settings_module, "ryot_url", "")
    ryot_api_key = getattr(settings_module, "ryot_api_key", "")
    if not radarr_url or not radarr_api_key:
        raise RuntimeError("Radarr URL and API key are required in settings.py.")
    if not ryot_url or not ryot_api_key:
        raise RuntimeError("Ryot URL and API key are required in settings.py.")

    radarr_client = RadarrClient(
        radarr_url,
        radarr_api_key,
        timeout_seconds=timeout_seconds,
        verify_ssl=bool(getattr(settings_module, "radarr_verify_ssl", True)),
    )
    ryot_client = DryRunRyotClient(
        url=ryot_url,
        api_key=ryot_api_key,
        graphql_path=getattr(settings_module, "ryot_graphql_path", "/backend/graphql"),
        verify_ssl=bool(getattr(settings_module, "ryot_verify_ssl", True)),
        timeout_seconds=timeout_seconds,
    )
    return resolved_path, radarr_client, ryot_client


def _radarr_movies(radarr_client: RadarrClient) -> List[dict]:
    movies = []
    for item in radarr_client.list_movies():
        try:
            tmdb_id = int(item.get("tmdbId"))
        except (TypeError, ValueError):
            continue
        movies.append(
            {
                "movie_id": item.get("id"),
                "tmdb_id": tmdb_id,
                "title": item.get("title"),
                "year": item.get("year"),
                "monitored": bool(item.get("monitored")),
                "has_file": bool(item.get("hasFile") or item.get("movieFile")),
                "tags": list(item.get("tags") or []),
                "minimum_availability": item.get("minimumAvailability"),
            }
        )
    return movies


def _rows(ryot_by_tmdb: Dict[int, dict], radarr_by_tmdb: Dict[int, dict], tmdb_ids: Iterable[int]) -> List[dict]:
    rows = []
    for tmdb_id in sorted(tmdb_ids):
        ryot_item = ryot_by_tmdb.get(tmdb_id)
        radarr_item = radarr_by_tmdb.get(tmdb_id)
        rows.append(
            {
                "metadata_id": (ryot_item or {}).get("metadata_id"),
                "tmdb_id": tmdb_id,
                "title": (ryot_item or {}).get("title") or (radarr_item or {}).get("title"),
                "year": (ryot_item or {}).get("year") or (radarr_item or {}).get("year"),
                "ryot_watched": bool((ryot_item or {}).get("watched")),
                "ryot_owned": bool((ryot_item or {}).get("owned")),
                "ryot_collections": " | ".join((ryot_item or {}).get("collection_names") or []),
                "radarr_present": bool((radarr_item or {}).get("has_file")),
                "radarr_monitored": bool((radarr_item or {}).get("monitored")),
                "radarr_movie_id": (radarr_item or {}).get("movie_id"),
            }
        )
    return rows


def _write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _load_manual_watch_overrides(path: Optional[str]) -> Dict[int, bool]:
    if not path:
        return {}
    csv_path = Path(path).expanduser().resolve()
    truthy = {"true", "t", "yes", "y", "1", "x"}
    overrides: Dict[int, bool] = {}
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            raw_tmdb_id = (row.get("tmdb_id") or "").strip()
            if not raw_tmdb_id:
                continue
            try:
                tmdb_id = int(raw_tmdb_id)
            except ValueError:
                continue
            raw_override = (row.get("watched manual override") or "").strip().casefold()
            if raw_override in truthy:
                overrides[tmdb_id] = True
    return overrides


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    settings_path, radarr_client, ryot_client = _load_clients(args.settings_file)
    manual_watch_overrides = _load_manual_watch_overrides(args.manual_watch_csv)
    ryot_movies = ryot_client.list_movies(page_size=args.page_size)
    radarr_movies = _radarr_movies(radarr_client)

    ryot_by_tmdb = {item["tmdb_id"]: item for item in ryot_movies}
    radarr_by_tmdb = {item["tmdb_id"]: item for item in radarr_movies}

    ryot_ids = set(ryot_by_tmdb)
    radarr_ids = set(radarr_by_tmdb)
    watched_ids = {tmdb_id for tmdb_id, item in ryot_by_tmdb.items() if item["watched"]}
    effective_watched_ids = watched_ids | set(manual_watch_overrides)
    current_owned_ids = {tmdb_id for tmdb_id, item in ryot_by_tmdb.items() if item["owned"]}
    radarr_present_ids = {tmdb_id for tmdb_id, item in radarr_by_tmdb.items() if item["has_file"]}
    radarr_missing_ids = {tmdb_id for tmdb_id, item in radarr_by_tmdb.items() if not item["has_file"]}

    only_ryot_ids = ryot_ids - radarr_ids
    only_radarr_ids = radarr_ids - ryot_ids
    both_ids = ryot_ids & radarr_ids

    future_owned_ids = effective_watched_ids | radarr_present_ids
    owned_to_add_ids = future_owned_ids - current_owned_ids
    owned_to_remove_ids = current_owned_ids - future_owned_ids
    owned_unchanged_ids = current_owned_ids & future_owned_ids
    reminders_candidate_ids = owned_to_remove_ids - set(manual_watch_overrides)

    only_ryot_rows = _rows(ryot_by_tmdb, radarr_by_tmdb, only_ryot_ids)
    only_radarr_rows = _rows(ryot_by_tmdb, radarr_by_tmdb, only_radarr_ids)
    owned_to_add_rows = _rows(ryot_by_tmdb, radarr_by_tmdb, owned_to_add_ids)
    owned_to_remove_rows = _rows(ryot_by_tmdb, radarr_by_tmdb, owned_to_remove_ids)
    reminders_candidate_rows = _rows(ryot_by_tmdb, radarr_by_tmdb, reminders_candidate_ids)

    for row in only_ryot_rows + only_radarr_rows + owned_to_add_rows + owned_to_remove_rows + reminders_candidate_rows:
        tmdb_id = row["tmdb_id"]
        row["manual_watched"] = bool(manual_watch_overrides.get(tmdb_id))
        row["effective_watched"] = bool(row["ryot_watched"] or row["manual_watched"])

    summary = {
        "settings_path": str(settings_path),
        "ryot_search_query": args.ryot_search_query,
        "manual_watch_csv": str(Path(args.manual_watch_csv).expanduser().resolve()) if args.manual_watch_csv else None,
        "counts": {
            "ryot_movies_total": len(ryot_ids),
            "radarr_movies_total": len(radarr_ids),
            "ryot_watched_total": len(watched_ids),
            "manual_watched_override_total": len(manual_watch_overrides),
            "effective_watched_total": len(effective_watched_ids),
            "ryot_owned_current_total": len(current_owned_ids),
            "radarr_present_total": len(radarr_present_ids),
            "radarr_missing_total": len(radarr_missing_ids),
            "intersection_total": len(both_ids),
            "ryot_minus_radarr_total": len(only_ryot_ids),
            "radarr_minus_ryot_total": len(only_radarr_ids),
        },
        "owned_rebuild_preview": {
            "future_owned_total": len(future_owned_ids),
            "current_owned_total": len(current_owned_ids),
            "owned_to_add_total": len(owned_to_add_ids),
            "owned_to_remove_total": len(owned_to_remove_ids),
            "owned_unchanged_total": len(owned_unchanged_ids),
            "reminders_candidate_total": len(reminders_candidate_ids),
        },
        "breakdowns": {
            "ryot_minus_radarr": {
                "watched": sum(1 for item in only_ryot_rows if item["ryot_watched"]),
                "not_watched": sum(1 for item in only_ryot_rows if not item["ryot_watched"]),
                "manual_watched": sum(1 for item in only_ryot_rows if item["manual_watched"]),
                "effective_watched": sum(1 for item in only_ryot_rows if item["effective_watched"]),
                "currently_owned": sum(1 for item in only_ryot_rows if item["ryot_owned"]),
                "neither_watched_nor_owned": sum(
                    1 for item in only_ryot_rows if not item["effective_watched"] and not item["ryot_owned"]
                ),
            },
            "currently_owned_but_not_future_owned": {
                "not_watched": sum(1 for item in owned_to_remove_rows if not item["ryot_watched"]),
                "not_effective_watched": sum(1 for item in owned_to_remove_rows if not item["effective_watched"]),
                "not_present_in_radarr": sum(1 for item in owned_to_remove_rows if not item["radarr_present"]),
            },
            "radarr_minus_ryot": {
                "present": sum(1 for item in only_radarr_rows if item["radarr_present"]),
                "missing": sum(1 for item in only_radarr_rows if not item["radarr_present"]),
                "monitored": sum(1 for item in only_radarr_rows if item["radarr_monitored"]),
                "unmonitored": sum(1 for item in only_radarr_rows if not item["radarr_monitored"]),
            },
            "reminders_candidates": {
                "effective_watched": sum(1 for item in reminders_candidate_rows if item["effective_watched"]),
                "not_effective_watched": sum(1 for item in reminders_candidate_rows if not item["effective_watched"]),
            },
        },
    }

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output_dir / "ryot_minus_radarr.sample.csv", only_ryot_rows[: args.sample_size])
    _write_csv(output_dir / "radarr_minus_ryot.sample.csv", only_radarr_rows[: args.sample_size])
    _write_csv(output_dir / "owned_to_add.sample.csv", owned_to_add_rows[: args.sample_size])
    _write_csv(output_dir / "owned_to_remove.sample.csv", owned_to_remove_rows[: args.sample_size])
    _write_csv(output_dir / "reminders_candidates.sample.csv", reminders_candidate_rows[: args.sample_size])
    _write_csv(output_dir / "ryot_minus_radarr.full.csv", only_ryot_rows)
    _write_csv(output_dir / "radarr_minus_ryot.full.csv", only_radarr_rows)
    _write_csv(output_dir / "owned_to_add.full.csv", owned_to_add_rows)
    _write_csv(output_dir / "owned_to_remove.full.csv", owned_to_remove_rows)
    _write_csv(output_dir / "reminders_candidates.full.csv", reminders_candidate_rows)

    print(f"Settings: {settings_path}")
    print(f"Output:   {output_dir}")
    print(f"Ryot movies: {len(ryot_ids)}")
    print(f"Radarr movies: {len(radarr_ids)}")
    print(f"Ryot - Radarr: {len(only_ryot_ids)}")
    print(f"Radarr - Ryot: {len(only_radarr_ids)}")
    print(f"Manual watched overrides: {len(manual_watch_overrides)}")
    print(f"Future Owned total: {len(future_owned_ids)}")
    print(f"Owned to add: {len(owned_to_add_ids)}")
    print(f"Owned to remove: {len(owned_to_remove_ids)}")
    print(f"Reminders candidates: {len(reminders_candidate_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
