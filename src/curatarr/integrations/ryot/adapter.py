"""Read-only Ryot adapter implementation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from curatarr.adapters import AdapterResult
from curatarr.domain import DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.ryot.client import RyotClient, RyotError


METADATA_BY_ID_QUERY = """
query CuratarrMetadataById($metadataId: String!) {
  metadataDetails(metadataId: $metadataId) {
    response
  }
  userMetadataDetails(metadataId: $metadataId) {
    response
  }
}
"""

METADATA_SEARCH_QUERY = """
query CuratarrMetadataSearch($query: String!) {
  metadataSearch(query: $query) {
    response {
      items
    }
  }
}
"""

COLLECTION_MEMBERS_QUERY = """
query CuratarrCollectionMembers($collectionName: String!) {
  collectionContents(collectionName: $collectionName) {
    response {
      items
    }
  }
}
"""


@dataclass(frozen=True)
class RyotStateCollections:
    """Collection names used to represent Curatarr durable states in Ryot."""

    archived: str = "Archived"
    owned: str = "Owned"
    radarr: str = "Radarr"
    sonarr: str = "Sonarr"

    def state_for_collection(self, name: str) -> DurableState | None:
        normalized = name.casefold()
        mapping = {
            self.archived.casefold(): DurableState.ARCHIVED,
            self.owned.casefold(): DurableState.OWNED,
            self.radarr.casefold(): DurableState.RADARR,
            self.sonarr.casefold(): DurableState.SONARR,
        }
        return mapping.get(normalized)

    def collection_for_state(self, state: DurableState) -> str | None:
        return {
            DurableState.ARCHIVED: self.archived,
            DurableState.OWNED: self.owned,
            DurableState.RADARR: self.radarr,
            DurableState.SONARR: self.sonarr,
        }.get(state)


@dataclass(frozen=True)
class RyotReadAdapter:
    """Read-only Ryot adapter that maps Ryot responses to domain objects."""

    client: RyotClient
    collections: RyotStateCollections = RyotStateCollections()

    def get_item(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        metadata_id = identity.value if identity.namespace == "ryot" else self._resolve_metadata_id(identity)
        if metadata_id is None:
            return AdapterResult(None, diagnostics={"identity": str(identity)})

        data = self.client.execute(METADATA_BY_ID_QUERY, {"metadataId": metadata_id})
        item = _item_from_metadata_details(data, self.collections)
        return AdapterResult(item, diagnostics={"metadata_id": metadata_id})

    def scan_collection_memberships(
        self,
        states: frozenset[DurableState],
    ) -> AdapterResult[Sequence[MediaItem]]:
        requested = states or frozenset(
            {
                DurableState.ARCHIVED,
                DurableState.OWNED,
                DurableState.RADARR,
                DurableState.SONARR,
            }
        )
        merged: dict[MediaIdentity, MediaItem] = {}
        for state in requested:
            collection_name = self.collections.collection_for_state(state)
            if collection_name is None:
                continue
            data = self.client.execute(COLLECTION_MEMBERS_QUERY, {"collectionName": collection_name})
            for raw_item in _extract_collection_items(data):
                item = _media_item_from_raw(
                    raw_item,
                    self.collections,
                    additional_states={state},
                )
                key = _stable_key(item)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = item
                else:
                    merged[key] = _merge_items(existing, item)
        return AdapterResult(tuple(merged.values()), diagnostics={"collections_scanned": str(len(requested))})

    def apply_state(self, identity: MediaIdentity, state: DurableState) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Ryot state writes are not implemented in the read-only adapter.")

    def remove_state(self, identity: MediaIdentity, state: DurableState) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Ryot state writes are not implemented in the read-only adapter.")

    def _resolve_metadata_id(self, identity: MediaIdentity) -> str | None:
        data = self.client.execute(METADATA_SEARCH_QUERY, {"query": identity.value})
        for raw_item in _extract_search_items(data):
            identities = _identities_from_raw(raw_item)
            if identity in identities:
                metadata_id = raw_item.get("id") or raw_item.get("metadataId")
                return str(metadata_id) if metadata_id is not None else None
        return None


def _item_from_metadata_details(data: dict[str, Any], collections: RyotStateCollections) -> MediaItem:
    metadata = _response_payload(data.get("metadataDetails"))
    user_metadata = _response_payload(data.get("userMetadataDetails"))
    if not isinstance(metadata, dict):
        raise RyotError("Ryot metadataDetails response is missing metadata.")
    if isinstance(user_metadata, dict):
        combined = {**metadata, **user_metadata, "metadata": metadata}
    else:
        combined = metadata
    return _media_item_from_raw(combined, collections)


def _media_item_from_raw(
    raw: dict[str, Any],
    collections: RyotStateCollections,
    *,
    additional_states: Iterable[DurableState] = (),
) -> MediaItem:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else raw
    title = metadata.get("title") or raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise RyotError("Ryot media item is missing a title.")

    states = {DurableState.LIBRARY, *additional_states}
    if _is_completed(raw):
        states.add(DurableState.WATCHED)
    for collection_name in _collection_names(raw):
        state = collections.state_for_collection(collection_name)
        if state is not None:
            states.add(state)

    return MediaItem(
        media_type=_media_type_from_raw(metadata),
        title=title,
        year=_year_from_raw(metadata),
        identities=_identities_from_raw(metadata),
        durable_states=frozenset(states),
    )


def _identities_from_raw(raw: dict[str, Any]) -> frozenset[MediaIdentity]:
    identities: set[MediaIdentity] = set()
    metadata_id = raw.get("id") or raw.get("metadataId")
    if metadata_id is not None:
        identities.add(MediaIdentity("ryot", str(metadata_id)))

    source = str(raw.get("source") or "").lower()
    identifier = raw.get("identifier")
    if identifier is not None and source in {"tmdb", "imdb", "tvdb"}:
        identities.add(MediaIdentity(source, str(identifier)))

    external_ids = raw.get("externalIds")
    if isinstance(external_ids, dict):
        for namespace in ("tmdb", "imdb", "tvdb"):
            value = external_ids.get(namespace)
            if value is not None:
                identities.add(MediaIdentity(namespace, str(value)))
    return frozenset(identities)


def _media_type_from_raw(raw: dict[str, Any]) -> MediaType:
    lot = str(raw.get("lot") or raw.get("mediaType") or "").upper()
    if lot in {"SHOW", "SERIES", "TV_SHOW"}:
        return MediaType.SERIES
    return MediaType.MOVIE


def _year_from_raw(raw: dict[str, Any]) -> int | None:
    value = raw.get("publishYear") or raw.get("year")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_completed(raw: dict[str, Any]) -> bool:
    if raw.get("seenByUserCount", 0):
        return True
    history = raw.get("history")
    if isinstance(history, list):
        for event in history:
            if isinstance(event, dict) and str(event.get("state") or "").upper() == "COMPLETED":
                return True
    return False


def _collection_names(raw: dict[str, Any]) -> tuple[str, ...]:
    collections = raw.get("collections", ())
    names = []
    if isinstance(collections, list):
        for collection in collections:
            if isinstance(collection, str):
                names.append(collection)
            elif isinstance(collection, dict):
                details = collection.get("details")
                if isinstance(details, dict) and isinstance(details.get("collectionName"), str):
                    names.append(details["collectionName"])
                elif isinstance(collection.get("collectionName"), str):
                    names.append(collection["collectionName"])
    return tuple(names)


def _response_payload(value: Any) -> Any:
    if isinstance(value, dict) and "response" in value:
        return value["response"]
    return value


def _extract_search_items(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    payload = _response_payload(data.get("metadataSearch"))
    if isinstance(payload, dict):
        items = payload.get("items", ())
        return tuple(item for item in items if isinstance(item, dict))
    return ()


def _extract_collection_items(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    payload = _response_payload(data.get("collectionContents"))
    if isinstance(payload, dict):
        items = payload.get("items", ())
        return tuple(item for item in items if isinstance(item, dict))
    return ()


def _stable_key(item: MediaItem) -> MediaIdentity:
    if item.identities:
        return sorted(item.identities)[0]
    return MediaIdentity("title", item.title.casefold())


def _merge_items(left: MediaItem, right: MediaItem) -> MediaItem:
    return MediaItem(
        media_type=left.media_type,
        title=left.title,
        year=left.year or right.year,
        identities=left.identities | right.identities,
        durable_states=left.durable_states | right.durable_states,
        arr_runtime_state=left.arr_runtime_state,
    )
