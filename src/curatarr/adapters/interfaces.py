"""Protocol interfaces for Curatarr external adapters."""

from __future__ import annotations

from typing import Protocol, Sequence

from curatarr.adapters.base import AdapterResult
from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity, MediaItem, MediaType


class RyotAdapter(Protocol):
    """Durable-state adapter boundary."""

    def get_item(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        """Return the current durable media item matching an identity, if known."""

    def scan_collection_memberships(
        self,
        states: frozenset[DurableState],
    ) -> AdapterResult[Sequence[MediaItem]]:
        """Read durable-state collection memberships as domain media items."""

    def apply_state(
        self,
        identity: MediaIdentity,
        state: DurableState,
    ) -> AdapterResult[MediaItem]:
        """Apply a durable state such as Archived, Owned, Radarr, or Sonarr."""

    def remove_state(
        self,
        identity: MediaIdentity,
        state: DurableState,
    ) -> AdapterResult[MediaItem]:
        """Remove a durable overlay state when workflow rules authorize it."""


class RadarrAdapter(Protocol):
    """Movie execution-system adapter boundary."""

    def lookup_movie(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        """Read current Radarr movie state as a domain media item."""

    def add_movie(
        self,
        item: MediaItem,
        *,
        profile: str,
        root_folder: str,
        monitored: bool,
        search: bool,
    ) -> AdapterResult[MediaItem]:
        """Add a movie to Radarr and return verified domain state."""

    def update_movie_runtime_state(
        self,
        identity: MediaIdentity,
        runtime_state: ArrRuntimeState,
    ) -> AdapterResult[MediaItem]:
        """Update monitoring/runtime facts and return verified domain state."""

    def sync_exclusions(self, archived_items: Sequence[MediaItem]) -> AdapterResult[Sequence[MediaItem]]:
        """Project archived movie state into Radarr exclusions."""


class SonarrAdapter(Protocol):
    """Series execution-system adapter boundary."""

    def lookup_series(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        """Read current Sonarr show state as a domain media item."""

    def add_series(
        self,
        item: MediaItem,
        *,
        profile: str,
        root_folder: str,
        monitored: bool,
        search: bool,
    ) -> AdapterResult[MediaItem]:
        """Add a show to Sonarr and return verified domain state."""

    def update_series_runtime_state(
        self,
        identity: MediaIdentity,
        runtime_state: ArrRuntimeState,
    ) -> AdapterResult[MediaItem]:
        """Update show-level monitoring/runtime facts and return verified domain state."""

    def sync_exclusions(self, archived_items: Sequence[MediaItem]) -> AdapterResult[Sequence[MediaItem]]:
        """Project archived show state into Sonarr exclusions."""


class JellyfinAdapter(Protocol):
    """Playback and reception-sensor adapter boundary."""

    def confirm_reception(self, item: MediaItem) -> AdapterResult[bool]:
        """Return whether imported media is indexed and watchable."""

    def trigger_library_scan(self, media_type: MediaType | None = None) -> AdapterResult[bool]:
        """Request a Jellyfin library scan when policy allows it."""


class TelegramAdapter(Protocol):
    """User/admin interaction adapter boundary."""

    def send_home_summary(self, text: str) -> AdapterResult[str]:
        """Send or update the persistent home/control message."""

    def send_decision_prompt(
        self,
        item: MediaItem,
        *,
        audience: str,
        prompt_key: str,
    ) -> AdapterResult[str]:
        """Send a revalidatable decision prompt and return adapter message id."""

    def close_prompt(self, message_id: str, *, reason: str) -> AdapterResult[bool]:
        """Close or mark a prompt terminal so stale buttons are not left open."""


class MetadataAdapter(Protocol):
    """Search and cross-resolution adapter boundary."""

    def search(self, query: str, media_type: MediaType) -> AdapterResult[Sequence[MediaItem]]:
        """Search external metadata and return domain media candidates."""

    def resolve_identities(self, identity: MediaIdentity) -> AdapterResult[frozenset[MediaIdentity]]:
        """Resolve known typed identities for a media item."""
