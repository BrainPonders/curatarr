"""Shared adapter result and error types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar


T = TypeVar("T")


class AdapterError(RuntimeError):
    """Base error raised by adapter implementations."""


class ExternalSignal(str, Enum):
    """External event signal types that require Curatarr revalidation."""

    RYOT_SCAN = "ryot_scan"
    RADARR_WEBHOOK = "radarr_webhook"
    SONARR_WEBHOOK = "sonarr_webhook"
    JELLYFIN_RECEPTION = "jellyfin_reception"


@dataclass(frozen=True)
class AdapterResult(Generic[T]):
    """Typed adapter result with optional diagnostics for logs or admin views."""

    value: T
    diagnostics: dict[str, str] = field(default_factory=dict)
