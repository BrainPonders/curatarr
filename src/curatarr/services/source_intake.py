"""Read-only source collection and durable household proposal decisions.

No provider writer or Telegram delivery lives here. A successful bounded source
snapshot is committed atomically; an unavailable source never means an empty one.
"""
from dataclasses import dataclass
from datetime import date
import calendar
import csv
import io
import json
import math
import re
import secrets

from curatarr.adapters import AdapterError
from curatarr.storage.journal import CoordinationJournal, encode, movie_key, text
from curatarr.storage.sqlite import StorageError


@dataclass(frozen=True)
class WantedSnapshot:
    keys: tuple[str, ...]
    total_count: int
    labels: tuple[tuple[str, str], ...] = ()


def imdb_csv_snapshot(content, *, expected_count, complete):
    """Explicit exported CSV fallback, never an HTML/challenge scraper.

    The operator must attest the export is complete and provide the total source
    count. All identifiers, including currently unsupported title types, enter
    the historical baseline so later metadata availability cannot awaken them.
    """
    if complete is not True or type(expected_count) is not int or not 0 <= expected_count <= 50000:
        raise ValueError('A complete export and source count are required.')
    if not isinstance(content, str) or len(content.encode()) > 20_000_000:
        raise ValueError('Invalid or oversized IMDb export.')
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')), strict=True)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or 'Const' not in reader.fieldnames:
            raise ValueError('The IMDb export must contain a unique Const column.')
        keys, labels = [], []
        for row in reader:
            identifier = row.get('Const', '')
            if None in row or not isinstance(identifier, str) or not re.fullmatch(r'tt[0-9]{5,12}', identifier):
                raise ValueError('The IMDb export contains an invalid or incomplete row.')
            keys.append('imdb:' + identifier)
            title = row.get('Title')
            if isinstance(title, str) and title.strip():
                title = ' '.join(title.split())
                if len(title) > 1024:
                    raise ValueError('An IMDb export title exceeds the display limit.')
                text(title, maximum=1024)
                labels.append(('imdb:'+identifier, title))
            if len(keys) > 50000:
                raise ValueError('The IMDb export exceeds the source limit.')
    except csv.Error:
        raise ValueError('The IMDb export is not valid CSV.') from None
    if len(keys) != expected_count or len(set(keys)) != len(keys):
        raise ValueError('The IMDb export is incomplete, duplicated or differs from its source count.')
    return WantedSnapshot(tuple(keys), expected_count, tuple(labels))


@dataclass(frozen=True)
class DiscoveryFilters:
    months: int = 18
    rating: float = 7
    votes: int = 100
    excluded_genres: tuple[int, ...] = ()
    max_pages: int = 5

    def __post_init__(self):
        if type(self.months) is not int or not 1 <= self.months <= 120:
            raise ValueError('Choose a release window between 1 and 120 months.')
        if type(self.rating) not in (int, float) or not math.isfinite(self.rating) or not 0 <= self.rating <= 10:
            raise ValueError('Choose a rating between 0 and 10.')
        if type(self.votes) is not int or not 0 <= self.votes <= 1_000_000:
            raise ValueError('Invalid minimum vote count.')
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 50:
            raise ValueError('Choose a source depth between 1 and 50 pages.')
        if not isinstance(self.excluded_genres, tuple) or len(set(self.excluded_genres)) != len(self.excluded_genres) or any(type(g) is not int or g <= 0 for g in self.excluded_genres):
            raise ValueError('Genre exclusions must be unique native genre identifiers.')

    def start_date(self, today):
        month = today.year * 12 + today.month - 1 - self.months
        year, month = divmod(month, 12)
        return date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))


