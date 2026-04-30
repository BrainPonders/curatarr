"""Read-only Radarr adapter implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from curatarr.adapters import AdapterResult
from curatarr.domain import ArrRuntimeState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.radarr.client import RadarrClient, RadarrError


@dataclass(frozen=True)
class RadarrReadAdapter:
    """Read-only Radarr adapter that maps movies to Curatarr domain objects."""

    client: RadarrClient

    def lookup_movie(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        if identity.namespace == "tmdb":
            raw = self.client.lookup_by_tmdb_id(identity.value)
        elif identity.namespace == "imdb":
            raw = self.client.lookup_by_imdb_id(identity.value)
        else:
            return AdapterResult(None, diagnostics={"unsupported_identity": str(identity)})
        if raw is None:
            return AdapterResult(None, diagnostics={"identity": str(identity)})
        return AdapterResult(_movie_from_raw(raw), diagnostics={"source": "radarr"})

    def add_movie(
        self,
        item: MediaItem,
        *,
        profile: str,
        root_folder: str,
        monitored: bool,
        search: bool,
    ) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Radarr writes are not implemented in the read-only adapter.")

    def update_movie_runtime_state(
        self,
        identity: MediaIdentity,
        runtime_state: ArrRuntimeState,
    ) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Radarr writes are not implemented in the read-only adapter.")

    def sync_exclusions(self, archived_items: Sequence[MediaItem]) -> AdapterResult[Sequence[MediaItem]]:
        raise NotImplementedError("Radarr exclusion sync is not implemented in the read-only adapter.")


def _movie_from_raw(raw: dict[str, Any]) -> MediaItem:
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise RadarrError("Radarr movie payload is missing title.")

    identities = _identities_from_raw(raw)
    if not identities:
        raise RadarrError("Radarr movie payload is missing typed identifiers.")

    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=_year_from_raw(raw),
        identities=identities,
        arr_runtime_state=_runtime_state_from_raw(raw),
    )


def _identities_from_raw(raw: dict[str, Any]) -> frozenset[MediaIdentity]:
    identities = set()
    tmdb_id = raw.get("tmdbId") or raw.get("tmdb_id")
    imdb_id = raw.get("imdbId") or raw.get("imdb_id")
    radarr_id = raw.get("id")
    if tmdb_id is not None:
        identities.add(MediaIdentity("tmdb", str(tmdb_id)))
    if imdb_id:
        identities.add(MediaIdentity("imdb", str(imdb_id)))
    if radarr_id is not None:
        identities.add(MediaIdentity("radarr", str(radarr_id)))
    return frozenset(identities)


def _year_from_raw(raw: dict[str, Any]) -> int | None:
    year = raw.get("year")
    if isinstance(year, int):
        return year
    if isinstance(year, str) and year.isdigit():
        return int(year)
    return None


def _runtime_state_from_raw(raw: dict[str, Any]) -> ArrRuntimeState:
    monitored = bool(raw.get("monitored", False))
    has_file = _has_file(raw)
    if has_file and monitored:
        return ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED
    if has_file and not monitored:
        return ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED
    if not has_file and monitored:
        return ArrRuntimeState.ACTIVE_MISSING_MONITORED
    return ArrRuntimeState.ACTIVE_MISSING_UNMONITORED


def _has_file(raw: dict[str, Any]) -> bool:
    if isinstance(raw.get("hasFile"), bool):
        return bool(raw["hasFile"])
    if isinstance(raw.get("movieFile"), dict):
        return True
    if raw.get("movieFileId") not in (None, 0):
        return True
    return False
