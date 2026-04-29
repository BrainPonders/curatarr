"""Media identity and state primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


_IDENTITY_PATTERN = re.compile(r"^(?P<namespace>[a-z][a-z0-9_]*):(?P<value>[^:\s]+)$")


class MediaType(str, Enum):
    """Supported logical media types."""

    MOVIE = "movie"
    SERIES = "series"


class DurableState(str, Enum):
    """Durable media state language owned by Ryot or a future equivalent backend."""

    LIBRARY = "library"
    WATCHED = "watched"
    ARCHIVED = "archived"
    OWNED = "owned"
    RADARR = "radarr"
    SONARR = "sonarr"


class ArrRuntimeState(str, Enum):
    """Runtime execution states owned by Radarr or Sonarr."""

    NOT_ACTIVE = "not_active"
    ACTIVE_MISSING_MONITORED = "active_missing_monitored"
    ACTIVE_MISSING_UNMONITORED = "active_missing_unmonitored"
    ACTIVE_DOWNLOADED_MONITORED = "active_downloaded_monitored"
    ACTIVE_DOWNLOADED_UNMONITORED = "active_downloaded_unmonitored"

    @property
    def is_invalid(self) -> bool:
        return self is ArrRuntimeState.ACTIVE_MISSING_UNMONITORED

    @property
    def file_present(self) -> bool:
        return self in {
            ArrRuntimeState.ACTIVE_DOWNLOADED_MONITORED,
            ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED,
        }

    @property
    def arr_active(self) -> bool:
        return self is not ArrRuntimeState.NOT_ACTIVE


class MatchConfidence(str, Enum):
    """Confidence level for cross-system media matching."""

    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


@dataclass(frozen=True, order=True)
class MediaIdentity:
    """Typed external identifier such as `tmdb:123` or `imdb:tt123456`."""

    namespace: str
    value: str

    def __post_init__(self) -> None:
        normalized_namespace = self.namespace.strip().lower()
        normalized_value = self.value.strip()
        if not normalized_namespace or not normalized_value:
            raise ValueError("Media identity namespace and value are required.")
        if ":" in normalized_namespace or ":" in normalized_value:
            raise ValueError("Media identity namespace and value must not contain ':'.")
        object.__setattr__(self, "namespace", normalized_namespace)
        object.__setattr__(self, "value", normalized_value)

    def __str__(self) -> str:
        return f"{self.namespace}:{self.value}"


@dataclass(frozen=True)
class ParsedIdentity:
    """Parsed representation of a namespaced media identity string."""

    raw: str
    identity: MediaIdentity


@dataclass(frozen=True)
class MediaItem:
    """Logical media item with known identities and current state facts."""

    media_type: MediaType
    title: str
    year: int | None = None
    identities: frozenset[MediaIdentity] = field(default_factory=frozenset)
    durable_states: frozenset[DurableState] = field(default_factory=frozenset)
    arr_runtime_state: ArrRuntimeState = ArrRuntimeState.NOT_ACTIVE

    @property
    def is_watched(self) -> bool:
        return DurableState.WATCHED in self.durable_states

    @property
    def is_archived(self) -> bool:
        return DurableState.ARCHIVED in self.durable_states

    @property
    def was_owned(self) -> bool:
        return DurableState.OWNED in self.durable_states

    @property
    def file_present(self) -> bool:
        return self.arr_runtime_state.file_present


def parse_identity(raw: str) -> ParsedIdentity:
    """Parse a string identity and require an explicit namespace."""

    match = _IDENTITY_PATTERN.match(raw.strip())
    if not match:
        raise ValueError(f"Invalid media identity {raw!r}; expected namespace:value.")
    return ParsedIdentity(
        raw=raw,
        identity=MediaIdentity(
            namespace=match.group("namespace"),
            value=match.group("value"),
        ),
    )


def can_authorize_destructive_action(confidence: MatchConfidence) -> bool:
    """Return whether a match can authorize archive, delete, or removal flows."""

    return confidence is MatchConfidence.STRONG