class TmdbDiscovery:
    def __init__(self, client):
        self.client = client

    def collect(self, filters, *, today=None):
        today = today or date.today()
        params = {'include_adult': 'false', 'include_video': 'false', 'sort_by': 'popularity.desc',
                  'primary_release_date.gte': filters.start_date(today).isoformat(),
                  'primary_release_date.lte': today.isoformat(),
                  'vote_average.gte': str(filters.rating), 'vote_count.gte': str(filters.votes)}
        # Enforce ANY excluded genre locally. Do not depend on upstream AND/OR
        # interpretation, or discard candidates whose genre metadata is missing.
        results, seen, totals = [], set(), None
        page = 1
        while True:
            data = self.client.discover_movies(dict(params, page=str(page)))
            if not isinstance(data, dict) or data.get('page') != page or type(data.get('page')) is not int:
                raise AdapterError('Discovery returned an invalid page.')
            pages, count, rows = data.get('total_pages'), data.get('total_results'), data.get('results')
            if type(pages) is not int or type(count) is not int or pages < 0 or count < 0 or not isinstance(rows, list):
                raise AdapterError('Discovery returned incomplete pagination.')
            if totals is None:
                totals = (pages, count)
            if totals != (pages, count) or (count == 0) != (pages == 0):
                raise AdapterError('Discovery changed during collection; retry the snapshot.')
            expected = min(20, max(0, count - (page - 1) * 20))
            if len(rows) != expected or pages != (count + 19) // 20:
                raise AdapterError('Discovery returned a partial page.')
            for raw in rows:
                item = self._candidate(raw)
                key = item['tmdb']
                if key in seen:
                    raise AdapterError('Discovery repeated a movie across pages.')
                seen.add(key)
                results.append(item)
            if page >= min(max(pages, 1), filters.max_pages):
                return results
            page += 1

    @staticmethod
    def _candidate(raw):
        if not isinstance(raw, dict) or type(raw.get('id')) is not int or not 0 < raw['id'] <= 2147483647:
            raise AdapterError('Discovery has no reliable movie identity.')
        title = raw.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 1024:
            raise AdapterError('Discovery has no reliable movie title.')
        genres = raw.get('genre_ids')
        if not isinstance(genres, list) or not genres or any(type(g) is not int or g <= 0 for g in genres):
            genres = None
        released = raw.get('release_date')
        try:
            released = date.fromisoformat(released).isoformat()
        except (ValueError, TypeError):
            released = None
        rating, votes = raw.get('vote_average'), raw.get('vote_count')
        if type(rating) not in (int, float) or not math.isfinite(rating) or not 0 <= rating <= 10:
            rating = None
        if type(votes) is not int or votes < 0:
            votes = None
        return {'tmdb': str(raw['id']), 'title': title, 'genres': genres, 'release_date': released,
                'rating': rating, 'votes': votes, 'adult': raw.get('adult'), 'video': raw.get('video'),
                'overview': raw.get('overview', '')[:20000] if isinstance(raw.get('overview', ''), str) else '',
                'poster_path': raw.get('poster_path') if isinstance(raw.get('poster_path'), str)
                and re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png|webp)', raw['poster_path']) else None}


