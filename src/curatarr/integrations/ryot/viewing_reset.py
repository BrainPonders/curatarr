"""Exact-account movie history reset evidence and narrowly scoped native writes."""

from copy import deepcopy
import hashlib
import json

from curatarr.integrations.ryot.adapter import METADATA_FIELDS, _identities_from_raw
from curatarr.integrations.ryot.client import RyotError
from curatarr.integrations.ryot.viewing import HISTORY, REVIEWS, RyotViewingAdapter


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def normalized(name):
    return name.casefold().replace(" ", "").replace("_", "")


class RyotViewingReset:
    def __init__(self, reader):
        self.reader = reader

    def snapshot(self, context):
        account = RyotViewingAdapter(self.reader).account()
        if account["source"] != context["source"] or account["id"] != context["account_id"]:
            raise RyotError("Viewing reset account changed.")
        metadata_id = context["media_key"].removeprefix("ryot:")
        if context["media_key"] != "ryot:" + metadata_id:
            raise RyotError("An exact Ryot movie is required.")
        data = self.reader.client.execute(f"""query CuratarrViewingResetSnapshot($id:String!) {{
          metadataDetails(metadataId:$id) {{ response {{ {METADATA_FIELDS} }} }}
          userMetadataDetails(metadataId:$id) {{ response {{ history {{ {HISTORY} }} reviews {{ {REVIEWS} }}
            collections {{ details {{ collectionId collectionName rank information }} }} }} }}
          userCollectionsList {{ response {{ id name isDefault creator {{ id }} }} }}
        }}""", {"id": metadata_id})
        try:
            movie = data["metadataDetails"]["response"]
            user = data["userMetadataDetails"]["response"]
            history, reviews = user["history"], user["reviews"]
            expected = {i for i in context["identities"] if not i.startswith("ryot:")}
            actual = {str(i) for i in _identities_from_raw(movie) if i.namespace != "ryot"}
            if (movie["id"] != metadata_id or movie["lot"] != "MOVIE" or not expected or expected != actual
                    or not isinstance(history, list) or not isinstance(reviews, list)):
                raise ValueError
            for event in history:
                RyotViewingAdapter._validate_event(event, metadata_id, account["id"])
            if len({e["id"] for e in history}) != len(history):
                raise ValueError
            if any(not isinstance(r.get("id"), str) or not isinstance(r.get("postedBy"), dict) for r in reviews):
                raise ValueError
            if len({r["id"] for r in reviews}) != len(reviews):
                raise ValueError
            collections = {c["details"]["collectionName"]: c["details"] for c in user["collections"]}
            if len(collections) != len(user["collections"]):
                raise ValueError
            targets = {}
            for name in ("completed", "inprogress", "monitoring"):
                matches = [c for c in data["userCollectionsList"]["response"]
                           if normalized(c["name"]) == name]
                if (len(matches) != 1 or matches[0]["isDefault"] is not True
                        or matches[0]["creator"]["id"] != account["id"]):
                    raise ValueError
                targets[name] = {"id": matches[0]["id"], "name": matches[0]["name"]}
            for name, member in collections.items():
                if normalized(name) in targets and member["collectionId"] != targets[normalized(name)]["id"]:
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RyotError("Complete movie/history/collection evidence could not be verified.") from None
        return {"account_id": account["id"], "source": account["source"], "media_key": context["media_key"],
                "title": movie["title"], "identities": sorted(actual), "targets": targets,
                "history": sorted(deepcopy(history), key=lambda e: e["id"]),
                "reviews": sorted(deepcopy(reviews), key=lambda r: r["id"]), "collections": collections}

    @staticmethod
    def blocked_reason(snapshot):
        # Ryot's deleteSeenItem removes Watchlist/Monitoring as a side effect.
        # Do not silently change those intent memberships or invent restoration data.
        if snapshot["history"] and any(normalized(n) in {"watchlist", "monitoring"} for n in snapshot["collections"]):
            return "Watchlist or Monitoring membership needs manual review before deleting history."
        return ""

    @staticmethod
    def check_remaining(original, fresh):
        for key in ("account_id", "source", "media_key", "identities", "targets", "reviews"):
            if original[key] != fresh[key]:
                raise RyotError("Movie, account, collections or reviews changed during the reset.")
        old = {e["id"]: e for e in original["history"]}
        if any(old.get(e["id"]) != e for e in fresh["history"]):
            raise RyotError("Viewing history changed; no newly added or edited viewing will be deleted.")
        # Only derived native flags may change during this operation.
        keep = lambda s: {n: v for n, v in s["collections"].items()
                          if normalized(n) not in {"completed", "inprogress", "monitoring"}}
        if keep(original) != keep(fresh):
            raise RyotError("Other collection memberships changed during the reset.")
        monitoring = lambda s: {n: v for n, v in s["collections"].items() if normalized(n) == "monitoring"}
        if monitoring(original) and monitoring(original) != monitoring(fresh):
            raise RyotError("Monitoring intent changed during the reset.")

    def delete(self, event_id):
        data = self.reader.client.execute("""mutation CuratarrClearViewing($id:String!) {
          deleteSeenItem(seenId:$id) { id }
        }""", {"id": event_id})
        if data.get("deleteSeenItem", {}).get("id") != event_id:
            raise RyotError("Viewing deletion was not confirmed; read-back is required.")

    def remove_flag(self, snapshot, key):
        target = snapshot["targets"][key]
        data = self.reader.client.execute("""mutation CuratarrClearViewingFlag($input:ChangeCollectionToEntitiesInput!) {
          deployRemoveEntitiesFromCollectionJob(input:$input)
        }""", {"input": {"creatorUserId": snapshot["account_id"], "collectionName": target["name"],
                         "entities": [{"entityId": snapshot["media_key"].split(":", 1)[1], "entityLot": "METADATA"}]}})
        if data.get("deployRemoveEntitiesFromCollectionJob") is not True:
            raise RyotError("Native viewing flag removal was not confirmed.")
