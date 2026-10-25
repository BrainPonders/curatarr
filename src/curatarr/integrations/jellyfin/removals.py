"""Read-only verification of movie removal hints, never missing-file inference alone."""

import json
import time
from pathlib import PurePosixPath
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from curatarr.adapters import AdapterError
from curatarr.integrations.jellyfin import JellyfinReadAdapter, _provider_ids


def clean_path(value):
    if (not isinstance(value, str) or len(value) > 4096 or not value.startswith("/")
            or any(part in {".", ".."} for part in value.split("/")) or "\\" in value or "\x00" in value):
        raise ValueError("A valid absolute media path is required.")
    return str(PurePosixPath(value))


def removal_hint(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid webhook payload.")
    if payload.get("Event") != "ItemRemoved":
        return None
    item = payload.get("Item")
    if not isinstance(item, dict):
        raise ValueError("Missing removal item.")
    if item.get("Type") != "Movie":
        return None
    identifier = item.get("Id")
    if (not isinstance(identifier, str) or len(identifier.replace("-", "")) != 32
            or any(c not in "0123456789abcdefABCDEF-" for c in identifier)):
        raise ValueError("Invalid Jellyfin item ID.")
    providers = _provider_ids(item.get("ProviderIds"))
    tmdb, imdb = providers.get("tmdb"), providers.get("imdb")
    if not tmdb or not tmdb.isdecimal() or int(tmdb) <= 0:
        raise ValueError("Exact TMDB movie identity is required.")
    if imdb and (not imdb.startswith("tt") or not imdb[2:].isdecimal()):
        raise ValueError("Invalid IMDb identity.")
    title = item.get("Name")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("Missing movie title.")
    return {"item_id": identifier.replace("-", "").lower(), "tmdb": tmdb, "imdb": imdb,
            "title": " ".join(title.split()), "year": item.get("ProductionYear"),
            "path": clean_path(item.get("Path"))}


class JellyfinRemovalVerifier:
    def __init__(self, url, api_key, transport=None):
        self.url, self.api_key, self.transport = url, api_key, transport
        self.reader = JellyfinReadAdapter(url, api_key, transport)

    def get(self, path, params=None):
        if self.transport:
            return self.transport(path, params or {})
        request = Request(self.url.rstrip("/") + path + ("?" + urlencode(params) if params else ""),
                          headers={"X-Emby-Token": self.api_key, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise AdapterError("Jellyfin storage verification is unavailable.") from None

    def present(self, hint):
        items = self.reader._get({"Ids": hint["item_id"], "Fields": "ProviderIds", "EnableImages": "false"})
        if items["TotalRecordCount"] != len(items["Items"]) or len(items["Items"]) > 1:
            raise AdapterError("Jellyfin item presence is ambiguous.")
        return bool(items["Items"])

    def verify(self, hint):
        """Require complete catalogue, exact absence, no alternate copy and accessible storage."""
        if self.present(hint):
            return "present"
        rows, total = [], None
        deadline = time.monotonic() + 120
        while total is None or len(rows) < total:
            if time.monotonic() > deadline:
                raise AdapterError("Jellyfin removal verification timed out.")
            payload = self.reader._get({"Recursive": "true", "IncludeItemTypes": "Movie", "Limit": "500",
                                        "StartIndex": str(len(rows)), "Fields": "ProviderIds,MediaSources",
                                        "EnableImages": "false"})
            count = payload["TotalRecordCount"]
            if count > 50000 or (total is not None and count != total) or not payload["Items"] and len(rows) < count:
                raise AdapterError("Jellyfin removal catalogue is incomplete.")
            total = count
            rows.extend(payload["Items"])
            if len(rows) > total:
                raise AdapterError("Jellyfin removal catalogue changed.")
        if len({r["Id"] for r in rows}) != len(rows):
            raise AdapterError("Jellyfin removal catalogue is duplicated.")
        for row in rows:
            ids = _provider_ids(row.get("ProviderIds"))
            if row["Id"].replace("-", "").lower() == hint["item_id"] or ids.get("tmdb") == hint["tmdb"] or hint.get("imdb") and ids.get("imdb") == hint["imdb"]:
                return "present"  # Includes alternate/temporarily unready copies and upgrades.
        folders = self.get("/Library/VirtualFolders")
        if not isinstance(folders, list):
            raise AdapterError("Jellyfin library roots are unavailable.")
        roots = [clean_path(p) for folder in folders for p in folder.get("Locations", [])]
        path = PurePosixPath(hint["path"])
        matches = [root for root in roots if PurePosixPath(root) in path.parents]
        if not matches:
            raise AdapterError("Removed file is outside the configured libraries.")
        root = max(matches, key=len)
        # A readable empty mount is insufficient: prove a different indexed movie
        # on this same library root still physically exists before trusting absence.
        anchor = False
        for row in rows:
            for source in row.get("MediaSources") or []:
                candidate = source.get("Path")
                if isinstance(candidate, str) and PurePosixPath(root) in PurePosixPath(candidate).parents:
                    if self.file_exists(root, clean_path(candidate)):
                        anchor = True
                        break
            if anchor:
                break
        if not anchor:
            raise AdapterError("Storage availability could not be independently verified.")
        if self.file_exists(root, hint["path"]):
            return "present"  # Library index removal without verified file loss.
        return "absent"

    def file_exists(self, root, path):
        base = PurePosixPath(root)
        parts = PurePosixPath(path).relative_to(base).parts
        if not parts:
            raise AdapterError("A movie file path is required.")
        for index, part in enumerate(parts):
            rows = self.get("/Environment/DirectoryContents", {"path": str(base), "includeFiles": "true", "includeDirectories": "true"})
            if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("Path"), str)
                                                or r.get("Type") not in {"File", "Directory", "NetworkComputer", "NetworkShare"} for r in rows):
                raise AdapterError("Storage listing is incomplete.")
            match = [r for r in rows if PurePosixPath(r["Path"]).name == part]
            if not match:
                return False
            if len(match) != 1:
                raise AdapterError("Storage identity is ambiguous.")
            if index == len(parts) - 1:
                return match[0]["Type"] == "File"
            if match[0]["Type"] != "Directory":
                raise AdapterError("Storage directory changed.")
            base /= part
        return False
