"""Durable coordination primitives: immutable intake and fenced, recoverable writes."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import time

from curatarr.storage.sqlite import StorageError


JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS coordination_source (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    account TEXT NOT NULL,
    config_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    baseline_hash TEXT,
    baseline_at REAL,
    UNIQUE(kind, identity, account)
);
CREATE TABLE IF NOT EXISTS coordination_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES coordination_source(id),
    occurrence_key TEXT NOT NULL,
    media_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    effective_at REAL,
    resolved_media_key TEXT,
    received_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'received' CHECK(status IN ('received','processing','deferred','processed')),
    lease_token TEXT,
    lease_until REAL,
    retry_at REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    operation_id TEXT REFERENCES coordination_operation(id),
    UNIQUE(source_id, occurrence_key)
);
CREATE INDEX IF NOT EXISTS coordination_events_ready ON coordination_event(status, retry_at, id);
CREATE TABLE IF NOT EXISTS coordination_operation (
    id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    media_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','blocked','uncertain','completed','cancelled')),
    generation INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until REAL,
    retry_at REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS coordination_movie_reservation ON coordination_operation(media_key)
WHERE status IN ('running','blocked','uncertain');
CREATE TABLE IF NOT EXISTS coordination_step (
    operation_id TEXT NOT NULL REFERENCES coordination_operation(id),
    name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','attempted','accepted','verified')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY(operation_id, name),
    UNIQUE(operation_id, ordinal)
);
CREATE TABLE IF NOT EXISTS wanted_request (
    source_id TEXT NOT NULL REFERENCES coordination_source(id),
    item_key TEXT NOT NULL,
    movie_key TEXT,
    state TEXT NOT NULL CHECK(state IN ('historical','new','applying','fulfilled','declined')),
    operation_id TEXT REFERENCES coordination_operation(id),
    first_seen_at REAL NOT NULL,
    PRIMARY KEY(source_id, item_key)
);
CREATE TABLE IF NOT EXISTS intake_poll (
    source_id TEXT PRIMARY KEY REFERENCES coordination_source(id),
    token TEXT,
    lease_until REAL,
    source_revision INTEGER NOT NULL,
    last_success REAL,
    last_error TEXT,
    item_count INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_candidate (
    movie_key TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    eligibility TEXT NOT NULL CHECK(eligibility IN ('eligible','deferred','filtered','known')),
    decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','selected','declined')),
    revision INTEGER NOT NULL DEFAULT 0,
    decided_by TEXT,
    first_seen_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_provenance (
    movie_key TEXT NOT NULL REFERENCES discovery_candidate(movie_key),
    source_id TEXT NOT NULL REFERENCES coordination_source(id),
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY(movie_key,source_id)
);
CREATE TABLE IF NOT EXISTS wanted_resolution (
    source_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    candidate_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('waiting','needs_input','ready','fulfilled','withdrawn')),
    error_code TEXT,
    retry_at REAL NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(source_id,item_key),
    FOREIGN KEY(source_id,item_key) REFERENCES wanted_request(source_id,item_key)
);
CREATE TABLE IF NOT EXISTS playback_session (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES coordination_source(id),
    session_key TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    media_key TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(source_id,session_key,cycle)
);
CREATE TABLE IF NOT EXISTS playback_candidate (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES coordination_source(id),
    occurrence_key TEXT NOT NULL,
    media_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('playback','manual')),
    error_code TEXT,
    payload_json TEXT NOT NULL,
    decision_id INTEGER REFERENCES pending_decision(id),
    created_at REAL NOT NULL,
    UNIQUE(source_id,occurrence_key)
);
"""


