"""Read-only Radarr adapter implementation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence

from curatarr.adapters import AdapterResult
from curatarr.domain.media import public_rating
from curatarr.domain import ArrRuntimeState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.radarr.client import RadarrClient, RadarrError


@dataclass(frozen=True)
class RadarrReadAdapter:
    """Read-only Radarr adapter that maps movies to Curatarr domain objects."""

    client: RadarrClient

    def scan_movies(self) -> AdapterResult[Sequence[MediaItem]]:
        items = []
        seen = set()
        for raw in self.client.movies():
            if (type(raw.get("id")) is not int or raw["id"] <= 0 or raw["id"] in seen
                    or type(raw.get("monitored")) is not bool or type(raw.get("hasFile")) is not bool):
                raise RadarrError("Radarr snapshot has incomplete or duplicate runtime records.")
            seen.add(raw["id"])
            try:
                items.append(_movie_from_raw(raw))
            except (TypeError, ValueError) as exc:
                raise RadarrError("Radarr snapshot has invalid typed identifiers.") from exc
        return AdapterResult(tuple(items), diagnostics={"source": "radarr"})

    def quality_profile(self, profile_id: int) -> dict[str, Any]:
        raw = self.client.quality_profile(profile_id)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RadarrError("Radarr quality profile is missing its name.")
        items = raw.get("formatItems")
        items = items if isinstance(items, list) else []
        return {"name": name, "upgrade_allowed": raw.get("upgradeAllowed")
                if type(raw.get("upgradeAllowed")) is bool else None,
                "cutoff_quality": _cutoff_name(raw.get("items"), raw.get("cutoff")),
                "cutoff_qualities": _cutoff_qualities(raw.get("items"), raw.get("cutoff")),
                "score_target": _integer(raw.get("cutoffFormatScore")),
                "language": raw.get("language") if isinstance(raw.get("language"), dict) else None,
                "format_items": [{"id": item["format"], "score": item["score"]}
                                 for item in items if isinstance(item, dict)
                                 and type(item.get("format")) is int and type(item.get("score")) is int]}

    def file_details(self, file_id: int, movie_id: int) -> dict[str, Any]:
        raw = self.client.movie_file(file_id, movie_id)
        quality = raw.get("quality")
        quality = quality.get("quality") if isinstance(quality, dict) else None
        formats = raw.get("customFormats")
        format_ids = ([item["id"] for item in formats] if isinstance(formats, list)
                      and all(isinstance(item, dict) and type(item.get("id")) is int for item in formats) else None)
        languages = raw.get("languages")
        languages = ([item["id"] for item in languages] if isinstance(languages, list)
                     and all(isinstance(item, dict) and type(item.get("id")) is int for item in languages) else None)
        return {"score": _integer(raw.get("customFormatScore")), "format_ids": format_ids,
                "quality": _quality_facts(quality), "language_ids": languages,
                "quality_cutoff_not_met": raw.get("qualityCutoffNotMet")
                if type(raw.get("qualityCutoffNotMet")) is bool else None}

    def core_formats(self) -> list[dict[str, Any]]:
        return [core for raw in self.client.custom_formats() if (core := _core_format(raw)) is not None]

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

    movie_file = raw.get("movieFile") if isinstance(raw.get("movieFile"), dict) else {}
    quality = movie_file.get("quality") if isinstance(movie_file.get("quality"), dict) else {}
    quality = quality.get("quality") if isinstance(quality.get("quality"), dict) else {}
    size = movie_file.get("size")
    original = raw.get("originalLanguage")
    ratings = raw.get("ratings") if isinstance(raw.get("ratings"), dict) else {}
    imdb = ratings.get("imdb") if isinstance(ratings.get("imdb"), dict) else {}

    return MediaItem(
        media_type=MediaType.MOVIE,
        title=title,
        year=_year_from_raw(raw),
        identities=identities,
        arr_runtime_state=_runtime_state_from_raw(raw),
        imdb_rating=public_rating(imdb.get("value"), imdb.get("votes"))
        if any(identity.namespace == "imdb" and re.fullmatch(r"tt[0-9]{5,12}", identity.value)
               for identity in identities) else None,
        quality_name=quality.get("name") if isinstance(quality.get("name"), str) else None,
        size_bytes=size if type(size) is int and size >= 0 else None,
        quality_profile_id=raw.get("qualityProfileId")
        if type(raw.get("qualityProfileId")) is int and raw["qualityProfileId"] > 0 else None,
        movie_file_id=movie_file.get("id") if type(movie_file.get("id")) is int and movie_file["id"] > 0 else None,
        original_language_id=original.get("id") if isinstance(original, dict)
        and type(original.get("id")) is int and original["id"] > 0 else None,
    )


def _integer(value: Any) -> int | None:
    return value if type(value) is int else None


def _quality_facts(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {"resolution": _integer(raw.get("resolution")),
            "modifier": raw.get("modifier") if isinstance(raw.get("modifier"), str) else None}


def _cutoff_qualities(items: Any, cutoff: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or type(cutoff) is not int:
        return []
    for item in items:
        if not isinstance(item, dict):
            continue
        quality = item.get("quality")
        if item.get("id") == cutoff or isinstance(quality, dict) and quality.get("id") == cutoff:
            return _quality_leaves([item])
        found = _cutoff_qualities(item.get("items"), cutoff)
        if found:
            return found
    return []


def _quality_leaves(items: list) -> list[dict[str, Any]]:
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("quality"), dict):
            result.append(_quality_facts(item["quality"]))
        elif isinstance(item.get("items"), list):
            result.extend(_quality_leaves(item["items"]))
    return result


def _core_format(raw: dict[str, Any]) -> dict[str, Any] | None:
    # Recognize only simple core definitions; Radarr still decides whether a file matches.
    specs = raw.get("specifications")
    if (type(raw.get("id")) is not int or not isinstance(raw.get("name"), str)
            or not isinstance(specs, list) or not specs
            or any(not isinstance(s, dict) or s.get("negate") is not False for s in specs)
            or len(specs) > 1 and any(s.get("required") is not True for s in specs)):
        return None
    implementations = {s.get("implementation") for s in specs}
    if any(not isinstance(s.get("fields"), list) or any(not isinstance(f, dict) for f in s["fields"]) for s in specs):
        return None
    fields = [{f.get("name"): f.get("value") for f in s.get("fields", []) if isinstance(f, dict)} for s in specs]
    label, key, category = raw["name"], f"format:{raw['id']}", "language"
    if implementations == {"ResolutionSpecification"} and len(specs) == 1:
        value = fields[0].get("value")
        if type(value) is not int or value <= 0:
            return None
        label, key, category = "4K" if value == 2160 else f"{value}p", f"resolution:{value}", "quality"
    elif implementations == {"QualityModifierSpecification"} and len(specs) == 1 and fields[0].get("value") == 5:
        label, key, category = "Remux", "remux", "remux"
    elif implementations == {"LanguageSpecification"}:
        if any(type(f.get("value")) is not int or f["value"] not in {-2} and f["value"] <= 0 for f in fields):
            return None
        if len(specs) == 1 and fields[0].get("exceptLanguage") is False:
            value = fields[0]["value"]
            value_field = next((f for f in specs[0].get("fields", []) if f.get("name") == "value"), {})
            options = value_field.get("selectOptions")
            options = options if isinstance(options, list) else []
            name = next((o.get("name") for o in options if isinstance(o, dict) and o.get("value") == value
                         and isinstance(o.get("name"), str)), None)
            label, key = "Original audio" if value == -2 else name or label, f"language:{value}"
    else:
        return None
    return {"id": raw["id"], "label": label, "key": key, "category": category}


def _cutoff_name(items: Any, cutoff: Any) -> str | None:
    if not isinstance(items, list) or type(cutoff) is not int:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        quality = item.get("quality")
        quality = quality if isinstance(quality, dict) else {}
        if item.get("id") == cutoff or quality.get("id") == cutoff:
            name = item.get("name") or quality.get("name")
            if isinstance(name, str) and name.strip():
                return name
        nested = _cutoff_name(item.get("items"), cutoff)
        if nested:
            return nested
    return None


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
