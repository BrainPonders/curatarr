"""Read-only movie reconciliation; all corrective actions remain suggestions."""

from dataclasses import dataclass

from curatarr.adapters import AdapterError, RadarrAdapter, RyotAdapter
from curatarr.domain import ArrRuntimeState, DurableState, MediaItem, MediaType
from curatarr.storage import Storage


RULES = {
    "missing_unmonitored": (
        "Movies registered in Radarr are classified as wanted. Radarr reports no downloaded file and monitoring is disabled. "
        "A wanted movie without a file must be monitored under Curatarr's rules; Radarr itself permits this combination.",
        ["Enable monitoring to retain acquisition intent", "Cancel / Archive if no longer wanted", "Skip for later review"]),
    "archived_active": (
        "Ryot marks this movie Archived, but its Radarr record still exists. Archived intent takes precedence.",
        ["Review incomplete archive: deletion of remaining managed files and removal from Radarr require confirmation",
         "Explicitly reactivate if wanted again", "Skip for later review"]),
    "ryot_match_missing": (
        "The Radarr record has no exact external-ID match in the completed Ryot movie snapshot. This is not proof the title is unknown.",
        ["Verify identity, then record Library and active Radarr membership if appropriate", "Skip for later review"]),
    "library_missing": (
        "A matching Ryot record exists but lacks explicit Library membership. Radarr presence indicates acquisition or retention intent.",
        ["Review catalogue membership", "Skip for later review"]),
    "radarr_membership_missing": (
        "The movie exists in Radarr but the matching Ryot record lacks active Radarr membership.",
        ["Review active Radarr membership", "Skip for later review"]),
    "previously_acquired_missing": (
        "Radarr reports a downloaded movie, but Ryot lacks Previously acquired membership. Radarr's imported-file record is sufficient acquisition evidence; this is not a filesystem availability check.",
        ["Add Previously acquired in Ryot using Radarr's imported-file record", "Skip for later review"]),
    "radarr_record_missing": (
        "Ryot records active Radarr intent, but the completed Radarr snapshot has no exact external-ID match. Do not infer archival or deletion.",
        ["Verify identity and acquisition intent before restoring the record or archiving", "Skip for later review"]),
    "identity_review": (
        "Identity evidence is missing, conflicting, duplicated, or only title-based. Collection corrections must wait for identity review.",
        ["Review typed identifiers and confirm the correct match", "Skip for later review"]),
}


def _external(item: MediaItem) -> set:
    return {identity for identity in item.identities if identity.namespace in {"tmdb", "imdb"}}


def _key(item: MediaItem, provider: str) -> str:
    identity = next((i for i in sorted(item.identities) if i.namespace == provider), None)
    if identity is None:
        raise AdapterError("Snapshot record is missing its provider identity.")
    return str(identity)


def _conflict(left: MediaItem, right: MediaItem) -> bool:
    for namespace in ("tmdb", "imdb"):
        a = {i.value for i in left.identities if i.namespace == namespace}
        b = {i.value for i in right.identities if i.namespace == namespace}
        if a and b and a != b:
            return True
    return False


