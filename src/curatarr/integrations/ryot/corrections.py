"""Narrow, additive acquisition-history writer; no general state mutations."""

from dataclasses import dataclass

from curatarr.domain import DurableState, MediaIdentity
from curatarr.integrations.ryot.adapter import RyotReadAdapter
from curatarr.integrations.ryot.client import RyotError


@dataclass(frozen=True)
class RyotAcquisitionWriter:
    reader: RyotReadAdapter

    def record_acquisition(self, identity: MediaIdentity) -> bool:
        if identity.namespace != "ryot":
            raise RyotError("An exact Ryot identity is required.")
        client = self.reader.client
        data = client.execute("""query CuratarrAcquisitionTarget {
          userDetails { ... on UserDetails { id } }
          userCollectionsList { response { name isDefault creator { id } } }
        }""")
        user_details = data.get("userDetails")
        collection_list = data.get("userCollectionsList")
        user = user_details.get("id") if isinstance(user_details, dict) else None
        collections = collection_list.get("response") if isinstance(collection_list, dict) else None
        if not isinstance(user, str) or not user or not isinstance(collections, list):
            raise RyotError("Collection ownership could not be confirmed.")
        name = self.reader.collections.previously_acquired
        matches = [c for c in collections if isinstance(c, dict)
                   and isinstance(c.get("name"), str) and c["name"].casefold() == name.casefold()
                   and isinstance(c.get("creator"), dict) and c["creator"].get("id") == user]
        if len(matches) != 1 or matches[0].get("isDefault") is not False:
            raise RyotError("The configured custom acquisition collection is unavailable.")
        result = client.execute("""mutation CuratarrRecordAcquisition($input: ChangeCollectionToEntitiesInput!) {
          deployAddEntitiesToCollectionJob(input: $input)
        }""", {"input": {"creatorUserId": user, "collectionName": matches[0]["name"],
                          "entities": [{"entityId": identity.value, "entityLot": "METADATA"}]}})
        if result.get("deployAddEntitiesToCollectionJob") is not True:
            raise RyotError("Ryot did not accept the acquisition update.")
        # Job acceptance is not proof that membership has been applied.
        item = self.reader.get_item(identity).value
        return item is not None and DurableState.PREVIOUSLY_ACQUIRED in item.durable_states
