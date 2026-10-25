"""Stable completed-event evidence and narrowly scoped, private viewing ratings."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib

from curatarr.integrations.ryot.adapter import RyotReadAdapter, METADATA_FIELDS, _media_item_from_raw, _identities_from_raw
from curatarr.integrations.ryot.client import RyotError


HISTORY = "id userId metadataId state progress finishedOn reviewId providersConsumedOn"
REVIEWS = "id rating textOriginal visibility isSpoiler postedBy { id }"
USER_FIELDS = f"hasInteracted collections {{ details {{ collectionName }} }} history {{ {HISTORY} }}"
ACCOUNT = "query CuratarrViewingAccount { userDetails { ... on UserDetails { id preferences { general { reviewScale } } } } }"
SUPPORTED_REVIEW_SCALES = {"OUT_OF_HUNDRED", "OUT_OF_FIVE"}


class ViewingEventMissing(RyotError):
    """Exact account/movie history was valid but lacked the original event."""

    def __init__(self, identities):
        super().__init__("The original viewing event is no longer available.")
        self.identities = identities


class ViewingEventChanged(RyotError):
    """Exact native viewing exists but is no longer completed."""

    def __init__(self, identities):
        super().__init__("The original viewing is no longer completed.")
        self.identities = identities


def viewing_review_text(context, display_name="Household"):
    """Readable provenance with an event-specific reference, persisted before writing."""
    title = " ".join(str(context["title"]).split())[:240]
    if context.get("year"):
        title += f" ({context['year']})"
    try:
        date = datetime.fromisoformat(context["finished_on"].replace("Z", "+00:00")).date().isoformat()
    except (KeyError, TypeError, ValueError, AttributeError):
        date = "Not recorded"
    reference = hashlib.sha256(f"{context['source']}:{context['event_id']}".encode()).hexdigest()[:24]
    attribution = f"\n\nRated by {context['rated_by']['name']} through Curatarr." if context.get("rated_by") else ""
    return (f"{display_name} viewing confirmed through Curatarr.\n\nMovie: {title}\n\n"
            f"Viewing date: {date}{attribution}\n\nReference: CV-{reference}")


@dataclass(frozen=True)
class RyotViewingAdapter:
    reader: RyotReadAdapter

    def account(self):
        data = self.reader.client.execute(ACCOUNT).get("userDetails")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise RyotError("Viewing account could not be verified.")
        try:
            scale = data["preferences"]["general"]["reviewScale"]
        except (KeyError, TypeError):
            raise RyotError("Rating scale could not be verified.") from None
        return {"id": data["id"], "scale": scale,
                "source": hashlib.sha256((self.reader.client.url + "\0" + data["id"]).encode()).hexdigest()}

    def scan(self):
        account = self.account()
        events, seen_movies, seen_events = [], set(), set()
        for raw in self.reader._library_records(include_user_state=True, user_fields=USER_FIELDS):
            metadata = raw["metadata"]
            if not isinstance(metadata.get("lot"), str) or metadata["id"] in seen_movies:
                raise RyotError("Viewing catalogue snapshot is incomplete or duplicated.")
            seen_movies.add(metadata["id"])
            if metadata["lot"] != "MOVIE":
                continue
            item = _media_item_from_raw(raw, self.reader.collections)
            history = raw.get("history")
            if not isinstance(history, list):
                raise RyotError("Viewing history is unavailable.")
            for event in history:
                self._validate_event(event, metadata["id"], account["id"])
                if event["id"] in seen_events:
                    raise RyotError("Viewing snapshot contains duplicate event identifiers.")
                seen_events.add(event["id"])
                if event["state"] == "COMPLETED":
                    events.append({"event_id": event["id"], "media_key": f"ryot:{metadata['id']}",
                                   "title": item.title, "year": item.year,
                                   "identities": [str(i) for i in sorted(item.identities)],
                                   "finished_on": event.get("finishedOn")})
        return account, events

    @staticmethod
    def _validate_event(event, metadata_id, account_id):
        if (not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]
                or event.get("metadataId") != metadata_id or event.get("userId") != account_id
                or not isinstance(event.get("state"), str)):
            raise RyotError("Viewing event identity or ownership could not be verified.")

    def event(self, media_key, event_id, source):
        account = self.account()
        if account["source"] != source or not media_key.startswith("ryot:"):
            raise RyotError("Viewing source changed; review externally.")
        metadata_id = media_key.split(":", 1)[1]
        # Both records are fetched by exact ID; never use a title search for writes.
        query = f"""query CuratarrViewingEvent($id: String!) {{
          metadataDetails(metadataId: $id) {{ response {{ {METADATA_FIELDS} }} }}
          userMetadataDetails(metadataId: $id) {{ response {{ history {{ {HISTORY} }} reviews {{ {REVIEWS} }} }} }}
        }}"""
        data = self.reader.client.execute(query, {"id": metadata_id})
        try:
            metadata = data["metadataDetails"]["response"]
            user = data["userMetadataDetails"]["response"]
            history, reviews = user["history"], user["reviews"]
        except (KeyError, TypeError):
            raise RyotError("Viewing evidence is unavailable.") from None
        if (not isinstance(metadata, dict) or metadata.get("id") != metadata_id or metadata.get("lot") != "MOVIE"
                or not isinstance(history, list) or not isinstance(reviews, list)):
            raise RyotError("Viewing evidence has changed.")
        matches = [e for e in history if isinstance(e, dict) and e.get("id") == event_id]
        if not matches:
            for candidate in history:
                self._validate_event(candidate, metadata_id, account["id"])
            if len({e["id"] for e in history}) != len(history):
                raise RyotError("Viewing history contains duplicate events.")
            raise ViewingEventMissing({str(i) for i in _identities_from_raw(metadata) if i.namespace != "ryot"})
        if len(matches) != 1:
            raise RyotError("Viewing history contains duplicate events.")
        event = matches[0]
        self._validate_event(event, metadata_id, account["id"])
        if event["state"] != "COMPLETED":
            raise ViewingEventChanged({str(i) for i in _identities_from_raw(metadata) if i.namespace != "ryot"})
        if any(not isinstance(r, dict) or not isinstance(r.get("postedBy"), dict) for r in reviews):
            raise RyotError("Rating ownership evidence is incomplete.")
        account["movie_identities"] = {str(i) for i in _identities_from_raw(metadata) if i.namespace != "ryot"}
        return account, event, reviews

    def validate(self, context):
        expected = {i for i in context["identities"] if not i.startswith("ryot:")}
        try:
            evidence = self.event(context["media_key"], context["event_id"], context["source"])
        except (ViewingEventMissing, ViewingEventChanged) as missing:
            if expected and expected != missing.identities:
                raise RyotError("Movie identifiers changed; review externally before answering.") from None
            raise
        if expected and expected != evidence[0]["movie_identities"]:
            raise RyotError("Movie identifiers changed; review externally before answering.")
        return evidence

    def rate(self, context, rating, *, allow_create, remember_review):
        # Domain ratings and API read-back stay normalized to 0-100. Ryot scales
        # mutation input according to the current account preference.
        account, event, reviews = self.validate(context)
        if account["scale"] not in SUPPORTED_REVIEW_SCALES or type(rating) is not int or not 0 <= rating <= 100:
            raise RyotError("This rating scale is not supported for safe writes.")
        operation = context.get("operation") or {}
        # Old uncertain operations must still find their original exact text.
        marker = operation.get("review_text") or f"Curatarr viewing {context['source']}:{context['event_id']}"
        owned = [r for r in reviews if r["postedBy"].get("id") == account["id"]]
        if event.get("reviewId"):
            linked = [r for r in owned if r.get("id") == event["reviewId"]]
            if len(linked) == 1 and _rating(linked[0].get("rating")) == rating:
                return linked[0]["id"]
            raise RyotError("A different review is already linked; resolve it outside Curatarr.")
        candidates = [r for r in owned if r.get("textOriginal") == marker]
        if len(candidates) > 1:
            raise RyotError("Duplicate rating evidence requires external review.")
        if candidates:
            review = candidates[0]
            if _rating(review.get("rating")) != rating:
                raise RyotError("The pending rating differs; resolve it externally.")
            review_id = review.get("id")
        elif allow_create:
            input_rating = Decimal(rating) / (20 if account["scale"] == "OUT_OF_FIVE" else 1)
            result = self.reader.client.execute("""mutation CuratarrViewingRating($input: CreateOrUpdateReviewInput!) {
              createOrUpdateReview(input: $input) { id }
            }""", {"input": {"entityId": context["media_key"].split(":", 1)[1], "entityLot": "METADATA",
                              "rating": str(input_rating), "text": marker, "visibility": "PRIVATE"}})
            created = result.get("createOrUpdateReview")
            review_id = created.get("id") if isinstance(created, dict) else None
        else:
            raise RyotError("A previous rating attempt is uncertain; do not create another review.")
        if not isinstance(review_id, str) or not review_id:
            raise RyotError("Ryot did not return a review identifier.")
        remember_review(review_id)
        _, current, visible = self.validate(context)
        if current.get("reviewId") not in (None, review_id):
            raise RyotError("The viewing acquired a different review; refusing to replace it.")
        if not any(r.get("id") == review_id and r["postedBy"].get("id") == account["id"]
                   and r.get("textOriginal") == marker and r.get("visibility") == "PRIVATE"
                   and _rating(r.get("rating")) == rating for r in visible):
            raise RyotError("The new private rating could not be verified before linking.")
        result = self.reader.client.execute("""mutation CuratarrLinkViewingRating($input: UpdateSeenItemInput!) {
          updateSeenItem(input: $input)
        }""", {"input": {"seenId": context["event_id"], "reviewId": review_id}})
        if result.get("updateSeenItem") is not True:
            raise RyotError("Rating linkage was not accepted.")
        _, fresh, reviews = self.event(context["media_key"], context["event_id"], context["source"])
        if fresh.get("reviewId") != review_id or not any(
                r.get("id") == review_id and r["postedBy"].get("id") == account["id"]
                and _rating(r.get("rating")) == rating for r in reviews):
            raise RyotError("Rating linkage could not be verified.")
        return review_id


def _rating(value):
    try:
        rating = Decimal(str(value))
        return rating if rating.is_finite() else None
    except (InvalidOperation, ValueError):
        return None
