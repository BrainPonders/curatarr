"""Small HTTP boundary for Sonarr lookups."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from curatarr.adapters import AdapterError


class SonarrError(AdapterError):
    """Raised when Sonarr transport or API handling fails."""


Transport = Callable[[str, dict[str, str]], Any]


@dataclass(frozen=True)
class SonarrClient:
    """Minimal Sonarr client with injectable transport for tests."""

    url: str
    api_key: str
    transport: Transport | None = None

    def series(self) -> list[dict[str, Any]]:
        response = self._get("/api/v3/series", {})
        if not isinstance(response, list):
            raise SonarrError("Sonarr series list response was not a list.")
        return [item for item in response if isinstance(item, dict)]

    def lookup_by_tvdb_id(self, tvdb_id: str) -> dict[str, Any] | None:
        for series in self.series():
            if str(series.get("tvdbId") or series.get("tvdb_id") or "") == str(tvdb_id):
                return series
        return None

    def lookup_by_tmdb_id(self, tmdb_id: str) -> dict[str, Any] | None:
        for series in self.series():
            if str(series.get("tmdbId") or series.get("tmdb_id") or "") == str(tmdb_id):
                return series
        return None

    def lookup_by_imdb_id(self, imdb_id: str) -> dict[str, Any] | None:
        for series in self.series():
            if str(series.get("imdbId") or series.get("imdb_id") or "") == str(imdb_id):
                return series
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
            raise SonarrError(f"Sonarr HTTP error {exc.code}") from exc
        except URLError as exc:
            raise SonarrError(f"Sonarr connection error: {exc.reason}") from exc
        except OSError as exc:
            raise SonarrError(f"Sonarr transport error: {exc}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SonarrError("Sonarr returned invalid JSON.") from exc