class SourceIntake:
    """Short fenced poll transactions; no database transaction spans an API call."""
    def __init__(self, storage, *, clock=None):
        self.journal = CoordinationJournal(storage) if clock is None else CoordinationJournal(storage, clock)
        self.db = storage.connection

    def collect_discovery(self, source_id, feed, filters, known_reader, *, today=None):
        """Fetch all configured pages and native suppressions before publishing."""
        claim = self.begin(source_id)
        if claim is None:
            return False
        try:
            candidates = feed.collect(filters, today=today)
            known = known_reader()
            self.discovery(claim, candidates, filters, known_keys=known, today=today)
        except (AdapterError, ValueError, TypeError, KeyError):
            self.failed(claim)
            return False
        return True

    def health(self, source_id):
        source = self.journal.get_source(source_id)
        row = self.db.execute('SELECT * FROM intake_poll WHERE source_id=?', (source_id,)).fetchone()
        if row is None:
            return {'status': 'not_run', 'last_success': None, 'item_count': None}
        running = row['token'] is not None and (row['lease_until'] or 0) > self.journal.now()
        stale = row['token'] is not None and not running
        configured = row['source_revision'] == source['revision']
        status = ('settings_changed' if not configured else 'collecting' if running else 'incomplete' if stale or row['last_error'] else
                  'completed' if row['last_success'] is not None else 'not_run')
        return {'status': status, 'last_success': row['last_success'], 'item_count': row['item_count']}

    def begin(self, source_id):
        with self.journal.transaction():
            source = self.journal.get_source(source_id)
            previous = self.db.execute('SELECT * FROM intake_poll WHERE source_id=?', (source_id,)).fetchone()
            now = self.journal.now()
            if previous and (previous['lease_until'] or 0) > now:
                return None
            token = secrets.token_hex(16)
            self.db.execute('INSERT INTO intake_poll(source_id,token,lease_until,source_revision,updated_at) VALUES(?,?,?,?,?) '
                            'ON CONFLICT(source_id) DO UPDATE SET token=excluded.token,lease_until=excluded.lease_until,'
                            'source_revision=excluded.source_revision,updated_at=excluded.updated_at',
                            (source_id, token, now + 600, source['revision'], now))
            return (source_id, token, source['revision'])

    def _owner(self, claim):
        source_id, token, revision = claim
        row = self.db.execute('SELECT * FROM intake_poll WHERE source_id=?', (source_id,)).fetchone()
        source = self.journal.get_source(source_id)
        if not row or row['token'] != token or (row['lease_until'] or 0) <= self.journal.now() or source['revision'] != revision:
            raise StorageError('The source poll expired or its configuration changed.')
        return source

    def failed(self, claim):
        with self.journal.transaction():
            self._owner(claim)
            self.db.execute("UPDATE intake_poll SET token=NULL,lease_until=NULL,last_error='source_unavailable',updated_at=? WHERE source_id=?",
                            (self.journal.now(), claim[0]))

    def wanted(self, claim, snapshot):
        """Baseline or incremental complete snapshot in the same fenced commit."""
        if not isinstance(snapshot, WantedSnapshot):
            raise ValueError('A verified complete wanted snapshot is required.')
        keys = self.journal._wanted_keys(snapshot.keys)
        if type(snapshot.total_count) is not int or len(keys) != snapshot.total_count:
            raise ValueError('Wanted snapshot count changed.')
        labels = dict(snapshot.labels)
        if len(labels) != len(snapshot.labels) or not set(labels) <= set(keys):
            raise ValueError('Wanted snapshot labels do not match its identities.')
        for title in labels.values():
            text(title, maximum=1024)
        with self.journal.transaction():
            source = self._owner(claim)
            if source['kind'] != 'wanted_list':
                raise ValueError('This is not a trusted wanted source.')
            import hashlib
            now, historical = self.journal.now(), source['baseline_hash'] is None
            for key in keys:
                self.db.execute('INSERT INTO wanted_request(source_id,item_key,state,first_seen_at) VALUES(?,?,?,?) '
                                'ON CONFLICT(source_id,item_key) DO NOTHING',
                                (claim[0], key, 'historical' if historical else 'new', now))
                if key in labels:
                    self.db.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES(?,'wanted_label','',?) "
                                    'ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP',
                                    (claim[0]+':'+key, encode({'title':labels[key]})))
            if historical:
                digest = hashlib.sha256(encode(sorted(keys)).encode()).hexdigest()
                self.db.execute('UPDATE coordination_source SET baseline_hash=?,baseline_at=? WHERE id=?', (digest, now, claim[0]))
            self._success(claim, len(keys))

    def discovery(self, claim, candidates, filters, *, known_keys, today=None):
        """Publish only after complete collection AND complete native known-state reads."""
        today = today or date.today()
        known = set(known_keys)  # caller must fail closed on incomplete provider reads
        for key in known:
            if not movie_key(key).startswith('tmdb:'):
                raise ValueError('Discovery suppression requires canonical movie identities.')
        items, seen = [], set()
        if not isinstance(candidates, list) or len(candidates) > filters.max_pages * 20:
            raise ValueError('Invalid bounded discovery snapshot.')
        for candidate in candidates:
            candidate = self._validate_candidate(candidate)
            key = 'tmdb:' + candidate['tmdb']
            if key in seen:
                raise ValueError('Discovery snapshot contains duplicate identities.')
            seen.add(key)
            eligibility = self._eligibility(candidate, filters, today)
            if key in known:
                eligibility = 'known'
            items.append((key, encode(candidate), eligibility))
        with self.journal.transaction():
            source = self._owner(claim)
            if source['kind'] != 'discovery':
                raise ValueError('This is not a discovery source.')
            now = self.journal.now()
            # A proposal may have fallen out of today's feed. Re-evaluate the
            # retained backlog too: acquiring it elsewhere or changing filters
            # must not leave an obsolete offer visible indefinitely.
            for row in self.db.execute('SELECT movie_key,metadata_json,eligibility,decision FROM discovery_candidate').fetchall():
                key = row['movie_key']
                eligibility = 'known' if key in known else 'eligible' if row['decision']=='selected' else self._eligibility(json.loads(row['metadata_json']), filters, today)
                if eligibility != row['eligibility']:
                    self.db.execute('UPDATE discovery_candidate SET eligibility=?,revision=revision+1,updated_at=? WHERE movie_key=?',
                                    (eligibility, now, key))
            for key, metadata, eligibility in items:
                self.db.execute('INSERT INTO discovery_candidate(movie_key,metadata_json,eligibility,first_seen_at,updated_at) VALUES(?,?,?,?,?) '
                                "ON CONFLICT(movie_key) DO UPDATE SET metadata_json=excluded.metadata_json,eligibility=CASE WHEN discovery_candidate.decision='selected' AND excluded.eligibility!='known' THEN 'eligible' ELSE excluded.eligibility END,"
                                'revision=discovery_candidate.revision+1,updated_at=excluded.updated_at '
                                "WHERE discovery_candidate.metadata_json!=excluded.metadata_json OR discovery_candidate.eligibility!=CASE WHEN discovery_candidate.decision='selected' AND excluded.eligibility!='known' THEN 'eligible' ELSE excluded.eligibility END",
                                (key, metadata, eligibility, now, now))
                self.db.execute('INSERT INTO discovery_provenance(movie_key,source_id,first_seen_at,last_seen_at) VALUES(?,?,?,?) '
                                'ON CONFLICT(movie_key,source_id) DO UPDATE SET last_seen_at=excluded.last_seen_at', (key, claim[0], now, now))
            self._success(claim, len(items))

    def _success(self, claim, count):
        self.db.execute('UPDATE intake_poll SET token=NULL,lease_until=NULL,last_success=?,last_error=NULL,item_count=?,updated_at=? WHERE source_id=?',
                        (self.journal.now(), count, self.journal.now(), claim[0]))

    @staticmethod
    def _validate_candidate(value):
        if not isinstance(value, dict) or not re.fullmatch(r'[1-9][0-9]{0,9}', str(value.get('tmdb', ''))):
            raise ValueError('Discovery requires a canonical movie identity.')
        text(value.get('title'), maximum=1024)
        # Use the same normalization for provider and queue boundary; no caller
        # supplied URL, profile, adult or genre bypass can initiate a write.
        return TmdbDiscovery._candidate(dict(value, id=int(value['tmdb']), genre_ids=value.get('genres'),
                                             vote_average=value.get('rating'), vote_count=value.get('votes')))

    @staticmethod
    def _eligibility(candidate, filters, today):
        if (candidate['genres'] is None or candidate['release_date'] is None or candidate['rating'] is None
                or candidate['votes'] is None or type(candidate['adult']) is not bool or type(candidate['video']) is not bool):
            return 'deferred'
        if (candidate['adult'] or candidate['video'] or set(candidate['genres']) & set(filters.excluded_genres)
                or not filters.start_date(today).isoformat() <= candidate['release_date'] <= today.isoformat()
                or candidate['rating'] < filters.rating or candidate['votes'] < filters.votes):
            return 'filtered'
        return 'eligible'

    def proposals(self, *, after='', limit=10, view='pending'):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('Review up to ten proposals per page.')
        if after:
            movie_key(after)
        if view not in {'pending', 'declined'}:
            raise ValueError('Invalid suggestion view.')
        rows = self.db.execute("SELECT * FROM discovery_candidate d WHERE "
                               + ("decision IN ('pending','selected') AND eligibility='eligible' " if view=='pending' else "decision='declined' ")
                               + "AND movie_key>? AND NOT EXISTS (SELECT 1 FROM coordination_operation o WHERE "
                               "o.request_key='discovery:'||d.movie_key||':'||d.revision AND o.status!='cancelled') ORDER BY movie_key LIMIT ?",
                               (after, limit + 1)).fetchall()
        return {'items': [self._public(r) for r in rows[:limit]], 'continue': rows[limit - 1]['movie_key'] if len(rows) > limit else None}

    @staticmethod
    def _public(row):
        return {'movie_key': row['movie_key'], 'candidate': json.loads(row['metadata_json']), 'revision': row['revision'], 'decision': row['decision']}

    def decide(self, key, *, revision, actor, accept):
        movie_key(key); text(actor)
        if type(revision) is not int or type(accept) is not bool:
            raise ValueError('Invalid proposal decision.')
        with self.journal.transaction():
            row = self.db.execute('SELECT * FROM discovery_candidate WHERE movie_key=?', (key,)).fetchone()
            if (row is None or row['revision'] != revision or row['decision'] != 'pending' or row['eligibility'] != 'eligible'):
                raise StorageError('This proposal changed or was already answered.')
            # Selected means only that the household wants to choose an action;
            # it is not acquisition authorization. W03 still owns confirmation.
            self.db.execute('UPDATE discovery_candidate SET decision=?,decided_by=?,revision=revision+1,updated_at=? WHERE movie_key=?',
                            ('selected' if accept else 'declined', actor, self.journal.now(), key))
            self.db.execute('INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)',
                            (actor, 'discovery_selected' if accept else 'discovery_declined', key, encode({'revision': revision})))

    def reopen(self, key, *, revision, actor):
        movie_key(key); text(actor)
        if type(revision) is not int:
            raise ValueError('Invalid proposal revision.')
        with self.journal.transaction():
            row = self.db.execute('SELECT * FROM discovery_candidate WHERE movie_key=?', (key,)).fetchone()
            if row is None or row['revision'] != revision or row['decision'] == 'pending':
                raise StorageError('This proposal changed or is already open.')
            self.db.execute("UPDATE discovery_candidate SET decision='pending',decided_by=?,revision=revision+1,updated_at=? WHERE movie_key=?",
                            (actor, self.journal.now(), key))
            self.db.execute('INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)',
                            (actor, 'discovery_reopened', key, encode({'revision': revision})))
