"""Read-only playback-library evidence, matched by provider IDs."""

from dataclasses import dataclass, field
import json
import threading
import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from curatarr.adapters import AdapterError
from curatarr.domain import MediaType


class JellyfinError(AdapterError):
    """A playback-library read could not establish trustworthy evidence."""


@dataclass(frozen=True)
class JellyfinReadAdapter:
    url: str
    api_key: str = field(repr=False)
    transport: Callable | None = field(default=None, repr=False)
    _catalogue: dict = field(default_factory=dict, repr=False, compare=False)
    _lock: object = field(default_factory=threading.Lock, repr=False, compare=False)

    def _get(self, params):
        if self.transport is not None:
            payload = self.transport("/Items", params)
        else:
            request = Request(self.url.rstrip("/") + "/Items?" + urlencode(params),
                              headers={"X-Emby-Token": self.api_key, "Accept": "application/json"})
            try:
                with urlopen(request, timeout=10) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                raise JellyfinError(f"Playback library HTTP error {exc.code}.") from None
            except (URLError, OSError, ValueError):
                raise JellyfinError("Playback library could not be checked.") from None
        if (not isinstance(payload, dict) or not isinstance(payload.get("Items"), list)
                or type(payload.get("TotalRecordCount")) is not int or payload["TotalRecordCount"] < 0):
            raise JellyfinError("Playback library returned incomplete results.")
        return payload

    def _movie_records(self):
        # Jellyfin's public Items endpoint has no exact provider-ID filter. Cache only
        # the ID catalogue briefly, then re-read selected sources for live facts.
        with self._lock:
            if self._catalogue.get("expires", 0) > time.monotonic():
                return self._catalogue["records"]
            records = []
            seen = set()
            total = None
            deadline = time.monotonic() + 20
            while total is None or len(records) < total:
                if time.monotonic() >= deadline:
                    raise JellyfinError("Playback library scan timed out.")
                payload = self._get({"Recursive": "true", "IncludeItemTypes": "Movie", "Limit": "500",
                                     "StartIndex": str(len(records)), "Fields": "ProviderIds", "EnableImages": "false"})
                if total is not None and total != payload["TotalRecordCount"]:
                    raise JellyfinError("Playback library changed during pagination. Retry.")
                total = payload["TotalRecordCount"]
                if total > 50000 or len(payload["Items"]) > 500 or (not payload["Items"] and len(records) < total):
                    raise JellyfinError("Playback library pagination is incomplete.")
                for item in payload["Items"]:
                    if not isinstance(item, dict) or not isinstance(item.get("Id"), str) or item["Id"] in seen:
                        raise JellyfinError("Playback library pagination is invalid.")
                    seen.add(item["Id"])
                    records.append(item)
                if len(records) > total:
                    raise JellyfinError("Playback library pagination is invalid.")
            self._catalogue.update(records=records, expires=time.monotonic() + 30)
            return records

    def playback_facts(self, candidate) -> dict:
        if candidate.media_type is not MediaType.MOVIE:
            raise JellyfinError("Only movie reception is implemented.")
        identities = [identity for identity in sorted(candidate.identities) if identity.namespace in {"tmdb", "imdb"}]
        if not identities:
            raise JellyfinError("Movie has no supported playback-library identifier.")
        ids = []
        for record in self._movie_records():
            providers = _provider_ids(record.get("ProviderIds"))
            if any(providers.get(identity.namespace) == identity.value for identity in identities):
                ids.append(record["Id"])
        if not ids:
            return {"status": "not_present", "versions": [], "version_count": 0}
        payload = self._get({"Ids": ",".join(ids), "Fields": "ProviderIds,MediaSources,MediaStreams",
                             "EnableImages": "false"})
        if payload["TotalRecordCount"] != len(payload["Items"]):
            raise JellyfinError("Playback library returned incomplete results.")
        versions = []
        ambiguous = False
        for item in payload["Items"]:
            if not isinstance(item, dict):
                raise JellyfinError("Playback library returned invalid records.")
            if not isinstance(item.get("ProviderIds"), dict):
                raise JellyfinError("Playback library omitted provider identifiers.")
            providers = _provider_ids(item["ProviderIds"])
            matched = any(providers.get(identity.namespace) == identity.value for identity in identities)
            conflicting = any(identity.namespace in providers and providers[identity.namespace] != identity.value
                              for identity in identities)
            if item.get("Type") != "Movie" or not matched or conflicting:
                ambiguous = True
                continue
            if item.get("IsPlaceHolder") is True or item.get("LocationType") == "Virtual":
                continue
            sources = item.get("MediaSources")
            if not isinstance(sources, list):
                raise JellyfinError("Playback library omitted media-source evidence.")
            for source in sources:
                if not isinstance(source, dict):
                    raise JellyfinError("Playback library returned invalid source evidence.")
                if not source.get("Path") or source.get("Type") == "Placeholder":
                    continue
                streams = source.get("MediaStreams")
                if not isinstance(streams, list):
                    streams = item.get("MediaStreams", []) if len(sources) == 1 else []
                audio = [stream for stream in streams if isinstance(stream, dict) and stream.get("Type") == "Audio"]
                versions.append({"audio_languages": sorted({str(stream["Language"]).lower() for stream in audio
                                                           if isinstance(stream.get("Language"), str) and stream["Language"]}),
                                 "audio_complete": bool(audio) and all(stream.get("Language") not in (None, "", "und") for stream in audio)})
        if versions:
            # Do not merge different copies into a fictional bilingual single copy.
            return {"status": "ready", "versions": versions, "version_count": len(versions)}
        return {"status": "needs_review" if ambiguous else "not_present", "versions": [], "version_count": 0}

    def maintenance_snapshot(self):
        """Uncached, complete movie/source snapshot; never persist filesystem paths."""
        records, seen, total = [], set(), None
        deadline = time.monotonic() + 120
        while total is None or len(records) < total:
            if time.monotonic() > deadline:
                raise JellyfinError("Playback maintenance scan timed out.")
            payload = self._get({"Recursive": "true", "IncludeItemTypes": "Movie", "Limit": "500",
                                 "StartIndex": str(len(records)), "Fields": "ProviderIds,MediaSources",
                                 "EnableImages": "false"})
            if total is not None and total != payload["TotalRecordCount"]:
                raise JellyfinError("Playback library changed during maintenance pagination.")
            total = payload["TotalRecordCount"]
            if total > 50000 or len(payload["Items"]) > 500 or (not payload["Items"] and len(records) < total):
                raise JellyfinError("Playback maintenance snapshot is incomplete.")
            for item in payload["Items"]:
                record = _maintenance_record(item)
                if record["id"] in seen:
                    raise JellyfinError("Playback maintenance snapshot contains duplicate items.")
                seen.add(record["id"])
                records.append(record)
            if len(records) > total:
                raise JellyfinError("Playback maintenance pagination is invalid.")
        return records

    def maintenance_items(self, item_ids):
        """Read bounded exact IDs, including authoritative absence; return no paths."""
        ids = list(item_ids)
        if (len(ids) > 500
                or any(not isinstance(i, str) or not i or len(i) > 128 or "," in i for i in ids)
                or len(set(ids)) != len(ids)):
            raise JellyfinError("Playback validation identifiers are invalid or exceed the limit.")
        records = []
        deadline = time.monotonic() + 60
        for start in range(0, len(ids), 50):
            if time.monotonic() > deadline:
                raise JellyfinError("Playback item validation timed out.")
            batch = ids[start:start + 50]
            payload = self._get({"Ids": ",".join(batch), "Fields": "ProviderIds,MediaSources",
                                 "EnableImages": "false"})
            if payload["TotalRecordCount"] != len(payload["Items"]):
                raise JellyfinError("Playback item validation is incomplete.")
            selected = [_maintenance_record(item) for item in payload["Items"]]
            returned = {item["id"] for item in selected}
            if len(returned) != len(selected) or not returned <= set(batch):
                raise JellyfinError("Playback item validation returned unexpected or duplicate items.")
            records.extend(selected)
        return records


def _maintenance_record(item):
    if (not isinstance(item, dict) or not isinstance(item.get("Id"), str) or not item["Id"]
            or item.get("Type") != "Movie" or not isinstance(item.get("ProviderIds"), dict)):
        raise JellyfinError("Playback maintenance identity evidence is incomplete.")
    virtual = item.get("IsPlaceHolder") is True or item.get("LocationType") == "Virtual"
    sources = item.get("MediaSources")
    if not virtual and (not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources)):
        raise JellyfinError("Playback maintenance source evidence is incomplete.")
    providers = _provider_ids(item["ProviderIds"])
    return {"id": item["Id"], "identities": [f"{k}:{v}" for k, v in sorted(providers.items())
                                             if k in {"tmdb", "imdb"} and v],
            "ready": not virtual and any(s.get("Path") and s.get("Type") != "Placeholder" for s in sources or [])}


def _provider_ids(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
        raise JellyfinError("Playback library returned invalid provider identifiers.")
    return {name.casefold(): str(identity) for name, identity in value.items()}
