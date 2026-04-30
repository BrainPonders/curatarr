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
        if not isinstance(response, list):
            raise RadarrError("Radarr movie list response was not a list.")
        return [item for item in response if isinstance(item, dict)]

    def lookup_by_tmdb_id(self, tmdb_id: str) -> dict[str, Any] | None:
        for movie in self.movies():
            if str(movie.get("tmdbId") or movie.get("tmdb_id") or "") == str(tmdb_id):
                return movie
        return None

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
