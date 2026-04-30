"""Metadata adapter backed by TMDB-style payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from curatarr.adapters import AdapterResult
from curatarr.domain import MediaIdentity, MediaItem, MediaType
from curatarr.integrations.tmdb.client import TmdbClient, TmdbError


@dataclass(frozen=True)
class TmdbMetadataAdapter:
    """Search and cross-resolution adapter for TMDB-style metadata."""

    client: TmdbClient

    def search(self, query: str, media_type: MediaType) -> AdapterResult[tuple[MediaItem, ...]]:
        response = (
            self.client.search_movie(query)
            if media_type is MediaType.MOVIE
            else self.client.search_series(query)
        )
        results = response.get("results")
        if not isinstance(results, list):
            raise TmdbError("TMDB search response did not contain a results list.")
        items = tuple(_item_from_search_result(item, media_type) for item in results if isinstance(item, dict))
        return AdapterResult(items, diagnostics={"source": "tmdb", "count": str(len(items))})

    def resolve_identities(self, identity: MediaIdentity) -> AdapterResult[frozenset[MediaIdentity]]:
        if identity.namespace == "tmdb":
            movie = self._resolve_tmdb_movie(identity.value)
            if movie:
                return AdapterResult(movie)
            series = self._resolve_tmdb_series(identity.value)
            if series:
                return AdapterResult(series)
            return AdapterResult(frozenset({identity}))
        if identity.namespace == "imdb":
            return AdapterResult(frozenset({identity}))
        if identity.namespace == "tvdb":
            return AdapterResult(frozenset({identity}))
        return AdapterResult(frozenset({identity}))

    def _resolve_tmdb_movie(self, tmdb_id: str) -> frozenset[MediaIdentity]:
        try:
            external = self.client.movie_external_ids(tmdb_id)
        except TmdbError:
            return frozenset()
        if not external.get("id") and not external.get("imdb_id"):
            return frozenset()
        identities = {MediaIdentity("tmdb", tmdb_id)}
        imdb_id = external.get("imdb_id")
        if imdb_id:
            identities.add(MediaIdentity("imdb", str(imdb_id)))
        return frozenset(identities)

    def _resolve_tmdb_series(self, tmdb_id: str) -> frozenset[MediaIdentity]:
        try:
            external = self.client.series_external_ids(tmdb_id)
        except TmdbError:
            return frozenset()
        if not external.get("id") and not external.get("imdb_id") and not external.get("tvdb_id"):
            return frozenset()
        identities = {MediaIdentity("tmdb", tmdb_id)}
        imdb_id = external.get("imdb_id")
        tvdb_id = external.get("tvdb_id")
        if imdb_id:
            identities.add(MediaIdentity("imdb", str(imdb_id)))
        if tvdb_id:
            identities.add(MediaIdentity("tvdb", str(tvdb_id)))
        return frozenset(identities)


def _item_from_search_result(raw: dict[str, Any], media_type: MediaType) -> MediaItem:
    tmdb_id = raw.get("id")
    title = raw.get("title") if media_type is MediaType.MOVIE else raw.get("name")
    if tmdb_id is None or not isinstance(title, str) or not title.strip():
        raise TmdbError("TMDB search result is missing id or title.")

    identities = {MediaIdentity("tmdb", str(tmdb_id))}
    imdb_id = raw.get("imdb_id")
    tvdb_id = raw.get("tvdb_id")
    if imdb_id:
        identities.add(MediaIdentity("imdb", str(imdb_id)))
    if tvdb_id:
        identities.add(MediaIdentity("tvdb", str(tvdb_id)))

    return MediaItem(
        media_type=media_type,
        title=title,
        year=_year_from_search_result(raw, media_type),
        identities=frozenset(identities),
    )


def _year_from_search_result(raw: dict[str, Any], media_type: MediaType) -> int | None:
    date_value = raw.get("release_date") if media_type is MediaType.MOVIE else raw.get("first_air_date")
    if isinstance(date_value, str) and len(date_value) >= 4 and date_value[:4].isdigit():
        return int(date_value[:4])
    return None
