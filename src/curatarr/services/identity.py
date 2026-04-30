"""Media identity matching service."""

from __future__ import annotations

from dataclasses import dataclass, field

from curatarr.adapters import MetadataAdapter
from curatarr.domain import MatchConfidence, MediaIdentity, MediaItem
from curatarr.storage import Storage


@dataclass(frozen=True)
class IdentityMatchResult:
    """Result of comparing two media records."""

    confidence: MatchConfidence | None
    reason: str
    matched_identities: frozenset[MediaIdentity] = field(default_factory=frozenset)
    blocked: bool = False

    @property
    def is_match(self) -> bool:
        return self.confidence is not None and not self.blocked


class IdentityMatcher:
    """Resolve match confidence using exact IDs, local mappings, metadata, and title/year."""

    def __init__(
        self,
        *,
        storage: Storage | None = None,
        metadata_adapter: MetadataAdapter | None = None,
    ) -> None:
        self.storage = storage
        self.metadata_adapter = metadata_adapter

    def compare(self, left: MediaItem, right: MediaItem) -> IdentityMatchResult:
        exact = left.identities & right.identities
        if exact:
            return IdentityMatchResult(
                confidence=MatchConfidence.STRONG,
                reason="exact shared typed identity",
                matched_identities=frozenset(exact),
            )

        stored = self._stored_mapping(left, right)
        if stored is not None:
            if stored.is_same:
                return IdentityMatchResult(
                    confidence=MatchConfidence.STRONG,
                    reason="confirmed local identity mapping",
                    matched_identities=frozenset({stored.left, stored.right}),
                )
            return IdentityMatchResult(
                confidence=None,
                reason="confirmed negative local identity mapping",
                matched_identities=frozenset({stored.left, stored.right}),
                blocked=True,
            )

        metadata_match = self._metadata_cross_resolution(left, right)
        if metadata_match:
            return IdentityMatchResult(
                confidence=MatchConfidence.MEDIUM,
                reason="trusted metadata cross-resolution",
                matched_identities=metadata_match,
            )

        if _same_title(left.title, right.title):
            if left.year is not None and right.year is not None and left.year != right.year:
                return IdentityMatchResult(
                    confidence=None,
                    reason="title matched but year differed",
                )
            return IdentityMatchResult(
                confidence=MatchConfidence.WEAK,
                reason="title/year candidate match",
            )

        return IdentityMatchResult(confidence=None, reason="no matching evidence")

    def _stored_mapping(self, left: MediaItem, right: MediaItem):
        if self.storage is None:
            return None
        for left_identity in left.identities:
            for right_identity in right.identities:
                mapping = self.storage.get_identity_mapping(left_identity, right_identity)
                if mapping is not None:
                    return mapping
        return None

    def _metadata_cross_resolution(
        self,
        left: MediaItem,
        right: MediaItem,
    ) -> frozenset[MediaIdentity]:
        if self.metadata_adapter is None:
            return frozenset()
        left_resolved = set(left.identities)
        right_resolved = set(right.identities)
        for identity in left.identities:
            left_resolved.update(self.metadata_adapter.resolve_identities(identity).value)
        for identity in right.identities:
            right_resolved.update(self.metadata_adapter.resolve_identities(identity).value)
        return frozenset(left_resolved & right_resolved)


def _same_title(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())
