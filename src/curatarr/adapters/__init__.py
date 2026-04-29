"""Adapter interfaces for external systems."""

from curatarr.adapters.base import AdapterError, AdapterResult, ExternalSignal
from curatarr.adapters.interfaces import (
    JellyfinAdapter,
    MetadataAdapter,
    RadarrAdapter,
    RyotAdapter,
    SonarrAdapter,
    TelegramAdapter,
)

__all__ = [
    "AdapterError",
    "AdapterResult",
    "ExternalSignal",
    "JellyfinAdapter",
    "MetadataAdapter",
    "RadarrAdapter",
    "RyotAdapter",
    "SonarrAdapter",
    "TelegramAdapter",
]