def movie_findings(ryot: tuple[MediaItem, ...], radarr: tuple[MediaItem, ...]) -> list[dict]:
    """Compare exact typed IDs only; ambiguous matches never authorize corrections."""
    findings = []
    index = {}
    titles = {}
    for position, item in enumerate(ryot):
        for identity in _external(item):
            index.setdefault(identity, set()).add(position)
        titles.setdefault(" ".join(item.title.casefold().split()), set()).add(position)
    arr_index = {}
    for position, item in enumerate(radarr):
        for identity in _external(item):
            arr_index.setdefault(identity, set()).add(position)
    considered = set()

    def add(kind, item, history=None, candidates=(), reasons=(), comparison=()):
        message, actions = RULES[kind]
        findings.append({"media_key": _key(item, "radarr" if item.arr_runtime_state.arr_active else "ryot"),
                         "kind": kind, "context": {
                             "title": item.title, "year": item.year,
                             "identities": [str(i) for i in sorted(item.identities)],
                             "ryot_states": sorted(s.value for s in (history or item).durable_states),
                             "history_identities": [str(i) for i in sorted(history.identities)] if history else [],
                             "radarr_state": item.arr_runtime_state.value,
                             "explanation": message, "suggested_actions": actions,
                             "candidate_keys": sorted(candidates), "actions_enabled": False}})
        if kind == "identity_review":
            findings[-1]["context"].update(identity_reasons=list(reasons),
                                           identity_candidates=list(comparison))

    for item in radarr:
        external = _external(item)
        matches = set().union(*(index.get(identity, set()) for identity in external))
        possible = matches or titles.get(" ".join(item.title.casefold().split()), set())
        considered.update(possible)
        duplicate = any(len(arr_index[identity]) > 1 for identity in external)
        history = ryot[next(iter(matches))] if len(matches) == 1 else None
        if not external or duplicate or len(matches) > 1 or (history and _conflict(item, history)) or (not matches and possible):
            reasons = []
            if not external:
                reasons.append("missing_external_ids")
            if duplicate:
                reasons.append("duplicate_radarr_ids")
            if len(matches) > 1:
                reasons.append("multiple_ryot_matches")
            if history and _conflict(item, history):
                reasons.append("conflicting_external_ids")
            if not matches and possible:
                reasons.append("title_only")
            add("identity_review", item, candidates=[_key(ryot[p], "ryot") for p in sorted(possible)],
                reasons=reasons, comparison=[{"title": ryot[p].title, "year": ryot[p].year,
                    "identities": [str(i) for i in sorted(ryot[p].identities)]} for p in sorted(possible)])
            continue
        if history is not None and history.is_archived:
            add("archived_active", item, history)
            continue
        if history is None:
            add("ryot_match_missing", item)
            continue
        if item.arr_runtime_state is ArrRuntimeState.ACTIVE_MISSING_UNMONITORED:
            add("missing_unmonitored", item, history)
        for state, kind in ((DurableState.LIBRARY, "library_missing"),
                            (DurableState.RADARR, "radarr_membership_missing")):
            if state not in history.durable_states:
                add(kind, item, history)
        if item.file_present and DurableState.PREVIOUSLY_ACQUIRED not in history.durable_states:
            add("previously_acquired_missing", item, history)
    for position, item in enumerate(ryot):
        if position not in considered and DurableState.RADARR in item.durable_states:
            add("radarr_record_missing" if _external(item) else "identity_review", item)
    return sorted(findings, key=lambda finding: (finding["media_key"], finding["kind"]))


@dataclass(frozen=True)
class MovieReconciliationService:
    ryot: RyotAdapter
    radarr: RadarrAdapter
    storage: Storage

    def scan(self) -> dict:
        """Fresh provider snapshots on every call; no service mutations or filesystem claims."""
        run_id = self.storage.start_reconciliation()
        snapshots = {}
        errors = []
        for name, adapter in (("ryot", self.ryot), ("radarr", self.radarr)):
            try:
                items = tuple(adapter.scan_movies().value)
                if any(item.media_type is not MediaType.MOVIE
                       or name == "radarr" and not item.arr_runtime_state.arr_active for item in items):
                    raise AdapterError("Snapshot has invalid movie records.")
                keys = [_key(item, name) for item in items]
                if len(set(keys)) != len(keys):
                    raise AdapterError("Duplicate provider identifiers.")
                snapshots[name] = items
            except AdapterError:
                # Do not persist credentials, URLs, paths or raw provider exceptions.
                errors.append(name)
        counts = {name + "_movies": len(items) for name, items in snapshots.items()}
        findings = [] if errors else movie_findings(snapshots["ryot"], snapshots["radarr"])
        return self.storage.publish_reconciliation(run_id, findings, errors, counts)
