"""Read-only ecosystem checks; compact public status and private diagnostic evidence."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import secrets
import time

from curatarr.adapters import AdapterError, AdapterResult
from curatarr.domain import DurableState as State, MediaType
from curatarr.integrations.ryot.viewing import RyotViewingAdapter, ViewingEventMissing, ViewingEventChanged, SUPPORTED_REVIEW_SCALES
from curatarr.services.reconciliation import MovieReconciliationService
from curatarr.storage.viewing import ViewingStore


INTERVAL = 86400
RETRY_INTERVAL = 3600
LEASE = 1800
REPORT_VERSION = 1
MAX_PLAYBACK_VALIDATIONS = 500
MAX_PLAYBACK_VALIDATION_ROUNDS = 3


def issue(kind, key, **evidence):
    return {"kind": kind, "key": str(key), "evidence": evidence}


def external(item):
    return {str(i) for i in item.identities if i.namespace in {"tmdb", "imdb"}}


def item_key(item):
    identity = next((i for i in sorted(item.identities) if i.namespace in {"ryot", "radarr"}), None)
    if identity is None:
        raise AdapterError("Provider identity is missing.")
    return str(identity)


def identity_issues(records, provider):
    by_id = defaultdict(list)
    findings = []
    for key, identities in records:
        if not identities:
            findings.append(issue("missing_external_identity", key, provider=provider))
        namespaces = defaultdict(set)
        for identity in identities:
            namespaces[identity.split(":", 1)[0]].add(identity)
            by_id[identity].append(key)
        if any(len(values) > 1 for values in namespaces.values()):
            findings.append(issue("conflicting_record_identities", key, identities=sorted(identities), provider=provider))
    for identity, keys in by_id.items():
        if len(keys) > 1 and provider != "jellyfin":
            findings.append(issue("duplicate_external_identity", identity, records=sorted(keys), provider=provider))
    return findings


def catalogue_issues(items):
    findings = identity_issues([(item_key(i), external(i)) for i in items], "ryot")
    managed = {State.ARCHIVED, State.PREVIOUSLY_ACQUIRED, State.DELETED, State.RADARR, State.SONARR}
    for item in items:
        states, key = item.durable_states, item_key(item)
        if states & managed and State.LIBRARY not in states:
            findings.append(issue("managed_movie_outside_library", key))
        if State.ARCHIVED in states and State.RADARR in states:
            findings.append(issue("archived_active_memberships", key))
        if State.DELETED in states and State.PREVIOUSLY_ACQUIRED not in states:
            findings.append(issue("deletion_without_acquisition_record", key))
        if State.SONARR in states:
            findings.append(issue("movie_in_series_collection", key))
        if ("completed" in {n.casefold().replace(" ", "") for n in item.collection_names}
                and item.completed_watch_count == 0):
            findings.append(issue("watched_flag_without_viewing", key))
        if item.completed_watch_count is None:
            findings.append(issue("viewing_count_unverified", key))
    return findings


def playback_issues(ryot, radarr, playback):
    findings = identity_issues([(p["id"], set(p["identities"])) for p in playback], "jellyfin")
    ryot_index, arr_index = defaultdict(set), defaultdict(set)
    for number, item in enumerate(ryot):
        for identity in external(item):
            ryot_index[identity].add(number)
    for number, item in enumerate(radarr):
        for identity in external(item):
            arr_index[identity].add(number)
    received = set()
    for record in playback:
        ids = set(record["identities"])
        matches = set().union(*(ryot_index[i] for i in ids))
        arr_matches = set().union(*(arr_index[i] for i in ids))
        ambiguous = len(matches) > 1 or len(arr_matches) > 1
        for item in [ryot[n] for n in matches] + [radarr[n] for n in arr_matches]:
            for namespace in ("tmdb", "imdb"):
                a = {i for i in ids if i.startswith(namespace + ":")}
                b = {i for i in external(item) if i.startswith(namespace + ":")}
                ambiguous = ambiguous or bool(a and b and a != b)
        if ambiguous:
            findings.append(issue("playback_identity_conflict", record["id"], identities=sorted(ids)))
            continue
        if not record["ready"]:
            # Indexed placeholders are ordinary; only a managed imported copy is expected ready.
            continue
        if ids and not matches:
            findings.append(issue("playback_missing_ryot_match", record["id"], identities=sorted(ids)))
        for n in matches:
            history = ryot[n]
            if State.LIBRARY not in history.durable_states:
                findings.append(issue("playback_outside_library", item_key(history)))
            if history.is_archived:
                findings.append(issue("archived_copy_in_playback", item_key(history)))
        for n in arr_matches:
            received.add(n)
            if not radarr[n].file_present:
                findings.append(issue("playback_radarr_file_disagreement", item_key(radarr[n])))
    for n, item in enumerate(radarr):
        if item.file_present and n not in received:
            findings.append(issue("imported_copy_not_ready_in_playback", item_key(item), identities=sorted(external(item))))
    return findings


def playback_validation_ids(ryot, radarr, playback, findings):
    """Find copies behind both item-level and logical-movie findings."""
    direct = {"missing_external_identity", "conflicting_record_identities",
              "playback_identity_conflict", "playback_missing_ryot_match"}
    ids = {finding["key"] for finding in findings if finding["kind"] in direct}
    keys = {finding["key"] for finding in findings}
    identities = set().union(*(external(item) for item in (*ryot, *radarr) if item_key(item) in keys))
    ids.update(record["id"] for record in playback if identities.intersection(record["identities"]))
    return ids


@dataclass
class SnapshotReader:
    items: object

    def scan_movies(self):
        if self.items is None:
            raise AdapterError("Snapshot unavailable.")
        return AdapterResult(self.items)


class SystemMaintenance:
    def __init__(self, storage, ryot, radarr, jellyfin, *, clock=time.time):
        self.storage, self.ryot, self.radarr, self.jellyfin, self.clock = storage, ryot, radarr, jellyfin, clock

    def report(self):
        row = self.storage.connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key='ecosystem' AND source='system_maintenance'").fetchone()
        return json.loads(row[0]) if row else {"scopes": {}, "checked_epoch": None, "complete": False}

    def latest(self):
        row = self.storage.connection.execute("SELECT * FROM job_run WHERE job_name='system_maintenance' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def summary(self):
        report, latest, now = self.report(), self.latest(), self.clock()
        previous_issues = [i for scope in report["scopes"].values() for i in scope.get("issues", [])]
        running = bool(latest and latest["status"] == "running" and json.loads(latest["cursor_json"])["lease_until"] > now)
        fresh = bool(report.get("checked_epoch") is not None and now - report["checked_epoch"] < INTERVAL)
        complete = bool(report.get("version") == REPORT_VERSION and report.get("complete") and fresh
                        and (not latest or latest["status"] == "completed"))
        return {"maintenance_required": True if previous_issues else False if complete and not running else None,
                "status": "checking" if running else "completed" if complete else "not_run" if not latest else "incomplete",
                "last_checked_at": report.get("checked_at"), "last_completed_at": report.get("last_completed_at"),
                "complete": complete and not running, "read_only": True,
                "interval_hours": 24}

    def claim(self, *, force=False):
        now, connection = self.clock(), self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = self.latest()
            if latest:
                previous = json.loads(latest["cursor_json"])
                if latest["status"] == "running" and previous["lease_until"] > now:
                    return None
                interval = 30 if force else INTERVAL if latest["status"] == "completed" else RETRY_INTERVAL
                if previous.get("started_epoch", 0) + interval > now and latest["status"] != "running":
                    return None
                if latest["status"] == "running":
                    connection.execute("UPDATE job_run SET status='failed',finished_at=CURRENT_TIMESTAMP WHERE id=?", (latest["id"],))
            operation = {"claim": secrets.token_hex(16), "started_epoch": now, "lease_until": now + LEASE}
            run = connection.execute("INSERT INTO job_run(job_name,status,cursor_json) VALUES('system_maintenance','running',?)",
                                     (json.dumps(operation),)).lastrowid
        return {"run_id": run, **operation}

    def owns(self, claim):
        row = self.storage.connection.execute("SELECT status,cursor_json FROM job_run WHERE id=?", (claim["run_id"],)).fetchone()
        return bool(row and row["status"] == "running" and json.loads(row["cursor_json"])["claim"] == claim["claim"]
                    and json.loads(row["cursor_json"])["lease_until"] > self.clock())

    def tick(self):
        claim = self.claim()
        if claim:
            self.run(claim)

    def run(self, claim):
        if not self.owns(claim):
            return
        previous, scopes, snapshots = self.report(), {}, {}

        def check(name, loader):
            try:
                issues = loader()
                scopes[name] = {"complete": True, "issues": issues}
            except (AdapterError, ValueError, TypeError, KeyError, AttributeError):
                scopes[name] = {"complete": False, "issues": previous.get("scopes", {}).get(name, {}).get("issues", [])}

        def snapshot(name, adapter):
            items = tuple(adapter.scan_movies().value)
            if any(i.media_type is not MediaType.MOVIE or name == "radarr" and not i.arr_runtime_state.arr_active for i in items) or len({item_key(i) for i in items}) != len(items):
                raise AdapterError("Movie snapshot is incomplete or duplicated.")
            snapshots[name] = items
            return []

        check("ryot", lambda: snapshot("ryot", self.ryot))
        check("radarr", lambda: snapshot("radarr", self.radarr))
        if not self.owns(claim):
            return
        legacy = MovieReconciliationService(SnapshotReader(snapshots.get("ryot")), SnapshotReader(snapshots.get("radarr")), self.storage).scan()
        scopes["library_rules"] = {"complete": legacy["revalidated"], "issues": [issue(f["kind"], f["media_key"], decision_id=f["decision_id"])
                                                                                  for f in legacy["findings"]]}
        def catalogue():
            if "ryot" not in snapshots:
                raise AdapterError("Catalogue unavailable.")
            return catalogue_issues(snapshots["ryot"])
        check("catalogue_integrity", catalogue)

        def playback():
            snapshots["jellyfin"] = self.jellyfin.maintenance_snapshot()
            return []
        check("jellyfin", playback)

        def cross_service():
            if any(provider not in snapshots for provider in ("ryot", "radarr", "jellyfin")):
                raise AdapterError("Cross-service checks unavailable.")
            records = snapshots["jellyfin"]
            original_ids = {record["id"] for record in records}
            old_findings = previous.get("scopes", {}).get("playback_consistency", {}).get("issues", [])
            validated, rounds = set(), 0
            while True:
                findings = playback_issues(snapshots["ryot"], snapshots["radarr"], records)
                pending = playback_validation_ids(snapshots["ryot"], snapshots["radarr"], records,
                                                  findings + old_findings) - validated
                if not pending:
                    return findings
                if (len(validated) + len(pending) > MAX_PLAYBACK_VALIDATIONS
                        or rounds >= MAX_PLAYBACK_VALIDATION_ROUNDS):
                    raise AdapterError("Playback validation limit reached; findings remain unverified.")
                fresh = self.jellyfin.maintenance_items(sorted(pending))
                replacements = {record["id"]: record for record in fresh}
                # Presence must agree with the complete catalogue. Missing from
                # both reads confirms removal; disagreement is not a clean scan.
                if len(replacements) != len(fresh) or set(replacements) != pending & original_ids:
                    raise AdapterError("Playback catalogue changed during exact item validation.")
                records = [replacements.get(record["id"], record) for record in records]
                validated.update(pending)
                rounds += 1
        check("playback_consistency", cross_service)
        check("radarr_operations", lambda: self.radarr.client.maintenance_health())
        check("viewing_workflow", self.workflow_issues)
        check("local_operations", self.local_issues)
        complete = all(scope["complete"] for scope in scopes.values())
        now = self.clock()
        at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        report = {"version": REPORT_VERSION, "checked_epoch": now, "checked_at": at, "complete": complete,
                  "last_completed_at": at if complete else previous.get("last_completed_at"),
                  "scopes": scopes, "counts": {name: len(items) for name, items in snapshots.items()}, "read_only": True}
        connection = self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self.owns(claim):
                return
            connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('ecosystem','system_maintenance','',?) "
                               "ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP",
                               (json.dumps(report, sort_keys=True),))
            connection.execute("UPDATE job_run SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                               ("completed" if complete else "failed", claim["run_id"]))

    def workflow_issues(self):
        store, adapter = ViewingStore(self.storage), RyotViewingAdapter(self.ryot)
        binding = store.binding()
        findings = []
        if binding:
            account = adapter.account()
            if binding.get("enabled") and account["scale"] not in SUPPORTED_REVIEW_SCALES:
                findings.append(issue("viewing_rating_scale_unsupported", "binding"))
            if account["source"] != binding["source"]:
                findings.append(issue("viewing_account_changed", "binding"))
            else:
                for decision in self.storage.viewing_followups():
                    context = decision.context
                    if context.get("source") != binding["source"]:
                        findings.append(issue("viewing_binding_mismatch", decision.decision_id))
                        continue
                    if context.get('direct_candidate') and not context.get('native_linked'):
                        continue  # No native event exists until household confirmation.
                    try:
                        adapter.validate(context)
                    except ViewingEventMissing:
                        findings.append(issue("viewing_event_missing", decision.decision_id))
                    except ViewingEventChanged:
                        findings.append(issue("viewing_event_changed", decision.decision_id))
        return findings

    def local_issues(self):
        findings = []
        for row in self.storage.connection.execute(
                "SELECT id FROM coordination_operation WHERE status IN ('blocked','uncertain') "
                "OR status='running' AND coalesce(lease_until,0)<?", (self.clock(),)):
            findings.append(issue("coordination_operation_unconfirmed", row['id']))
        for row in self.storage.connection.execute(
                'SELECT source_id FROM intake_poll WHERE last_error IS NOT NULL '
                'OR token IS NOT NULL AND coalesce(lease_until,0)<?', (self.clock(),)):
            findings.append(issue('source_collection_incomplete', row['source_id']))
        for row in self.storage.connection.execute("SELECT r.source_id,r.item_key FROM wanted_resolution r JOIN wanted_request w USING(source_id,item_key) "
                                                  "WHERE w.state='new' AND r.error_code IS NOT NULL"):
            findings.append(issue('wanted_source_request_unverified', row['source_id']+':'+row['item_key']))

        for row in self.storage.connection.execute("SELECT id FROM playback_session WHERE json_extract(state_json,'$.repeat_uncertain')=1"):
            findings.append(issue('playback_repeat_uncertain', row['id']))
        for row in self.storage.connection.execute('SELECT id FROM playback_candidate WHERE error_code IS NOT NULL AND decision_id IS NULL'):
            findings.append(issue('playback_candidate_unverified', row['id']))
        for row in self.storage.connection.execute("SELECT id FROM coordination_event WHERE kind LIKE 'jellyfin_%' AND error_code IS NOT NULL"):
            findings.append(issue('playback_evidence_unverified', row['id']))
        for decision in self.storage.viewing_followups():
            context = decision.context
            if context.get("operation") and context["operation"].get("state") != "verified":
                findings.append(issue("rating_operation_unconfirmed", decision.decision_id))
            if context.get("delivery", {}).get("state") in {"uncertain", "sending", "uncertain_removal"}:
                findings.append(issue("viewing_delivery_unconfirmed", decision.decision_id))
        for row in self.storage.connection.execute("SELECT media_key,payload_json FROM state_snapshot WHERE source='viewing_reset'"):
            if json.loads(row["payload_json"]).get("state") != "completed":
                findings.append(issue("viewing_reset_pending", row["media_key"]))
        for row in self.storage.connection.execute("SELECT id,context_json FROM pending_decision WHERE decision_type='external_removal' AND status='open'"):
            context = json.loads(row['context_json'])
            if context.get('needs_review') or context.get('operation') or context.get('delivery', {}).get('state') in {'sending', 'uncertain'}:
                findings.append(issue("movie_removal_unconfirmed", row['id']))
        row = self.storage.connection.execute("SELECT status FROM job_run WHERE job_name='owned_cleanup' ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0] in {"pending", "failed"}:
            findings.append(issue("owned_cleanup_unconfirmed", "owned_cleanup"))
        for row in self.storage.connection.execute("SELECT id,issue_type FROM operational_issue WHERE status='open' AND severity IN ('warning','error') AND issue_type != 'reconciliation_provider_unavailable'"):
            findings.append(issue("operational_issue_open", row["id"], issue_type=row["issue_type"]))
        return findings
