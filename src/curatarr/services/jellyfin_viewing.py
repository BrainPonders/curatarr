"""Durable playback evidence and shared movie candidates, before any Ryot write."""
from copy import deepcopy
import hashlib
import json

from curatarr.adapters import AdapterError
from curatarr.integrations.jellyfin.viewing import event_key, identifier, viewing_hint
from curatarr.storage import StorageError
from curatarr.storage.journal import CoordinationJournal, encode


INACTIVITY = 900


class DirectViewingSettings:
    def __init__(self,storage):self.storage=storage

    def get(self):
        row=self.storage.connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key='jellyfin_viewing' AND source='policy'").fetchone()
        return json.loads(row[0]) if row else {'enabled':False,'minimum':5,'completed':95,'accounts':[],'server_id':None,'revision':0}

    def save(self,values,*,revision,actor,setup):
        if (set(values)!={'enabled','minimum','completed','accounts'} or type(values['enabled']) is not bool
                or type(values['minimum']) is not int or type(values['completed']) is not int
                or not 0<=values['minimum']<values['completed']<=100 or not isinstance(values['accounts'],list)):
            raise ValueError('Choose valid playback thresholds and account settings.')
        accounts=[identifier(a,guid=True) for a in values['accounts']]
        if len(set(accounts))!=len(accounts) or len(accounts)>100:
            raise ValueError('Choose unique household playback accounts.')
        if not set(accounts)<={a['id'] for a in setup['users']} or values['enabled'] and not accounts:
            raise ValueError('Select at least one verified playback account before enabling.')
        server=identifier(setup['server_id'])
        with self.storage.connection:
            self.storage.connection.execute('BEGIN IMMEDIATE');old=self.get()
            if old['revision']!=revision:raise StorageError('Playback settings changed. Refresh before saving.')
            result={**values,'accounts':sorted(accounts),'server_id':server,'revision':revision+1}
            self.storage.connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('jellyfin_viewing','policy','',?) "
                'ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP',(encode(result),))
            self.storage.connection.execute('INSERT INTO audit_log(actor,action,details_json) VALUES(?,?,?)',
                (actor,'jellyfin_viewing_settings_changed',encode({'revision':result['revision'],'enabled':result['enabled']})))
        return result


