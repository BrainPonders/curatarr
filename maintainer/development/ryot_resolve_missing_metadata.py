#!/usr/bin/env python3
"""Standalone Ryot metadata resolver for dry-run CSV rows without metadata_id."""

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

METADATA_DETAILS_QUERY = """
query MetadataDetails($metadataId: String!) {
  metadataDetails(metadataId: $metadataId) {
    response {
      id
      title
      identifier
      publishYear
      lot
      source
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


class ResolverRyotClient:
    """Minimal Ryot GraphQL helper for resolving metadata ids from TMDB ids."""

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
        description="Resolve missing Ryot metadata_id values in owned_to_add CSV rows.",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        help="Path to the runtime settings.py to load. Falls back to CURATARR_SETTINGS_FILE, ./settings.py, or ./config/settings.py.",
    )
    parser.add_argument(
        "--input-csv",
        required=True,
        help="CSV to inspect, typically owned_to_add.full.csv from the dry-run helper.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where resolved, unresolved, and ambiguous CSVs will be written.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=25,
        help="Number of metadataSearch candidates to inspect per title. Default: 25.",
    )
    return parser.parse_args()


def _load_ryot_client(settings_path: Optional[str]):
    resolved_path, settings_module = load_settings(settings_path)
    timeout_seconds = int(getattr(settings_module, "requests_timeout_seconds", 15))
    if not getattr(settings_module, "ryot_enabled", False):
        raise RuntimeError("ryot_enabled must be True for this resolver.")
    ryot_url = getattr(settings_module, "ryot_url", "")
    ryot_api_key = getattr(settings_module, "ryot_api_key", "")
    if not ryot_url or not ryot_api_key:
        raise RuntimeError("Ryot URL and API key are required in settings.py.")
    client = ResolverRyotClient(
        url=ryot_url,
        api_key=ryot_api_key,
        graphql_path=getattr(settings_module, "ryot_graphql_path", "/backend/graphql"),
        verify_ssl=bool(getattr(settings_module, "ryot_verify_ssl", True)),
        timeout_seconds=timeout_seconds,
    )
    return resolved_path, client


def _load_rows(path: str) -> List[dict]:
    csv_path = Path(path).expanduser().resolve()
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _unique_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _candidate_queries(row: dict) -> List[str]:
    title = (row.get("title") or "").strip()
    queries = [title]
    year = (row.get("year") or "").strip()
    if title and year:
        queries.append(f"{title} {year}")
    return [query for query in _unique_preserve_order(queries) if query]


def _match_candidates(client: ResolverRyotClient, row: dict, page_size: int) -> List[dict]:
    raw_tmdb_id = (row.get("tmdb_id") or "").strip()
    if not raw_tmdb_id:
        return []
    try:
        target_tmdb_id = int(raw_tmdb_id)
    except ValueError:
        return []

    matches: List[dict] = []
    seen_metadata_ids = set()
    for query in _candidate_queries(row):
        for metadata_id in client.metadata_search(query=query, page_size=page_size):
            if metadata_id in seen_metadata_ids:
                continue
            seen_metadata_ids.add(metadata_id)
            details = client.metadata_details(metadata_id)
            if (details.get("lot") or "").upper() != "MOVIE":
                continue
            if (details.get("source") or "").upper() != "TMDB":
                continue
            try:
                identifier = int(str(details.get("identifier")))
            except (TypeError, ValueError):
                continue
            if identifier != target_tmdb_id:
                continue
            matches.append(
                {
                    "metadata_id": metadata_id,
                    "matched_title": details.get("title"),
                    "matched_year": details.get("publishYear"),
                    "matched_identifier": identifier,
                }
            )
    return matches


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    settings_path, client = _load_ryot_client(args.settings_file)
    rows = _load_rows(args.input_csv)
    target_rows = [row for row in rows if not (row.get("metadata_id") or "").strip()]

    resolved_rows: List[dict] = []
    unresolved_rows: List[dict] = []
    ambiguous_rows: List[dict] = []

    for row in target_rows:
        matches = _match_candidates(client, row, args.page_size)
        if len(matches) == 1:
            match = matches[0]
            resolved_row = dict(row)
            resolved_row["resolved_metadata_id"] = match["metadata_id"]
            resolved_row["resolved_title"] = match["matched_title"]
            resolved_row["resolved_year"] = match["matched_year"]
            resolved_row["resolved_identifier"] = match["matched_identifier"]
            resolved_rows.append(resolved_row)
            continue
        if len(matches) > 1:
            ambiguous_row = dict(row)
            ambiguous_row["matched_metadata_ids"] = " | ".join(match["metadata_id"] for match in matches)
            ambiguous_row["matched_titles"] = " | ".join(
                f"{match['matched_title']} ({match['matched_year']})" for match in matches
            )
            ambiguous_rows.append(ambiguous_row)
            continue
        unresolved_row = dict(row)
        unresolved_row["search_queries"] = " | ".join(_candidate_queries(row))
        unresolved_rows.append(unresolved_row)

    summary = {
        "settings_path": str(settings_path),
        "input_csv": str(Path(args.input_csv).expanduser().resolve()),
        "counts": {
            "input_rows_total": len(rows),
            "target_rows_missing_metadata_id_total": len(target_rows),
            "resolved_total": len(resolved_rows),
            "ambiguous_total": len(ambiguous_rows),
            "unresolved_total": len(unresolved_rows),
        },
    }

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output_dir / "resolved.full.csv", resolved_rows)
    _write_csv(output_dir / "ambiguous.full.csv", ambiguous_rows)
    _write_csv(output_dir / "unresolved.full.csv", unresolved_rows)
    _write_csv(output_dir / "resolved.sample.csv", resolved_rows[:25])
    _write_csv(output_dir / "ambiguous.sample.csv", ambiguous_rows[:25])
    _write_csv(output_dir / "unresolved.sample.csv", unresolved_rows[:25])

    print(f"Settings: {settings_path}")
    print(f"Input:    {Path(args.input_csv).expanduser().resolve()}")
    print(f"Output:   {output_dir}")
    print(f"Missing metadata_id rows: {len(target_rows)}")
    print(f"Resolved: {len(resolved_rows)}")
    print(f"Ambiguous: {len(ambiguous_rows)}")
    print(f"Unresolved: {len(unresolved_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
