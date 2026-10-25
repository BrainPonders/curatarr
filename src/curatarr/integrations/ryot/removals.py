"""Preserve native viewings/reviews while applying confirmed movie archive intent."""
import json

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.viewing import HISTORY, REVIEWS


class RyotRemovalWriter:
    def __init__(self, reader):
        self.reader = reader
        self.client = reader.client
        self.names = {key: getattr(reader.collections, key) for key in
                      ("library", "archived", "deleted", "previously_acquired", "radarr")}

    def account(self):
        data = self.client.execute("""query CuratarrRemovalAccount {
          userDetails { ... on UserDetails { id } }
          userCollectionsList { response { id name isDefault creator { id } } }
        }""")
        user = data["userDetails"]["id"]
        targets = {}
        for name in self.names.values():
            matches = [v for v in data["userCollectionsList"]["response"]
                       if v["name"] == name and not v["isDefault"] and v["creator"]["id"] == user]
            if len(matches) != 1:
                raise AdapterError("Archive collection ownership could not be verified.")
            targets[name] = matches[0]["id"]
        return {"id": user, "collections": targets}

    def find(self, hint):
        rows = list(self.reader._library_records())
        matches = [r for r in rows if r["lot"] == "MOVIE" and r["source"] == "TMDB" and r["identifier"] == hint["tmdb"]]
        if len(matches) != 1:
            raise AdapterError("One exact Ryot Library movie is required.")
        return self.snapshot(matches[0]["id"], hint)

    def snapshot(self, identifier, hint):
        query = """query CuratarrRemovalSnapshot($id:String!) {
          metadataDetails(metadataId:$id) { response { id title identifier lot source } }
          userMetadataDetails(metadataId:$id) { cacheId response {
            history { HISTORY } reviews { REVIEWS }
            collections { details { collectionId collectionName rank information } }
          } }
        }""".replace("HISTORY", HISTORY).replace("REVIEWS", REVIEWS)
        data = self.client.execute(query, {"id": identifier})
        cache = data["userMetadataDetails"].get("cacheId")
        if cache:
            if self.client.execute("mutation($id:UUID!){expireCacheKey(cacheId:$id)}", {"id": cache}).get("expireCacheKey") is not True:
                raise AdapterError("Archive read cache could not be revalidated.")
            data = self.client.execute(query, {"id": identifier})
        metadata, user = data["metadataDetails"]["response"], data["userMetadataDetails"]["response"]
        if (metadata["id"] != identifier or metadata["lot"] != "MOVIE" or metadata["source"] != "TMDB"
                or metadata["identifier"] != hint["tmdb"]):
            raise AdapterError("Ryot movie identity changed.")
        account = self.account()
        collections = {v["details"]["collectionName"]: v["details"] for v in user["collections"]}
        if len(collections) != len(user["collections"]) or self.names["library"] not in collections:
            raise AdapterError("Explicit Ryot Library membership is required.")
        for name, details in collections.items():
            if name in account["collections"] and account["collections"][name] != details["collectionId"]:
                raise AdapterError("Ryot collection ownership changed.")
        for field in ("history", "reviews"):
            if not isinstance(user[field], list):
                raise AdapterError("Ryot history preservation could not be verified.")
        return {"id": identifier, "metadata": metadata, "account": account, "collections": collections,
                "history": user["history"], "reviews": user["reviews"]}

    def preserved(self, before, after):
        if before["account"] != after["account"] or before["metadata"] != after["metadata"]:
            raise AdapterError("Ryot account or movie changed during removal.")
        normalize = lambda v: sorted(v, key=lambda x: json.dumps(x, sort_keys=True))
        for field in ("history", "reviews"):
            if normalize(before[field]) != normalize(after[field]):
                raise AdapterError("Movie history/reviews changed; maintainer review is required.")
        mutable = {self.names[k] for k in ("archived", "deleted", "previously_acquired", "radarr")}
        protected = lambda v: {k: d for k, d in v["collections"].items() if k not in mutable}
        if protected(before) != protected(after):
            raise AdapterError("An unrelated Ryot membership changed.")
        # Existing acquisition/archive/deletion memberships may never be erased.
        for key in ("archived", "deleted", "previously_acquired"):
            name = self.names[key]
            if name in before["collections"] and after["collections"].get(name) != before["collections"][name]:
                raise AdapterError("Existing durable history changed.")

    def change(self, snapshot, key, add):
        if key not in {"archived", "deleted", "previously_acquired", "radarr"} or (key != "radarr" and not add):
            raise ValueError("Unsupported movie retention membership change.")
        name = self.names[key]
        operation = "deployAddEntitiesToCollectionJob" if add else "deployRemoveEntitiesFromCollectionJob"
        response = self.client.execute("mutation CuratarrMovieRetention($input:ChangeCollectionToEntitiesInput!){" +
                                       operation + "(input:$input)}",
                                       {"input": {"creatorUserId": snapshot["account"]["id"], "collectionName": name,
                                                  "entities": [{"entityId": snapshot["id"], "entityLot": "METADATA"}]}})
        if response.get(operation) is not True:
            raise AdapterError("Archive job outcome is uncertain; wait for read-back.")
