"""Read-only Ryot adapter implementation."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from curatarr.adapters import AdapterResult
from curatarr.domain import DurableState, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.ryot.client import RyotClient, RyotError


METADATA_FIELDS = "id title identifier source lot publishYear externalIdentifiers { tvdbId }"
USER_METADATA_FIELDS = "hasInteracted history { state progress finishedOn } collections { details { collectionName } }"

METADATA_BY_ID_QUERY = """
query CuratarrMetadataById($metadataId: String!) {
  metadataDetails(metadataId: $metadataId) {
    response { id title identifier source lot publishYear externalIdentifiers { tvdbId } }
  }
  userMetadataDetails(metadataId: $metadataId) {
    response { hasInteracted history { state progress finishedOn } collections { details { collectionName } } }
  }
}
"""

USER_METADATA_LIST_QUERY = """
query CuratarrUserMetadataList($input: UserMetadataListInput!) {
  userMetadataList(input: $input) {
    response {
      items
      details { nextPage }
    }
  }
}
"""

@dataclass(frozen=True)
class RyotStateCollections:
    """Collection names used to represent Curatarr durable states in Ryot."""

    archived: str = "Archived"
    library: str = "Library"
    previously_acquired: str = "Previously acquired"
    deleted: str = "Deleted"
    radarr: str = "Radarr"
    sonarr: str = "Sonarr"

    def state_for_collection(self, name: str) -> DurableState | None:
        normalized = name.casefold()
        mapping = {
            self.archived.casefold(): DurableState.ARCHIVED,
            self.library.casefold(): DurableState.LIBRARY,
            self.previously_acquired.casefold(): DurableState.PREVIOUSLY_ACQUIRED,
            self.deleted.casefold(): DurableState.DELETED,
            self.radarr.casefold(): DurableState.RADARR,
            self.sonarr.casefold(): DurableState.SONARR,
        }
        return mapping.get(normalized)

    def collection_for_state(self, state: DurableState) -> str | None:
        return {
            DurableState.ARCHIVED: self.archived,
            DurableState.LIBRARY: self.library,
            DurableState.PREVIOUSLY_ACQUIRED: self.previously_acquired,
            DurableState.DELETED: self.deleted,
            DurableState.RADARR: self.radarr,
            DurableState.SONARR: self.sonarr,
        }.get(state)


@dataclass(frozen=True)
class RyotReadAdapter:
    """Read-only Ryot adapter that maps Ryot responses to domain objects."""

    client: RyotClient
    collections: RyotStateCollections = RyotStateCollections()
    _resolved_ids: dict[MediaIdentity, str] = field(default_factory=dict, repr=False, compare=False)

    def scan_movies(self) -> AdapterResult[Sequence[MediaItem]]:
        items = []
        seen = set()
        for raw in self._library_records(include_user_state=True):
            metadata = raw["metadata"]
            if not isinstance(metadata.get("lot"), str):
                raise RyotError("Ryot snapshot is missing media type evidence.")
            if metadata["id"] in seen:
                raise RyotError("Ryot snapshot contains duplicate metadata records.")
            seen.add(metadata["id"])
            if metadata["lot"].upper() == "MOVIE":
                try:
                    items.append(_media_item_from_raw(raw, self.collections))
                except (TypeError, ValueError) as exc:
                    raise RyotError("Ryot snapshot has invalid typed identifiers.") from exc
        return AdapterResult(tuple(items), diagnostics={"source": "ryot_user_library"})

    def get_item(self, identity: MediaIdentity) -> AdapterResult[MediaItem | None]:
        metadata_id = identity.value if identity.namespace == "ryot" else self._resolve_metadata_id(identity)
        if metadata_id is None:
            return AdapterResult(None, diagnostics={"identity": str(identity)})

        data = self.client.execute(METADATA_BY_ID_QUERY, {"metadataId": metadata_id})
        user = _response_payload(data.get("userMetadataDetails"))
        if not isinstance(user, dict):
            raise RyotError("Ryot userMetadataDetails response is missing user state.")
        if user.get("hasInteracted") is False:
            return AdapterResult(None, diagnostics={"identity": str(identity)})
        if not isinstance(user.get("collections"), list):
            raise RyotError("Ryot user state is missing collection evidence.")
        item = _item_from_metadata_details(data, self.collections)
        return AdapterResult(item, diagnostics={"metadata_id": metadata_id})

    def scan_collection_memberships(
        self,
        states: frozenset[DurableState],
    ) -> AdapterResult[Sequence[MediaItem]]:
        requested = states or frozenset(
            {
                DurableState.ARCHIVED,
                DurableState.LIBRARY,
                DurableState.PREVIOUSLY_ACQUIRED,
                DurableState.DELETED,
                DurableState.WATCHED,
                DurableState.RADARR,
                DurableState.SONARR,
            }
        )
        items = []
        for raw in self._library_records(include_user_state=True):
            item = _media_item_from_raw(raw, self.collections)
            if item.durable_states & requested:
                items.append(item)
        return AdapterResult(tuple(items), diagnostics={"source": "ryot_user_library"})

    def apply_state(self, identity: MediaIdentity, state: DurableState) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Ryot state writes are not implemented in the read-only adapter.")

    def remove_state(self, identity: MediaIdentity, state: DurableState) -> AdapterResult[MediaItem]:
        raise NotImplementedError("Ryot state writes are not implemented in the read-only adapter.")

    def _resolve_metadata_id(self, identity: MediaIdentity) -> str | None:
        if identity in self._resolved_ids:
            return self._resolved_ids[identity]
        # Ryot text search matches titles/descriptions, not external IDs. Read the user's
        # paginated catalogue and compare exact typed IDs; never infer absence from a title search.
        for raw in self._library_records():
            for found in _identities_from_raw(raw):
                self._resolved_ids[found] = str(raw["id"])
            if identity in self._resolved_ids:
                return self._resolved_ids[identity]
        return None

    def _library_records(self, *, include_user_state: bool = False, user_fields: str = USER_METADATA_FIELDS) -> Iterator[dict[str, Any]]:
        page = 1
        visited = set()
        while page is not None:
            if type(page) is not int or page < 1 or page in visited:
                raise RyotError("Ryot returned invalid library pagination.")
            visited.add(page)
            data = self.client.execute(USER_METADATA_LIST_QUERY, {
                "input": {"search": {"page": page, "take": 50}, "sort": {"by": "TITLE", "order": "ASC"}}})
            payload = _response_payload(data.get("userMetadataList"))
            if (not isinstance(payload, dict) or not isinstance(payload.get("items"), list)
                    or not isinstance(payload.get("details"), dict) or "nextPage" not in payload["details"]):
                raise RyotError("Ryot returned an incomplete user library page.")
            ids = payload["items"]
            if any(not isinstance(value, str) or not value for value in ids):
                raise RyotError("Ryot user library did not contain metadata IDs.")
            for start in range(0, len(ids), 50):
                batch = ids[start:start + 50]
                definitions = ", ".join(f"$id{i}: String!" for i in range(len(batch)))
                fields = []
                for i in range(len(batch)):
                    fields.append(f"m{i}: metadataDetails(metadataId: $id{i}) {{ response {{ {METADATA_FIELDS} }} }}")
                    if include_user_state:
                        fields.append(f"u{i}: userMetadataDetails(metadataId: $id{i}) {{ response {{ {user_fields} }} }}")
                result = self.client.execute(
                    f"query CuratarrLibraryDetails({definitions}) {{ {' '.join(fields)} }}",
                    {f"id{i}": value for i, value in enumerate(batch)},
                )
                for i in range(len(batch)):
                    metadata = _response_payload(result.get(f"m{i}"))
                    if not isinstance(metadata, dict) or metadata.get("id") != batch[i]:
                        raise RyotError("Ryot library metadata response was incomplete or mismatched.")
                    if include_user_state:
                        user = _response_payload(result.get(f"u{i}"))
                        if not isinstance(user, dict):
                            raise RyotError("Ryot library user state response was incomplete.")
                        if user.get("hasInteracted") is False:
                            continue
                        if not isinstance(user.get("collections"), list):
                            raise RyotError("Ryot library user state is missing collection evidence.")
                        yield {**metadata, **user, "metadata": metadata}
                    else:
                        yield metadata
            page = payload["details"]["nextPage"]


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

    states = set(additional_states)
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
        completed_watch_count=_watch_count(raw),
        last_watched_date=_last_watch_date(raw),
        collection_names=_collection_names(raw),
    )


def _watch_count(raw: dict[str, Any]) -> int | None:
    history = raw.get("history")
    if not isinstance(history, list) or any(not isinstance(event, dict) or not event.get("state") for event in history):
        return None
    count = sum(str(event.get("state", "")).upper() == "COMPLETED" for event in history)
    # A Completed collection proves a viewing, but not its count when no events exist.
    return None if count == 0 and _is_completed(raw) else count


def _last_watch_date(raw: dict[str, Any]) -> str | None:
    history = raw.get("history")
    if not isinstance(history, list):
        return None
    dates = []
    for event in history:
        if isinstance(event, dict) and str(event.get("state", "")).upper() == "COMPLETED":
            try:
                dates.append(datetime.fromisoformat(event["finishedOn"].replace("Z", "+00:00")).date())
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
    return max(dates).isoformat() if dates else None


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
    external_identifiers = raw.get("externalIdentifiers")
    if isinstance(external_identifiers, dict) and external_identifiers.get("tvdbId") is not None:
        identities.add(MediaIdentity("tvdb", str(external_identifiers["tvdbId"])))
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
    history = raw.get("history")
    if isinstance(history, list):
        for event in history:
            if isinstance(event, dict) and str(event.get("state") or "").upper() == "COMPLETED":
                return True
        return any(name.casefold() == "completed" for name in _collection_names(raw))
    if any(name.casefold() == "completed" for name in _collection_names(raw)):
        return True
    if not isinstance(history, list) and raw.get("seenByUserCount", 0):
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
                else:
                    raise RyotError("Ryot collection membership is missing its name.")
            else:
                raise RyotError("Ryot collection membership is invalid.")
    return tuple(names)


def _response_payload(value: Any) -> Any:
    if isinstance(value, dict) and "response" in value:
        return value["response"]
    return value
