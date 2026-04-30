"""Sonarr integration package."""

from curatarr.integrations.sonarr.adapter import SonarrReadAdapter
from curatarr.integrations.sonarr.client import SonarrClient, SonarrError

__all__ = [
    "SonarrClient",
    "SonarrError",
    "SonarrReadAdapter",
]
