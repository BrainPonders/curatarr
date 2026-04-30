"""TMDB metadata integration package."""

from curatarr.integrations.tmdb.adapter import TmdbMetadataAdapter
from curatarr.integrations.tmdb.client import TmdbClient, TmdbError

__all__ = [
    "TmdbClient",
    "TmdbError",
    "TmdbMetadataAdapter",
]
