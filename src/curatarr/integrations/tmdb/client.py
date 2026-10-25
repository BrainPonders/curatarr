"""Small HTTP boundary for TMDB-style metadata lookups."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
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

    api_key: str = field(repr=False)
    base_url: str = "https://api.themoviedb.org/3"
    transport: Transport | None = None

    def search_movie(self, query: str, *, year: str | None = None) -> dict[str, Any]:
        params = {"query": query}
        if year is not None:
            params["primary_release_year"] = year
        return self._get("/search/movie", params)

    def search_series(self, query: str) -> dict[str, Any]:
        return self._get("/search/tv", {"query": query})

    def discover_movies(self, params: dict[str, str]) -> dict[str, Any]:
        return self._get("/discover/movie", params)

    def movie_genres(self) -> dict[str, Any]:
        return self._get("/genre/movie/list", {})

    def find_imdb(self, imdb_id: str) -> dict[str, Any]:
        import re
        if not isinstance(imdb_id, str) or not re.fullmatch(r'tt[0-9]{5,12}', imdb_id):
            raise TmdbError('Invalid IMDb identifier.')
        return self._get(f'/find/{imdb_id}', {'external_source': 'imdb_id'})

    def movie_external_ids(self, tmdb_id: str) -> dict[str, Any]:
        return self._get(f"/movie/{tmdb_id}/external_ids", {})

    def movie_details(self, tmdb_id: str) -> dict[str, Any]:
        if (not isinstance(tmdb_id, str) or len(tmdb_id) > 10 or not tmdb_id.isascii()
                or not tmdb_id.isdigit() or not 0 < int(tmdb_id) <= 2147483647):
            raise TmdbError("Invalid TMDB movie identifier.")
        return self._get(f"/movie/{tmdb_id}", {})

    def series_external_ids(self, tmdb_id: str) -> dict[str, Any]:
        return self._get(f"/tv/{tmdb_id}/external_ids", {})

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport(path, params)

        credential = self.api_key.strip()
        bearer = credential.lower().startswith("bearer ")
        if bearer:
            credential = credential[7:].strip()
        bearer = bearer or (credential.startswith("eyJ") and credential.count(".") == 2)
        headers = {"Accept": "application/json"}
        query_params = dict(params)
        if bearer:
            headers["Authorization"] = f"Bearer {credential}"
        else:
            query_params["api_key"] = credential
        query = urlencode(query_params)
        url = f"{self.base_url.rstrip('/')}{path}" + (f"?{query}" if query else "")
        request = Request(url, headers=headers, method="GET")
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