def encode(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError("Coordination data must be finite JSON.") from None
    if len(result.encode()) > 262144:
        raise ValueError("Coordination data exceeds the storage limit.")
    return result


def text(value, *, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid coordination identifier.")
    return value


def movie_key(value):
    text(value)
    if not re.fullmatch(r'(?:tmdb:[1-9][0-9]*|imdb:tt[0-9]+|ryot:[A-Za-z0-9_-]+)', value):
        raise ValueError("A typed canonical movie identifier is required.")
    return value


def stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid coordination time.")
    return value


@dataclass(frozen=True)
class Claim:
    identifier: str | int
    token: str
    generation: int = 0


class CoordinationJournal:
    """Transactions never span provider calls. Claims guard every local transition."""

    def __init__(self, storage, clock=time.time):
        self.storage, self.connection, self.clock = storage, storage.connection, clock

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise StorageError("Finish the current transaction before coordinating work.")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            yield

    def now(self):
        return stamp(self.clock())

    def source(self, kind, identity, *, account='', config=None, expected_revision=None):
        text(kind, maximum=80); text(identity, maximum=1024)
        if account:
            text(account)
        elif not isinstance(account, str):
            raise ValueError("Invalid source account.")
        if config is not None and not isinstance(config, dict):
            raise ValueError("Source configuration must be an object.")
        config_json = encode(config or {})
        identifier = hashlib.sha256(encode([kind, identity, account]).encode()).hexdigest()
        with self.transaction():
            old = self.connection.execute('SELECT * FROM coordination_source WHERE id=?', (identifier,)).fetchone()
            if old:
                if old['config_json'] != config_json:
                    if type(expected_revision) is not int or expected_revision != old['revision']:
                        raise StorageError("Source configuration changed; refresh before saving.")
                    self.connection.execute('UPDATE coordination_source SET config_json=?,revision=revision+1 WHERE id=?', (config_json, identifier))
            else:
                self.connection.execute('INSERT INTO coordination_source(id,kind,identity,account,config_json) VALUES(?,?,?,?,?)',
                                        (identifier, kind, identity, account, config_json))
        return self.get_source(identifier)

    def get_source(self, identifier):
        row = self.connection.execute('SELECT * FROM coordination_source WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise StorageError("Coordination source is unavailable.")
        return dict(row)

    def event(self, source_id, occurrence_key, media, kind, payload, *, effective_at=None):
        """A source occurrence cannot be replayed with changed identity or content."""
        text(occurrence_key); movie_key(media); text(kind, maximum=80)
        if not isinstance(payload, dict):
            raise ValueError("Source event evidence must be an object.")
        data = encode(payload)
        if effective_at is not None:
            stamp(effective_at)
        fingerprint = hashlib.sha256(encode([media, kind, payload, effective_at]).encode()).hexdigest()
        with self.transaction():
            self.get_source(source_id)
            old = self.connection.execute('SELECT * FROM coordination_event WHERE source_id=? AND occurrence_key=?',
                                          (source_id, occurrence_key)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise StorageError("A source event changed after receipt.")
                identifier = old['id']
            else:
                row = self.connection.execute('INSERT INTO coordination_event(source_id,occurrence_key,media_key,kind,payload_json,fingerprint,effective_at,received_at) VALUES(?,?,?,?,?,?,?,?)',
                                              (source_id, occurrence_key, media, kind, data, fingerprint, effective_at, self.now()))
                identifier = row.lastrowid
        return self.get_event(identifier)

    def ready_events(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid event batch size.")
        return [r[0] for r in self.connection.execute(
            "SELECT id FROM coordination_event WHERE status!='processed' AND retry_at<=? "
            "AND coalesce(lease_until,0)<=? ORDER BY id LIMIT ?", (self.now(), self.now(), limit))]

    def get_event(self, identifier):
        row = self.connection.execute('SELECT * FROM coordination_event WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise StorageError("Source event is unavailable.")
        return dict(row)

    def claim_event(self, identifier, *, lease_seconds=120):
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 1800:
            raise ValueError("Invalid event lease duration.")
        with self.transaction():
            row = self.get_event(identifier)
            now = self.now()
            if row['status'] == 'processed' or row['retry_at'] > now or (row['lease_until'] or 0) > now:
                return None
            token = secrets.token_hex(24)
            self.connection.execute("UPDATE coordination_event SET status='processing',lease_token=?,lease_until=? WHERE id=?",
                                    (token, now + lease_seconds, identifier))
        return Claim(identifier, token)

    def finish_event(self, claim, *, operation_id=None):
        with self.transaction():
            row = self.get_event(claim.identifier)
            self._event_owner(row, claim)
            if operation_id is not None and self.operation(operation_id)['media_key'] != (row['resolved_media_key'] or row['media_key']):
                raise StorageError("Event and operation belong to different movies.")
            self.connection.execute("UPDATE coordination_event SET status='processed',operation_id=?,lease_token=NULL,lease_until=NULL,error_code=NULL WHERE id=?",
                                    (operation_id, claim.identifier))

    def resolve_event(self, claim, canonical_media):
        movie_key(canonical_media)
        if not canonical_media.startswith('tmdb:'):
            raise ValueError("A resolved TMDB movie identity is required for coordination.")
        with self.transaction():
            row = self.get_event(claim.identifier); self._event_owner(row, claim)
            if row['media_key'].startswith('tmdb:') and row['media_key'] != canonical_media:
                raise StorageError("The canonical source movie cannot be retargeted.")
            if row['resolved_media_key'] not in {None, canonical_media}:
                raise StorageError("The resolved event identity cannot change.")
            self.connection.execute('UPDATE coordination_event SET resolved_media_key=? WHERE id=?', (canonical_media, claim.identifier))

    def defer_event(self, claim, error_code, *, delay=300):
        text(error_code, maximum=80); stamp(delay)
        with self.transaction():
            row = self.get_event(claim.identifier); self._event_owner(row, claim)
            self.connection.execute("UPDATE coordination_event SET status='deferred',lease_token=NULL,lease_until=NULL,retry_at=?,error_code=? WHERE id=?",
                                    (self.now() + delay, error_code, claim.identifier))

    def _event_owner(self, row, claim):
        if row['status'] != 'processing' or row['lease_token'] != claim.token or (row['lease_until'] or 0) <= self.now():
            raise StorageError("Source event claim expired or changed.")

    def baseline_wanted(self, source_id, item_keys, *, total_count, complete):
        """Immutable baseline: ALL existing personal-list entries are handled, not downloads."""
        keys = self._wanted_keys(item_keys)
        if complete is not True or type(total_count) is not int or total_count != len(keys):
            raise StorageError("A complete, count-verified wanted snapshot is required for baselining.")
        digest = hashlib.sha256(encode(sorted(keys)).encode()).hexdigest()
        with self.transaction():
            source = self.get_source(source_id)
            if source['kind'] != 'wanted_list':
                raise ValueError("Only a trusted wanted source has an acquisition baseline.")
            if source['baseline_hash'] is not None:
                if source['baseline_hash'] != digest:
                    raise StorageError("The wanted baseline already exists and cannot be replaced.")
                return len(keys)
            now = self.now()
            self.connection.executemany("INSERT INTO wanted_request(source_id,item_key,state,first_seen_at) VALUES(?,?,'historical',?)",
                                        [(source_id, key, now) for key in keys])
            self.connection.execute('UPDATE coordination_source SET baseline_hash=?,baseline_at=? WHERE id=?', (digest, now, source_id))
        return len(keys)

    def observe_wanted(self, source_id, item_keys):
        keys = self._wanted_keys(item_keys)
        with self.transaction():
            source = self.get_source(source_id)
            if source['kind'] != 'wanted_list' or source['baseline_hash'] is None:
                raise StorageError("Establish a complete historical wanted baseline before new-entry processing.")
            now, created = self.now(), []
            for key in keys:
                cursor = self.connection.execute("INSERT INTO wanted_request(source_id,item_key,state,first_seen_at) VALUES(?,?,'new',?) ON CONFLICT(source_id,item_key) DO NOTHING",
                                                 (source_id, key, now))
                if cursor.rowcount:
                    created.append(key)
        return tuple(created)

    @staticmethod
    def _wanted_keys(values):
        keys = list(values)
        if len(keys) > 50000 or len(keys) != len(set(keys)):
            raise ValueError("Wanted source snapshot is duplicated or exceeds its limit.")
        for key in keys:
            movie_key(key)
        return keys

    def create_operation(self, request_key, media, kind, authorization, expected, steps, *, origin=None):
        text(request_key); movie_key(media); text(kind, maximum=80)
        if not media.startswith("tmdb:"):
            raise ValueError("Resolve the canonical TMDB movie before creating a write operation.")
        if not isinstance(authorization, dict) or not authorization.get('actor') or not authorization.get('basis'):
            raise ValueError("An actor and explicit authorization basis are required.")
        text(authorization['actor']); text(authorization['basis'], maximum=80)
        auth, snapshot = encode(authorization), encode(expected)
        names = list(steps)
        if not 1 <= len(names) <= 32 or len(names) != len(set(names)):
            raise ValueError("An operation needs unique bounded step names.")
        for name in names:
            text(name, maximum=80)
        with self.transaction():
            old = self.connection.execute('SELECT * FROM coordination_operation WHERE request_key=?', (request_key,)).fetchone()
            if old:
                operation = self.operation(old['id'])
                if (old['media_key'], old['kind'], old['authorization_json'], old['expected_json'], [s['name'] for s in operation['steps']]) != (media, kind, auth, snapshot, names):
                    raise StorageError("This operation request changed after authorization.")
                return operation
            identifier, now = secrets.token_hex(16), self.now()
            if origin:
                if origin['kind'] == 'wanted':
                    source = self.get_source(origin['source_id'])
                    wanted = self.connection.execute('SELECT * FROM wanted_request WHERE source_id=? AND item_key=?',
                                                     (source['id'], origin['item_key'])).fetchone()
                    selected = self.connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key='sources' AND source='wanted_selection'").fetchone()
                    if (source['revision'] != origin['revision'] or authorization['actor'].startswith('source:') and not json.loads(source['config_json']).get('enabled')
                            or not selected or json.loads(selected[0])['id'] != source['id']
                            or wanted is None or wanted['state'] != 'new'):
                        raise StorageError('Wanted source or request changed before authorization.')
                elif origin['kind'] == 'discovery':
                    proposal = self.connection.execute('SELECT * FROM discovery_candidate WHERE movie_key=?', (media,)).fetchone()
                    if proposal is None or proposal['decision'] != 'selected' or proposal['revision'] != origin['revision']:
                        raise StorageError('The suggestion changed before authorization.')
                else:
                    raise ValueError('Unsupported acquisition origin.')
            self.connection.execute('INSERT INTO coordination_operation(id,request_key,media_key,kind,authorization_json,expected_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                                    (identifier, request_key, media, kind, auth, snapshot, now, now))
            self.connection.executemany('INSERT INTO coordination_step(operation_id,name,ordinal,updated_at) VALUES(?,?,?,?)',
                                        [(identifier, name, index, now) for index, name in enumerate(names)])
            if origin and origin['kind'] == 'wanted':
                self.connection.execute("UPDATE wanted_request SET state='applying',movie_key=?,operation_id=? WHERE source_id=? AND item_key=?",
                                        (media, identifier, origin['source_id'], origin['item_key']))
        return self.operation(identifier)

    def operation(self, identifier):
        row = self.connection.execute('SELECT * FROM coordination_operation WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise StorageError("Coordination operation is unavailable.")
        result = dict(row)
        result['authorization'] = json.loads(result['authorization_json'])
        result['expected'] = json.loads(result['expected_json'])
        result['steps'] = [dict(s) for s in self.connection.execute('SELECT * FROM coordination_step WHERE operation_id=? ORDER BY ordinal', (identifier,))]
        return result

    def ready_operations(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid operation batch size.")
        # Pending unrelated work must not jump ahead of an uncertain operation
        # already reserving this movie. Other movies can proceed independently.
        return [r[0] for r in self.connection.execute(
            "SELECT o.id FROM coordination_operation o WHERE o.status NOT IN ('completed','cancelled') "
            "AND o.retry_at<=? AND coalesce(o.lease_until,0)<=? AND NOT EXISTS ("
            "SELECT 1 FROM coordination_operation prior WHERE prior.media_key=o.media_key AND prior.id!=o.id "
            "AND prior.status IN ('running','blocked','uncertain')) ORDER BY o.created_at,o.id LIMIT ?",
            (self.now(), self.now(), limit))]

    def claim_operation(self, identifier, *, lease_seconds=120):
        if type(lease_seconds) is not int or not 60 <= lease_seconds <= 1800:
            raise ValueError("Invalid operation lease duration.")
        with self.transaction():
            row = self.operation(identifier)
            now = self.now()
            if row['status'] in {'completed', 'cancelled'} or row['retry_at'] > now or (row['lease_until'] or 0) > now:
                return None
            other = self.connection.execute("SELECT id FROM coordination_operation WHERE media_key=? AND id!=? AND status IN ('running','blocked','uncertain')",
                                            (row['media_key'], identifier)).fetchone()
            if other:
                return None  # Uncertain earlier effects reserve this movie even after lease expiry.
            token, generation = secrets.token_hex(24), row['generation'] + 1
            self.connection.execute("UPDATE coordination_operation SET status='running',lease_token=?,lease_until=?,generation=?,revision=revision+1,updated_at=? WHERE id=?",
                                    (token, now + lease_seconds, generation, now, identifier))
        return Claim(identifier, token, generation)

    def owned(self, claim):
        row = self.operation(claim.identifier)
        if (row['status'] != 'running' or row['lease_token'] != claim.token or row['generation'] != claim.generation
                or (row['lease_until'] or 0) <= self.now()):
            raise StorageError("Operation claim expired or was replaced.")
        return row

    def begin_step(self, claim, name):
        """Save attempted BEFORE native mutation. A retry must inspect, not resubmit."""
        with self.transaction():
            row = self.owned(claim)
            step = next((s for s in row['steps'] if s['name'] == name), None)
            if step is None:
                raise ValueError("Unknown operation step.")
            if any(s['ordinal'] < step['ordinal'] and s['status'] != 'verified' for s in row['steps']):
                raise StorageError("Earlier operation steps are not verified.")
            if step['status'] != 'planned':
                return False
            self.connection.execute("UPDATE coordination_step SET status='attempted',updated_at=? WHERE operation_id=? AND name=?", (self.now(), claim.identifier, name))
        return True

    def step_result(self, claim, name, *, verified, evidence):
        proof = encode(evidence)
        if type(verified) is not bool or not isinstance(evidence, dict) or not evidence:
            raise ValueError("A bounded outcome with verification evidence is required.")
        with self.transaction():
            row = self.owned(claim)
            step = next((s for s in row['steps'] if s['name'] == name), None)
            if step is None:
                raise ValueError("Unknown operation step.")
            if any(s['ordinal'] < step['ordinal'] and s['status'] != 'verified' for s in row['steps']):
                raise StorageError("Earlier operation steps are not verified.")
            if not verified and step['status'] != 'attempted':
                raise StorageError("Only an attempted step can be accepted.")
            self.connection.execute('UPDATE coordination_step SET status=?,evidence_json=?,updated_at=? WHERE operation_id=? AND name=?',
                                    ('verified' if verified else 'accepted', proof, self.now(), claim.identifier, name))

    def defer_operation(self, claim, error_code, *, uncertain=True, delay=300):
        text(error_code, maximum=80); stamp(delay)
        with self.transaction():
            self.owned(claim)
            self.connection.execute('UPDATE coordination_operation SET status=?,error_code=?,retry_at=?,lease_token=NULL,lease_until=NULL,revision=revision+1,updated_at=? WHERE id=?',
                                    ('uncertain' if uncertain else 'blocked', error_code, self.now() + delay, self.now(), claim.identifier))

    def finish_operation(self, claim):
        with self.transaction():
            row = self.owned(claim)
            if any(s['status'] != 'verified' for s in row['steps']):
                raise StorageError("Every operation step must be verified before completion.")
            self.connection.execute("UPDATE coordination_operation SET status='completed',lease_token=NULL,lease_until=NULL,error_code=NULL,revision=revision+1,updated_at=? WHERE id=?", (self.now(), claim.identifier))
            self.connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,'coordination_operation_verified',?,?)",
                                    (row['authorization']['actor'], row['media_key'], encode({'operation_id': row['id'], 'kind': row['kind']})))
        return self.operation(claim.identifier)

    def cancel_unstarted(self, identifier):
        with self.transaction():
            row = self.operation(identifier)
            if row['status'] in {'completed', 'cancelled'}:
                return False
            if (row['lease_until'] or 0) > self.now() or any(s['status'] != 'planned' for s in row['steps']):
                raise StorageError("An operation with possible native effects needs verified recovery.")
            self.connection.execute("UPDATE coordination_operation SET status='cancelled',lease_token=NULL,lease_until=NULL,revision=revision+1,updated_at=? WHERE id=?", (self.now(), identifier))
        return True
