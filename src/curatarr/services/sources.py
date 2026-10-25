"""Source configuration, exact metadata resolution and trusted wanted execution."""
from datetime import date
import json
import re

from curatarr.adapters import AdapterError
from curatarr.services.source_intake import DiscoveryFilters, SourceIntake, TmdbDiscovery, imdb_csv_snapshot
from curatarr.storage.journal import encode
from curatarr.storage.sqlite import StorageError


class MovieSources:
    def __init__(self, storage, metadata, acquisition, known_reader, *, clock=None):
        self.storage, self.metadata, self.acquisition, self.known_reader = storage, metadata, acquisition, known_reader
        self.intake = SourceIntake(storage, clock=clock)
        self.journal = self.intake.journal
        self.db = storage.connection

    def discovery_source(self):
        row = self.db.execute("SELECT id FROM coordination_source WHERE kind='discovery' AND identity='tmdb-discover' AND account='' ").fetchone()
        if row:
            return self.journal.get_source(row[0])
        return self.journal.source('discovery', 'tmdb-discover', config={
            'enabled': False, 'months': 18, 'rating': 7, 'votes': 100, 'excluded_genres': [], 'max_pages': 5})

    def wanted_source(self):
        row = self.db.execute("SELECT payload_json FROM state_snapshot WHERE media_key='sources' AND source='wanted_selection'").fetchone()
        return self.journal.get_source(json.loads(row[0])['id']) if row else None

    def genres(self):
        rows = self.metadata.movie_genres().get('genres')
        if (not isinstance(rows, list) or not rows or any(not isinstance(r, dict) or type(r.get('id')) is not int
                or r['id'] <= 0 or not isinstance(r.get('name'), str) or not r['name'].strip() for r in rows)
                or len({r['id'] for r in rows}) != len(rows)):
            raise AdapterError('Movie genres are unavailable.')
        return sorted([{'id': r['id'], 'name': r['name']} for r in rows], key=lambda r: r['name'])

    def save_discovery(self, values, *, revision, actor):
        source = self.discovery_source()
        previous = json.loads(source['config_json'])
        if source['revision'] != revision:
            raise StorageError('Source settings changed. Refresh before saving.')
        filters = DiscoveryFilters(months=values['months'], rating=values['rating'], votes=values['votes'],
                                   excluded_genres=tuple(values['excluded_genres']), max_pages=values['max_pages'])
        if type(values['enabled']) is not bool or (values['excluded_genres'] != previous['excluded_genres']
                and not set(filters.excluded_genres) <= {g['id'] for g in self.genres()}):
            raise ValueError('Choose valid movie genres and a collection policy.')
        updated = self.journal.source('discovery', 'tmdb-discover', config=values, expected_revision=revision)
        self.storage.append_audit_log(actor=actor, action='discovery_settings_changed', details={'source_id': source['id'], 'revision': updated['revision']})
        return updated

    def save_wanted(self, identity, values, *, revision, actor):
        if not isinstance(identity, str) or not re.fullmatch(r'ur[0-9]{1,16}', identity):
            raise ValueError('Enter the IMDb user identifier, for example ur1234567.')
        if type(values['enabled']) is not bool or values['mode'] not in {'catalogue_only', 'add_search'}:
            raise ValueError('Choose a wanted-source policy.')
        values = dict(values)
        active = self.wanted_source()
        previous = json.loads(active['config_json']) if active else {}
        disabling = (active and active['identity']=='imdb:user:'+identity and active['revision']==revision and values['enabled'] is False
                     and all(values.get(k)==previous.get(k) for k in ('mode','profile_id','root_id')))
        if values['mode'] == 'add_search':
            if disabling:
                values.update(profile_name=previous['profile_name'],root_path=previous['root_path'])
            else:
                profile, root = self.acquisition.radarr.selection(values.get('profile_id'), values.get('root_id'))
                values.update(profile_name=profile['name'], root_path=root['path'])
        elif values.get('profile_id') is not None or values.get('root_id') is not None:
            raise ValueError('Catalogue only does not use a download profile.')
        identifier = 'imdb:user:' + identity
        existing = self.db.execute("SELECT id FROM coordination_source WHERE kind='wanted_list' AND identity=? AND account=''", (identifier,)).fetchone()
        if not existing and revision != 0:
            raise StorageError('Wanted source changed. Refresh settings.')
        source = self.journal.source('wanted_list', identifier, config=values, expected_revision=revision)
        with self.journal.transaction():
            self.db.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('sources','wanted_selection','',?) "
                            'ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP', (encode({'id': source['id']}),))
            self.db.execute('INSERT INTO audit_log(actor,action,details_json) VALUES(?,?,?)',
                            (actor, 'wanted_settings_changed', encode({'source_id': source['id'], 'revision': source['revision']})))
        return source

    def public(self):
        def public_source(source):
            if source is None:
                return None
            values = json.loads(source['config_json'])
            values.pop('root_path', None)
            return {'id': source['id'], 'revision': source['revision'], 'settings': values,
                    'health': self.intake.health(source['id']), 'baseline_complete': source['baseline_hash'] is not None}
        result = {'discovery': public_source(self.discovery_source()), 'wanted': public_source(self.wanted_source()),
                  'collection_interval_hours': 24, 'notifications': 'off', 'wanted_access': 'manual_csv'}
        if result['wanted']:
            result['wanted']['imdb_user'] = self.wanted_source()['identity'].removeprefix('imdb:user:')
            result['wanted']['pending_count'] = self.db.execute("SELECT count(*) FROM wanted_request WHERE source_id=? AND state IN ('new','applying')", (result['wanted']['id'],)).fetchone()[0]
        return result

    def suggestions(self, *, after='', view='pending'):
        values = json.loads(self.discovery_source()['config_json'])
        filters = DiscoveryFilters(**{k:(tuple(v) if k=='excluded_genres' else v) for k,v in values.items() if k!='enabled'})
        with self.journal.transaction():
            for row in self.db.execute("SELECT * FROM discovery_candidate WHERE eligibility!='known' AND decision='pending'").fetchall():
                eligibility = self.intake._eligibility(json.loads(row['metadata_json']), filters, date.today())
                if eligibility != row['eligibility']:
                    self.db.execute('UPDATE discovery_candidate SET eligibility=?,revision=revision+1,updated_at=? WHERE movie_key=?',
                                    (eligibility,self.journal.now(),row['movie_key']))
        result = self.intake.proposals(after=after, view=view)
        for item in result['items']:
            item['source'] = 'TMDB Discover'
            item['source_url'] = 'https://www.themoviedb.org/movie/'+item['candidate']['tmdb']
        return {**result, 'notifications':'off', 'health':self.intake.health(self.discovery_source()['id'])}

    def import_wanted(self, source_id, *, revision, content, count, complete, actor):
        snapshot = imdb_csv_snapshot(content, expected_count=count, complete=complete)
        source = self.wanted_source()
        if source is None or source['id'] != source_id or source['revision'] != revision:
            raise StorageError('The selected wanted source changed. Refresh settings.')
        baseline = source['baseline_hash'] is None
        claim = self.intake.begin(source_id)
        if claim is None:
            raise StorageError('A source import is already running.')
        self.intake.wanted(claim, snapshot)
        self.storage.append_audit_log(actor=actor, action='wanted_snapshot_imported', details={'source_id': source_id, 'count': count, 'historical_baseline': baseline})
        return {'historical_baseline': baseline, 'count': count, 'source': self.public()['wanted']}

    def collect(self, *, force=False):
        source = self.discovery_source()
        values = json.loads(source['config_json'])
        if not values['enabled']:
            return False
        previous = self.db.execute('SELECT * FROM intake_poll WHERE source_id=?', (source['id'],)).fetchone()
        if previous:
            interval = 0 if previous['source_revision']!=source['revision'] else 86400 if previous['last_error'] is None else 3600
            if self.journal.now() < previous['updated_at'] + (30 if force else interval):
                return False
        filters = DiscoveryFilters(**{k: (tuple(v) if k=='excluded_genres' else v) for k,v in values.items() if k!='enabled'})
        return self.intake.collect_discovery(source['id'], TmdbDiscovery(self.metadata), filters, self.known_reader)

    def resolve_imdb(self, imdb):
        data = self.metadata.find_imdb(imdb)
        movies = data.get('movie_results')
        if not isinstance(movies, list) or len(movies) > 1:
            raise AdapterError('IMDb movie resolution is incomplete or ambiguous.')
        if not movies:
            return None  # TV-only or not-yet-supported metadata remains waiting.
        raw = movies[0]
        if not isinstance(raw, dict) or type(raw.get('id')) is not int or raw['id'] <= 0:
            raise AdapterError('IMDb resolution has no reliable movie identity.')
        tmdb = str(raw['id'])
        external = self.metadata.movie_external_ids(tmdb)
        if type(external.get('id')) is not int or external['id'] != raw['id'] or external.get('imdb_id') != imdb:
            raise AdapterError('IMDb and TMDB movie identifiers disagree.')
        details = self.metadata.movie_details(tmdb)
        if type(details.get('id')) is not int or details['id'] != raw['id'] or not isinstance(details.get('title'), str) or not details['title'].strip():
            raise AdapterError('Exact movie metadata is unavailable.')
        return {'tmdb': tmdb, 'imdb': imdb, 'title': details['title'], 'year': (details.get('release_date') or '')[:4] or None}

    def wanted_tick(self, *, limit=10):
        source = self.wanted_source()
        if source is None or source['baseline_hash'] is None:
            return
        policy = json.loads(source['config_json'])
        if not policy.get('enabled'):
            return
        rows = self.db.execute("SELECT w.*,r.status resolution_status,r.retry_at FROM wanted_request w LEFT JOIN wanted_resolution r USING(source_id,item_key) "
                               "WHERE w.source_id=? AND w.state IN ('new','applying') AND coalesce(r.retry_at,0)<=? "
                               "AND coalesce(r.status,'waiting')!='withdrawn' ORDER BY w.first_seen_at,w.item_key LIMIT ?",
                               (source['id'], self.journal.now(), limit)).fetchall()
        for row in rows:
            candidate = None
            try:
                if row['operation_id']:
                    operation = self.journal.operation(row['operation_id'])
                    if operation['status'] == 'completed':
                        self._resolution(row, 'fulfilled')
                        with self.journal.transaction():
                            self.db.execute("UPDATE wanted_request SET state='fulfilled' WHERE source_id=? AND item_key=? AND operation_id=?",
                                            (source['id'], row['item_key'], operation['id']))
                    continue
                if not row['item_key'].startswith('imdb:'):
                    raise ValueError('Wanted identity is unsupported.')
                candidate = self.resolve_imdb(row['item_key'][5:])
                if candidate is None:
                    self._resolution(row, 'waiting', retry=86400)
                    continue
                # A request already active is satisfied without extra search or
                # provenance stamp. Exact matching rejects external-ID conflicts.
                if self.acquisition.radarr.movie(candidate['tmdb'], candidate['imdb']) is not None:
                    catalogue = self.acquisition.catalogue.snapshot(candidate['tmdb'])
                    if (not catalogue['movie'] or self.acquisition.catalogue.reader.collections.library not in catalogue['movie']['collections']
                            or self.acquisition.catalogue.reader.collections.archived in catalogue['movie']['collections']):
                        raise AdapterError('An active movie is missing its verified catalogue membership.')
                    with self.journal.transaction():
                        self.db.execute("UPDATE wanted_request SET state='fulfilled',movie_key=? WHERE source_id=? AND item_key=? AND state='new'",
                                        ('tmdb:'+candidate['tmdb'], source['id'], row['item_key']))
                    self._resolution(row, 'fulfilled', candidate=candidate)
                    continue
                if policy['mode']=='add_search':
                    profile, root = self.acquisition.radarr.selection(policy['profile_id'], policy['root_id'])
                    if profile['name'] != policy['profile_name'] or root['path'] != policy['root_path']:
                        raise ValueError('Wanted profile or root changed.')
                origin = {'kind':'wanted', 'source_id':source['id'], 'item_key':row['item_key'], 'revision':source['revision']}
                preview = self.acquisition.preview(candidate, actor='source:'+source['id'], mode=policy['mode'], origin=origin,
                                                   profile_id=policy.get('profile_id'), root_id=policy.get('root_id'))
                self.acquisition.confirm(preview['decision_id'], actor='source:'+source['id'], version=preview['version'])
                self._resolution(row, 'ready', candidate=candidate)
            except AdapterError:
                self._resolution(row, 'needs_input', candidate=candidate, retry=3600, error='source_metadata_or_provider_unavailable')
            except (ValueError, StorageError):
                self._resolution(row, 'needs_input', candidate=candidate, retry=3600)

    def _resolution(self, row, status, *, candidate=None, retry=0, error=None):
        with self.journal.transaction():
            self.db.execute('INSERT INTO wanted_resolution(source_id,item_key,candidate_json,status,error_code,retry_at) VALUES(?,?,?,?,?,?) '
                            'ON CONFLICT(source_id,item_key) DO UPDATE SET candidate_json=coalesce(excluded.candidate_json,wanted_resolution.candidate_json),'
                            'status=excluded.status,error_code=excluded.error_code,retry_at=excluded.retry_at,revision=wanted_resolution.revision+1',
                            (row['source_id'], row['item_key'], encode(candidate) if candidate else None, status, error, self.journal.now()+retry))

    def requests(self):
        rows = self.db.execute("SELECT w.source_id,w.item_key,w.state,r.status,r.revision,r.candidate_json,label.payload_json label_json FROM wanted_request w "
                               "JOIN wanted_resolution r USING(source_id,item_key) LEFT JOIN state_snapshot label "
                               "ON label.source='wanted_label' AND label.media_key=w.source_id||':'||w.item_key "
                               "WHERE w.state='new' AND r.status IN ('waiting','needs_input') ORDER BY w.first_seen_at LIMIT 100").fetchall()
        return [{'source_id':r['source_id'], 'item_key':r['item_key'], 'status':r['status'], 'revision':r['revision'],
                 'title':json.loads(r['candidate_json'])['title'] if r['candidate_json'] else json.loads(r['label_json'])['title'] if r['label_json'] else r['item_key'],
                 'candidate':json.loads(r['candidate_json']) if r['candidate_json'] else None} for r in rows]

    def withdraw(self, source_id, item_key, *, revision, actor):
        with self.journal.transaction():
            row = self.db.execute('SELECT w.state,r.revision FROM wanted_request w JOIN wanted_resolution r USING(source_id,item_key) WHERE source_id=? AND item_key=?',
                                  (source_id,item_key)).fetchone()
            if row is None or row['state']!='new' or row['revision']!=revision:
                raise StorageError('This request changed or is already executing.')
            self.db.execute("UPDATE wanted_request SET state='declined' WHERE source_id=? AND item_key=?", (source_id,item_key))
            self.db.execute("UPDATE wanted_resolution SET status='withdrawn',revision=revision+1 WHERE source_id=? AND item_key=?", (source_id,item_key))
            self.db.execute('INSERT INTO audit_log(actor,action,details_json) VALUES(?,?,?)', (actor,'wanted_request_withdrawn',encode({'source_id':source_id,'item_key':item_key})))
