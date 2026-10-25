"""Small HTTP boundary for Radarr lookups."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from curatarr.adapters import AdapterError


class RadarrError(AdapterError):
    """Raised when Radarr transport or API handling fails."""


Transport = Callable[[str, dict[str, str]], Any]


@dataclass(frozen=True)
class RadarrClient:
    """Minimal Radarr client with injectable transport for tests."""

    url: str
    api_key: str
    transport: Transport | None = None

    def movies(self) -> list[dict[str, Any]]:
        response = self._get("/api/v3/movie", {})
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise RadarrError("Radarr movie list response was not a list of records.")
        return response

    def quality_profile(self, profile_id: int) -> dict[str, Any]:
        if type(profile_id) is not int or profile_id <= 0:
            raise RadarrError("Invalid quality profile identifier.")
        response = self._get(f"/api/v3/qualityprofile/{profile_id}", {})
        if not isinstance(response, dict) or response.get("id") != profile_id:
            raise RadarrError("Radarr quality profile response did not match the request.")
        return response

    def maintenance_health(self):
        """Read native warnings and the complete download/import queue, without secrets."""
        health = self._get("/api/v3/health", {})
        if not isinstance(health, list) or any(not isinstance(h, dict) or
                h.get("type") not in {"ok", "notice", "warning", "error"} for h in health):
            raise RadarrError("Radarr health response is incomplete.")
        result = [{"kind": "radarr_health_warning", "key": str(n), "severity": h["type"]}
                  for n, h in enumerate(health) if h["type"] in {"warning", "error"}]
        records, seen, total, page = [], set(), None, 1
        while total is None or len(records) < total:
            payload = self._get("/api/v3/queue", {"page": str(page), "pageSize": "500",
                                                "includeUnknownMovieItems": "true"})
            if (not isinstance(payload, dict) or type(payload.get("totalRecords")) is not int
                    or not 0 <= payload["totalRecords"] <= 50000 or not isinstance(payload.get("records"), list)
                    or len(payload["records"]) > 500):
                raise RadarrError("Radarr queue snapshot is incomplete.")
            if total is not None and total != payload["totalRecords"]:
                raise RadarrError("Radarr queue changed during pagination.")
            total = payload["totalRecords"]
            if not payload["records"] and len(records) < total:
                raise RadarrError("Radarr queue pagination is incomplete.")
            for item in payload["records"]:
                if (not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in seen
                        or item.get("trackedDownloadStatus") not in {"ok", "warning", "error"}):
                    raise RadarrError("Radarr queue status is incomplete or duplicated.")
                seen.add(item["id"])
                records.append(item)
                if (item["trackedDownloadStatus"].casefold() in {"warning", "error"}
                        or item.get("status") in {"failed", "warning"} or item.get("errorMessage")):
                    result.append({"kind": "radarr_queue_problem", "key": str(item["id"]),
                                   "movie_id": item.get("movieId") if type(item.get("movieId")) is int else None, "severity": "warning"})
            if len(records) > total:
                raise RadarrError("Radarr queue pagination is invalid.")
            page += 1
        return result

    def lookup_by_tmdb_id(self, tmdb_id: str) -> dict[str, Any] | None:
        for movie in self.movies():
            if str(movie.get("tmdbId") or movie.get("tmdb_id") or "") == str(tmdb_id):
                return movie
        return None

    def movie_file(self, file_id: int, movie_id: int) -> dict[str, Any]:
        if type(file_id) is not int or file_id <= 0 or type(movie_id) is not int or movie_id <= 0:
            raise RadarrError("Invalid movie-file identifiers.")
        response = self._get(f"/api/v3/moviefile/{file_id}", {})
        if (not isinstance(response, dict) or type(response.get("id")) is not int
                or response["id"] != file_id or type(response.get("movieId")) is not int
                or response["movieId"] != movie_id):
            raise RadarrError("Radarr file response did not match the requested movie and file.")
        return response

    def custom_formats(self) -> list[dict[str, Any]]:
        response = self._get("/api/v3/customformat", {})
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise RadarrError("Radarr custom format response was not a list of definitions.")
        return response

    def lookup_by_imdb_id(self, imdb_id: str) -> dict[str, Any] | None:
        for movie in self.movies():
            if str(movie.get("imdbId") or movie.get("imdb_id") or "") == str(imdb_id):
                return movie
        return None

    def _get(self, path: str, params: dict[str, str]) -> Any:
        if self.transport is not None:
            return self.transport(path, params)

        query = urlencode(params)
        url = f"{self.url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url,
            headers={
                "X-Api-Key": self.api_key,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RadarrError(f"Radarr HTTP error {exc.code}") from exc
        except URLError as exc:
            raise RadarrError(f"Radarr connection error: {exc.reason}") from exc
        except OSError as exc:
            raise RadarrError(f"Radarr transport error: {exc}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RadarrError("Radarr returned invalid JSON.") from exc
