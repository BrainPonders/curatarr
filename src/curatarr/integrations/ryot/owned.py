"""Exact-ID, account-owned movie inventory removal; no general collection writer."""

from curatarr.integrations.ryot.client import RyotError


class RyotOwnedCleanup:
    def __init__(self, reader):
        self.reader = reader

    def target(self):
        data = self.reader.client.execute("""query CuratarrOwnedTarget {
          userDetails { ... on UserDetails { id } }
          userCollectionsList { response { id name isDefault creator { id } } }
        }""")
        try:
            account = data["userDetails"]["id"]
            matches = [c for c in data["userCollectionsList"]["response"] if c["name"] == "Owned"
                       and c["isDefault"] is True and c["creator"]["id"] == account]
            if not isinstance(account, str) or not account or len(matches) != 1 or not matches[0]["id"]:
                raise ValueError
            return {"account_id": account, "collection_id": matches[0]["id"]}
        except (KeyError, TypeError, ValueError):
            raise RyotError("The account's native Owned collection could not be verified.") from None

    def snapshot(self):
        target = self.target()
        page, total, entities, visited = 1, None, [], set()
        while page is not None:
            if type(page) is not int or page < 1 or page in visited:
                raise RyotError("Owned pagination could not be verified.")
            visited.add(page)
            data = self.reader.client.execute("""query CuratarrOwnedContents($input: CollectionContentsInput!) {
              collectionContents(input:$input) { response { results {
                items { entityId entityLot } details { totalItems nextPage }
              } } }
            }""", {"input": {"collectionId": target["collection_id"], "search": {"page": page, "take": 100},
                              "sort": {"by": "TITLE", "order": "ASC"}}})
            try:
                result = data["collectionContents"]["response"]["results"]
                count = result["details"]["totalItems"]
                if type(count) is not int or count < 0 or (total is not None and total != count):
                    raise ValueError
                total = count
                if not isinstance(result["items"], list):
                    raise ValueError
                entities.extend(result["items"])
                page = result["details"]["nextPage"]
                if page is not None and not result["items"]:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise RyotError("Owned snapshot is incomplete or changed.") from None
        try:
            keys = {(e["entityLot"], e["entityId"]) for e in entities}
            if len(keys) != len(entities) or len(entities) != total or any(
                    not isinstance(identifier, str) or not identifier or not isinstance(lot, str) for lot, identifier in keys):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise RyotError("Owned snapshot is incomplete or duplicated.") from None
        metadata_ids = [identifier for lot, identifier in keys if lot == "METADATA"]
        movies = []
        for start in range(0, len(metadata_ids), 50):
            batch = metadata_ids[start:start + 50]
            definitions = ", ".join(f"$id{i}:String!" for i in range(len(batch)))
            fields = []
            for i in range(len(batch)):
                fields.append(f"m{i}:metadataDetails(metadataId:$id{i}) {{ response {{ id title lot }} }}")
                fields.append(f"u{i}:userMetadataDetails(metadataId:$id{i}) {{ response {{ collections {{ details {{ "
                              "collectionId collectionName rank createdOn lastUpdatedOn information } } } }")
            data = self.reader.client.execute("query CuratarrOwnedMovies(" + definitions + ") { " + " ".join(fields) + " }",
                                              {f"id{i}": identifier for i, identifier in enumerate(batch)})
            try:
                for i, identifier in enumerate(batch):
                    item = data[f"m{i}"]["response"]
                    memberships = data[f"u{i}"]["response"]["collections"]
                    owned = [c["details"] for c in memberships if c["details"]["collectionId"] == target["collection_id"]]
                    if (item["id"] != identifier or not isinstance(item["lot"], str) or len(owned) != 1
                            or owned[0]["collectionName"] != "Owned"):
                        raise ValueError
                    if item["lot"] == "MOVIE":
                        movies.append({"id": identifier, "title": item["title"], "lot": "MOVIE", "membership": owned[0]})
            except (KeyError, TypeError, ValueError):
                raise RyotError("Movie identity or Owned membership changed during the scan.") from None
        return {**target, "movies": sorted(movies, key=lambda item: item["id"]),
                "owned_ids": sorted(identifier for lot, identifier in keys if lot == "METADATA")}

    def remove(self, snapshot):
        if self.target() != {key: snapshot[key] for key in ("account_id", "collection_id")}:
            raise RyotError("Owned account or collection changed.")
        movies = snapshot["movies"]
        if not movies or any(m.get("lot") != "MOVIE" for m in movies):
            raise RyotError("Only verified movie memberships may be removed.")
        result = self.reader.client.execute("""mutation CuratarrRemoveMovieOwned($input:ChangeCollectionToEntitiesInput!) {
          deployRemoveEntitiesFromCollectionJob(input:$input)
        }""", {"input": {"creatorUserId": snapshot["account_id"], "collectionName": "Owned",
                          "entities": [{"entityId": m["id"], "entityLot": "METADATA"} for m in movies]}})
        if result.get("deployRemoveEntitiesFromCollectionJob") is not True:
            raise RyotError("Owned job acceptance was not confirmed.")
