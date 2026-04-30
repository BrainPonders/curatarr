"""Small HTTP boundary for TMDB-style metadata lookups."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from curatarr.adapters import AdapterError


class TmdbError(AdapterError):
    """Raised when TMDB transport or API handling fails."""


Transport = Callable[[str, dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class TmdbClient:
    """Minimal TMDB client with injectable transport for tests."""

    api_key: str
    base_url: str = "https://api.themoviedb.org/3"
    transport: Transport | None = None

    def search_movie(self, query: str) -> dict[str, Any]:
        return self._get("/search/movie", {"query": query})

    def search_series(self, query: str) -> dict[str, Any]:
        return self._get("/search/tv", {"query": query})

    def movie_external_ids(self, tmdb_id: str) -> dict[str, Any]:
        return self._get(f"/movie/{tmdb_id}/external_ids", {})

    def series_external_ids(self, tmdb_id: str) -> dict[str, Any]:
        return self._get(f"/tv/{tmdb_id}/external_ids", {})

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport(path, params)

        query = urlencode({**params, "api_key": self.api_key})
        url = f"{self.base_url.rstrip('/')}{path}?{query}"
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise TmdbError(f"TMDB HTTP error {exc.code}") from exc
        except URLError as exc:
            raise TmdbError(f"TMDB connection error: {exc.reason}") from exc
        except OSError as exc:
            raise TmdbError(f"TMDB transport error: {exc}") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TmdbError("TMDB returned invalid JSON.") from exc
        if not isinstance(decoded, dict):
            raise TmdbError("TMDB returned a non-object response.")
        return decoded
