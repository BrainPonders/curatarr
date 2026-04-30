"""Read-only Sonarr adapter implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from curatarr.adapters import AdapterResult
from curatarr.domain import ArrRuntimeState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.sonarr.client import SonarrClient, SonarrError


@dataclass(frozen=True)
class SonarrReadAdapter:
    """Read-only Sonarr adapter that maps shows to Curatarr domain objects."""

    client: SonarrClient

    def lookup_series(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        if identity.namespace == "tvdb":
            raw = self.client.lookup_by_tvdb_id(identity.value)
        elif identity.namespace == "tmdb":
            raw = self.client.lookup_by_tmdb_id(identity.value)
        elif identity.namespace == "imdb":
            raw = self.client.lookup_by_imdb_id(identity.value)
        else:
            return AdapterResult(None, diagnostics={"unsupported_identity": str(identity)})
        if raw is None:
            return AdapterResult(None, diagnostics={"identity": str(identity)})
        return AdapterResult(_series_from_raw(raw), diagnostics={"source": "sonarr"})

    def add_series(
        self,
        item: MediaItem,
        *,
        profile: str,
        root_folder: str,
        monitored: bool,
        search: bool,
    ) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Sonarr writes are not implemented in the read-only adapter.")

    def update_series_runtime_state(
        self,
        identity: MediaIdentity,
        runtime_state: ArrRuntimeState,
    ) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Sonarr writes are not implemented in the read-only adapter.")

    def sync_exclusions(self, archived_items: Sequence[MediaItem]) -> AdapterResult[Sequence[MediaItem]]:
        raise NotImplementedError("Sonarr exclusion sync is not implemented in the read-only adapter.")


def _series_from_raw(raw: dict[str, Any]) -> MediaItem:
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SonarrError("Sonarr series payload is missing title.")

    identities = _identities_from_raw(raw)
    if not identities:
        raise SonarrError("Sonarr series payload is missing typed identifiers.")

    return MediaItem(
        media_type=MediaType.SERIES,
        title=title,
        year=_year_from_raw(raw),
        identities=identities,
        arr_runtime_state=_runtime_state_from_raw(raw),
    )


def _identities_from_raw(raw: dict[str, Any]) -> frozenset[MediaIdentity]:
    identities = set()
    tvdb_id = raw.get("tvdbId") or raw.get("tvdb_id")
    tmdb_id = raw.get("tmdbId") or raw.get("tmdb_id")
    imdb_id = raw.get("imdbId") or raw.get("imdb_id")
    sonarr_id = raw.get("id")
    if tvdb_id is not None:
        identities.add(MediaIdentity("tvdb", str(tvdb_id)))
    if tmdb_id is not None:
        identities.add(MediaIdentity("tmdb", str(tmdb_id)))
    if imdb_id:
        identities.add(MediaIdentity("imdb", str(imdb_id)))
    if sonarr_id is not None:
        identities.add(MediaIdentity("sonarr", str(sonarr_id)))
    return frozenset(identities)


def _year_from_raw(raw: dict[str, Any]) -> int | None:
    year = raw.get("year")
    if isinstance(year, int):
        return year
    first_aired = raw.get("firstAired") or raw.get("first_air_date")
    if isinstance(first_aired, str) and len(first_aired) >= 4 and first_aired[:4].isdigit():
        return int(first_aired[:4])
    return None


def _runtime_state_from_raw(raw: dict[str, Any]) -> ArrRuntimeState:
    monitored = bool(raw.get("monitored", False))
    has_file = _has_files(raw)
    if has_file and monitored:
        return ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED
    if has_file and not monitored:
        return ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED
    if not has_file and monitored:
        return ArrRuntimeState.ACTIVE_MISSING_MONITORED
    return ArrRuntimeState.ACTIVE_MISSING_UNMONITORED


def _has_files(raw: dict[str, Any]) -> bool:
    statistics = raw.get("statistics")
    if isinstance(statistics, dict):
        episode_file_count = statistics.get("episodeFileCount")
        if isinstance(episode_file_count, int):
            return episode_file_count > 0
        size_on_disk = statistics.get("sizeOnDisk")
        if isinstance(size_on_disk, int):
            return size_on_disk > 0
    if raw.get("episodeFileCount") not in (None, 0):
        return True
    if raw.get("sizeOnDisk") not in (None, 0):
        return True
    return False