class JellyfinViewingIntake:
    def __init__(self,storage,verifier,*,clock=None):
        self.storage,self.verifier=storage,verifier
        self.journal=CoordinationJournal(storage) if clock is None else CoordinationJournal(storage,clock)
        self.db=storage.connection;self.settings=DirectViewingSettings(storage)

    def source(self,policy):
        row=self.db.execute("SELECT id,revision FROM coordination_source WHERE kind='jellyfin_viewing' AND identity=? AND account='household'",(policy['server_id'],)).fetchone()
        return self.journal.source('jellyfin_viewing',policy['server_id'],account='household',
                                  config={'policy_revision':policy['revision']},expected_revision=row['revision'] if row else None)

    def receive(self,payload):
        hint=viewing_hint(payload);policy=self.settings.get()
        if hint is None or not policy['enabled']:
            return False
        if hint['server_id']!=policy['server_id']:
            raise ValueError('The webhook came from a different server.')
        if hint['user_id'] not in policy['accounts']:
            return False
        source=self.source(policy)
        old=self.db.execute('SELECT payload_json,status FROM coordination_event WHERE source_id=? AND occurrence_key=?',
                            (source['id'],event_key(hint))).fetchone()
        if old is not None:
            if json.loads(old['payload_json'])['hint']!=hint:raise StorageError('Playback evidence changed.')
            return old['status']!='processed'
        event=self.journal.event(source['id'],event_key(hint),'tmdb:'+hint['tmdb'],'jellyfin_'+hint['event'],{'hint':hint,'policy':policy})
        return event['status']!='processed'

    def tick(self):
        policy=self.settings.get()
        if not policy['enabled']:return
        ready=self.db.execute("SELECT id FROM coordination_event WHERE kind LIKE 'jellyfin_%' AND status!='processed' "
            'AND retry_at<=? AND coalesce(lease_until,0)<=? ORDER BY id LIMIT 100',
            (self.journal.now(),self.journal.now())).fetchall()
        for row in ready:
            identifier=row['id'];event=self.journal.get_event(identifier)
            claim=self.journal.claim_event(identifier,lease_seconds=300)
            if claim is None:continue
            try:
                payload=json.loads(event['payload_json']);hint=payload['hint']
                if payload['policy']['revision']!=policy['revision'] or hint['server_id']!=policy['server_id'] or hint['user_id'] not in policy['accounts']:
                    self.journal.finish_event(claim);continue
                self.verifier.verify(hint)
                with self.journal.transaction():
                    self.journal._event_owner(self.journal.get_event(identifier),claim)
                    if self.settings.get()['revision']!=policy['revision']:
                        raise StorageError('Playback policy changed during verification.')
                    if hint['event']=='MarkPlayed':
                        existing=self.db.execute("SELECT c.id FROM playback_candidate c LEFT JOIN pending_decision d ON d.id=c.decision_id "
                            "WHERE c.source_id=? AND c.media_key=? AND json_extract(c.payload_json,'$._policy_revision')=? "
                            "AND (d.id IS NULL OR d.status='open' AND json_extract(d.context_json,'$.confirmed')=0 "
                            "AND coalesce(json_extract(d.context_json,'$.declined_all'),0)=0)",(event['source_id'],event['media_key'],policy['revision'])).fetchone()
                        if not existing:self._candidate(event['source_id'],'manual:'+event['occurrence_key'],'manual',hint)
                    elif hint['event']!='MarkUnplayed':self._session(event['source_id'],hint,policy)
                    # MarkUnplayed is retained source evidence, never history deletion.
                    self.db.execute("UPDATE coordination_event SET status='processed',lease_token=NULL,lease_until=NULL,error_code=NULL WHERE id=?",(identifier,))
            except (AdapterError,ValueError,KeyError,TypeError):
                self.journal.defer_event(claim,'playback_evidence_unverified')
        self.settle()

    def _session(self,source,hint,policy):
        key=hashlib.sha256(encode([hint['session_id'],hint['item_id']]).encode()).hexdigest()
        row=self.db.execute('SELECT * FROM playback_session WHERE source_id=? AND session_key=? ORDER BY cycle DESC LIMIT 1',(source,key)).fetchone()
        old=json.loads(row['state_json']) if row else None
        if row and row['media_key']!='tmdb:'+hint['tmdb']:
            raise ValueError('A playback item changed its movie identity.')
        anchor=[hint['play_count'],hint['last_played']]
        if old and hint['play_count'] is not None and old['latest_anchor'][0] is not None and hint['play_count']<old['latest_anchor'][0]:
            return  # An older delivery cannot start a fresh repeat cycle.
        if old and anchor[1] is not None and old['latest_anchor'][1] is not None and anchor[1]<old['latest_anchor'][1]:
            return
        fresh_start=hint['event']=='Play' and hint['position']*100<=policy['minimum']*hint['duration']
        distinct=old and any(v is not None for v in anchor) and anchor!=old['initial_anchor']
        if old and old['policy_revision']!=policy['revision'] and not fresh_start:
            return  # A threshold edit cannot arm old playback retroactively.
        new=old is None or fresh_start and (old['policy_revision']!=policy['revision'] or old['closed'] and distinct)
        if new:
            if old and old.get('unattributed_restart'):
                old.pop('repeat_uncertain',None);old.pop('unattributed_restart',None)
                self.db.execute('UPDATE playback_session SET state_json=? WHERE id=?',(encode(old),row['id']))
            cycle=(row['cycle']+1) if row else 1
            state={'policy_revision':policy['revision'],'minimum':policy['minimum'],'completed':policy['completed'],
                   'runtime':hint['duration'],'initial_anchor':anchor,'latest_anchor':anchor,'max_position':0,
                   'armed':False,'closed':False,'candidate':False,'require_advance':bool(old and old['candidate']),'users':[],'hint':hint}
            identity=hashlib.sha256(encode([source,key,cycle]).encode()).hexdigest()
        else:
            cycle=row['cycle'];state=old;identity=row['id']
            if state['runtime']!=hint['duration']:raise ValueError('Playback runtime changed within one occurrence.')
            if (hint['play_count'] is not None and state['latest_anchor'][0] is not None
                    and hint['play_count']<state['latest_anchor'][0]):return
        if not new and fresh_start and state['candidate']:
            state['unattributed_restart']=True;state['restart_max']=hint['position']
        if state.get('unattributed_restart') and hint['position'] is not None:
            state['restart_max']=max(state.get('restart_max',0),hint['position'])
        if hint['user_id'] not in state['users']:state['users'].append(hint['user_id'])
        state['latest_anchor']=anchor;state['hint']=hint
        if hint['position'] is not None:
            state['max_position']=max(state['max_position'],hint['position'])
            if (hint['position']*100>=state['minimum']*state['runtime']
                    and hint['position']*100>=state['completed']*state['runtime']):state['armed']=True
        if hint['event']=='Stop':
            state['closed']=True
            if (state['armed'] and not state['candidate'] and not self._repeat_verified(state)
                    or state.get('unattributed_restart') and state.get('restart_max',0)*100>=state['completed']*state['runtime']):
                state['repeat_uncertain']=True
            if state['armed'] and not state['candidate'] and self._repeat_verified(state):
                self._candidate(source,'session:'+identity,'playback',self._completion_hint(state));state['candidate']=True
        elif hint['event'] in {'Play','Resume'}:state['closed']=False if not state['candidate'] else state['closed']
        self.db.execute('INSERT INTO playback_session(id,source_id,session_key,cycle,media_key,state_json,updated_at) VALUES(?,?,?,?,?,?,?) '
                        'ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at',
                        (identity,source,key,cycle,'tmdb:'+hint['tmdb'],encode(state),self.journal.now()))

    @staticmethod
    def _repeat_verified(state):
        if not state.get('require_advance'):return True
        first,last = state['initial_anchor'],state['latest_anchor']
        return bool(first[0] is not None and last[0] is not None and last[0]>first[0]
                    or first[1] is not None and last[1] is not None and last[1]>first[1])

    @staticmethod
    def _completion_hint(state):
        hint = dict(state['hint'])
        # LastPlayedDate can refer to an earlier watch. Only a witnessed count
        # advance with a changed date attributes it to this playback cycle.
        initial = state['initial_anchor']
        hint['_finished_on'] = (hint['last_played'] if initial[0] is not None
            and hint['play_count'] is not None and hint['play_count']>initial[0]
            and hint['last_played']!=initial[1] else None)
        return hint

    def _candidate(self,source,occurrence,kind,hint):
        identity=hashlib.sha256(encode([source,occurrence]).encode()).hexdigest()[:32]
        hint={**hint,'_policy_revision':self.settings.get()['revision']}
        if kind=='manual':hint['_finished_on']=hint['last_played']
        self.db.execute('INSERT INTO playback_candidate(id,source_id,occurrence_key,media_key,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?) '
                        'ON CONFLICT(source_id,occurrence_key) DO NOTHING',(identity,source,occurrence,'tmdb:'+hint['tmdb'],kind,encode(hint),self.journal.now()))

    def settle(self):
        policy=self.settings.get()
        if not policy['enabled']:return
        with self.journal.transaction():
            if self.settings.get()['revision']!=policy['revision']:return
            for row in self.db.execute('SELECT * FROM playback_session WHERE updated_at<=?',(self.journal.now()-INACTIVITY,)).fetchall():
                state=json.loads(row['state_json'])
                if state['policy_revision']!=policy['revision'] or not state['armed'] or state['candidate'] or not self._repeat_verified(state):continue
                self._candidate(row['source_id'],'session:'+row['id'],'playback',self._completion_hint(state))
                state.update(candidate=True,closed=True)
                self.db.execute('UPDATE playback_session SET state_json=? WHERE id=?',(encode(state),row['id']))
