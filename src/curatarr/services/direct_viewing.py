"""Household confirmation before a journaled Ryot completion; legacy rating reuse."""
from copy import deepcopy
import hashlib
import json

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.viewing import SUPPORTED_REVIEW_SCALES
from curatarr.services.operations import Inspection,OperationRunner,OperationStep,Outcome
from curatarr.services.jellyfin_viewing import DirectViewingSettings
from curatarr.storage import StorageError
from curatarr.storage.journal import CoordinationJournal,encode
from curatarr.storage.viewing import ViewingStore,encoded


class DirectViewing:
    def __init__(self,storage,catalogue,completion,native,*,participants):
        self.storage,self.catalogue,self.completion,self.native=storage,catalogue,completion,native
        self.participants=tuple(sorted(set(participants)));self.db=storage.connection
        self.journal=CoordinationJournal(storage);self.store=ViewingStore(storage)

    def publish(self):
        policy=DirectViewingSettings(self.storage).get();binding=self.store.binding()
        if not policy['enabled'] or not binding or not binding.get('enabled'):return
        account=self.native.account()
        if binding['source']!=account['source'] or account['scale'] not in SUPPORTED_REVIEW_SCALES:
            raise AdapterError('The native viewing account or rating scale changed.')
        for row in self.db.execute("SELECT * FROM playback_candidate WHERE decision_id IS NULL AND json_extract(payload_json,'$._policy_revision')=? ORDER BY created_at LIMIT 20",(policy['revision'],)).fetchall():
            try:
                self._publish(row,policy,account)
            except (AdapterError,ValueError,KeyError,TypeError):
                with self.db:
                    self.db.execute("UPDATE playback_candidate SET error_code='candidate_publication_unverified' WHERE id=? AND decision_id IS NULL",(row['id'],))

    def _publish(self,row,policy,account):
        hint=json.loads(row['payload_json'])
        if hint.get('_policy_revision')!=policy['revision']:return
        before=self.catalogue.snapshot(hint['tmdb']);self.completion.history(before)
        if before['account_id']!=account['id']:raise AdapterError('Viewing and catalogue account differ.')
        context={'workflow':'viewing_v1','direct_candidate':row['id'],'native_linked':False,
                 'source':account['source'],'account_id':account['id'],'event_id':'jfc_'+row['id'],
                 'media_key':row['media_key'],'title':hint['title'],'year':hint.get('year'),
                 'identities':['tmdb:'+hint['tmdb']],'finished_on':hint.get('_finished_on'),
                 'questions':['watched'],'confirmed':False,'rating':None,'rating_scale':5,
                 'participants':list(self.participants),'responses':{},'delivery':{'state':'pending'},'operation':None,
                 'native_before':before,'candidate_kind':row['kind'],'playback_source':row['source_id']}
        with self.journal.transaction():
            current=self.db.execute('SELECT decision_id FROM playback_candidate WHERE id=?',(row['id'],)).fetchone()
            if current['decision_id'] is not None:return
            if DirectViewingSettings(self.storage).get()['revision']!=policy['revision']:return
            cursor=self.db.execute("INSERT INTO pending_decision(media_key,decision_type,audience,state_fingerprint,context_json) VALUES(?,'viewing_followup','shared_household',?,?)",
                (row['media_key'],hashlib.sha256(row['id'].encode()).hexdigest(),encoded(context)))
            self.db.execute('UPDATE playback_candidate SET decision_id=?,error_code=NULL WHERE id=?',(cursor.lastrowid,row['id']))
    def validate(self,context):
        row=self.db.execute('SELECT * FROM playback_candidate WHERE id=?',(context['direct_candidate'],)).fetchone()
        if row is None or row['media_key']!='tmdb:'+json.loads(row['payload_json'])['tmdb'] or row['media_key']!=context['media_key']:
            raise AdapterError('The original local viewing candidate changed.')
        account=self.native.account()
        if account['source']!=context['source'] or account['id']!=context['account_id']:
            raise AdapterError('The native viewing account changed.')
        return account,{'id':context['event_id'],'reviewId':None},[]

    def answer(self,decision,action,*,actor,user_name=None,admin_review=False):
        from curatarr.services.viewing_workflow import ViewingWorkflow,ViewingResponseError
        context=deepcopy(decision.context);user=int(actor.rsplit(':',1)[1])
        if context.get('confirmed_by') is not None:
            if context['confirmed_by']!=user and not admin_review:raise ViewingResponseError('This viewing is already confirmed by another household member.')
            self.resume(decision.decision_id)
            return self.storage.get_pending_decision(decision.decision_id)
        if action not in {'yes','no'}:raise ViewingResponseError('Confirm watched status before rating.')
        if context.get('declined_all') and not admin_review:raise ViewingResponseError('This viewing awaits admin review.')
        if user not in context['participants'] and not admin_review:raise ViewingResponseError('You are not a respondent for this request.')
        from datetime import datetime,timezone
        context['responses'][str(user)]={'answer':action,'name':ViewingWorkflow.respondent_name(user_name,user),
            'answered_at':datetime.now(timezone.utc).isoformat()}
        context['declined_all']=action=='no' and bool(context['participants']) and all(context['responses'].get(str(p),{}).get('answer')=='no' for p in context['participants'])
        context.setdefault('delivery',{})['web_refresh']=True
        if action=='yes':context.update(confirmed_by=user,watch_state='preparing',declined_all=False)
        updated=self.store.update(decision,context,actor=actor,action='direct_viewing_confirmed' if action=='yes' else 'direct_viewing_declined_all' if context['declined_all'] else 'direct_viewing_user_declined')
        if action=='yes':
            self.resume(updated.decision_id)
            updated=self.storage.get_pending_decision(updated.decision_id)
        return updated

    def resume(self,decision_id):
        decision=self.storage.get_pending_decision(decision_id);context=decision.context
        if context.get('native_linked') or context.get('confirmed_by') is None or decision.status!='open':return
        key='viewing:'+context['direct_candidate']
        existing=self.db.execute('SELECT id FROM coordination_operation WHERE request_key=?',(key,)).fetchone()
        if existing:operation=self.journal.operation(existing['id'])
        else:
            before=self.catalogue.snapshot(decision.media_key.split(':',1)[1]);history=self.completion.history(before)
            reuse=None
            if context['candidate_kind']=='manual':
                complete=[v for v in history.values() if v['state']=='COMPLETED']
                if complete:reuse=sorted(complete,key=lambda v:(v.get('finishedOn') or '',v['id']))[-1]['id']
            expected={'decision_id':decision_id,'candidate_id':context['direct_candidate'],'candidate':{'tmdb':decision.media_key.split(':',1)[1],'title':context['title']},
                      'before':before,'source':context['source'],'account_id':context['account_id'],'finished_on':context.get('finished_on'),'reuse':reuse,
                      'confirmed_by':context['confirmed_by']}
            operation=self.journal.create_operation(key,decision.media_key,'movie_viewing',
                {'actor':'household:user:'+str(context['confirmed_by']),'basis':'confirmed_viewing'},expected,
                ['guard','library','metadata_ready','completed_viewing','marker_collection','marker_membership','preservation'])
        current=self.storage.get_pending_decision(decision_id)
        if current.context.get('watch_operation')!=operation['id']:
            update=deepcopy(current.context);update['watch_operation']=operation['id'];update['watch_state']='pending'
            self.store.update(current,update,actor='system',action='direct_viewing_operation_linked')
        result=self.run(operation['id'])
        return result

    def guard(self,operation):
        e=operation['expected'];decision=self.storage.get_pending_decision(e['decision_id'])
        if (decision is None or decision.status!='open' or decision.context.get('direct_candidate')!=e['candidate_id']
                or decision.context.get('confirmed_by')!=e['confirmed_by'] or decision.context.get('declined_all')):
            raise ValueError('Household authorization changed.')
        account=self.native.account()
        if account['source']!=e['source'] or account['id']!=e['account_id'] or account['scale'] not in SUPPORTED_REVIEW_SCALES:
            raise AdapterError('The viewing account changed.')
        current=self.catalogue.snapshot(e['candidate']['tmdb'])
        if self.store.reset_pending(decision.media_key) or current['movie'] and self.store.reset_pending('ryot:'+current['movie']['metadata']['id']):
            raise ValueError('A viewing history reset is pending.')
        untouched=all(s['status']=='planned' for s in operation['steps'] if s['name']!='guard')
        if untouched and current!=e['before']:raise ValueError('Native facts changed before viewing creation.')
        if current['movie'] and any(v['state']!='COMPLETED' for v in self.completion.history(current).values()):
            raise ValueError('Existing partial or other viewing history requires maintainer review before completion.')
        if current['movie'] and any(n.casefold().replace(' ','') in {'watchlist','monitoring'} for n in current['movie']['collections']):
            raise ValueError('Native watch intent has completion side effects; maintainer review is required.')
        self.completion.preserved(e['before'],current,operation['id'],finished_on=e['finished_on'],reuse=e['reuse'],allow_unlinked=True)
        return current

    def inspect(self,name,operation):
        e=operation['expected'];current=self.guard(operation);movie=current['movie']
        if name=='guard':done=True
        elif name=='library':done=movie is not None and self.catalogue.reader.collections.library in movie['collections']
        elif name=='metadata_ready':done=movie is not None and movie['metadata'].get('isPartial') is False
        elif name=='completed_viewing':
            event=self.completion.result(e['before'],current,operation['id'],finished_on=e['finished_on'],reuse=e['reuse']) if movie else None
            return Inspection(Outcome.VERIFIED if event else Outcome.NEEDS_WRITE,{'event_id':event['id']} if event else {})
        elif name=='marker_collection':done=self.catalogue.marker in self.catalogue.account()['collections']
        elif name=='marker_membership':done=movie is not None and self.catalogue.marker in movie['collections']
        elif name=='preservation':
            self.completion.preserved(e['before'],current,operation['id'],finished_on=e['finished_on'],reuse=e['reuse']);done=True
        else:raise ValueError('Unknown viewing operation step.')
        return Inspection(Outcome.VERIFIED if done else Outcome.NEEDS_WRITE,{'step':name})

    def apply(self,name,operation):
        e=operation['expected'];current=self.guard(operation)
        if name=='library':
            return (self.catalogue.register_library(e['candidate']['tmdb'],e['candidate']['title'],current['account_id'],operation['id'])
                    if current['movie'] is None else self.catalogue.membership(current,self.catalogue.reader.collections.library,True))
        if name=='metadata_ready':return self.catalogue.refresh_metadata(current)
        if name=='completed_viewing':return self.completion.create(current,operation['id'],e['finished_on'])
        if name=='marker_collection':return self.catalogue.ensure_marker()
        if name=='marker_membership':return self.catalogue.membership(current,self.catalogue.marker,True)
        raise ValueError('This step has no native write.')

    def run(self,identifier):
        names=['guard','library','metadata_ready','completed_viewing','marker_collection','marker_membership','preservation']
        plan=[OperationStep(n,lambda op,n=n:self.inspect(n,op),lambda op,n=n:self.apply(n,op)) for n in names]
        result=OperationRunner(self.journal,{'movie_viewing':plan}).run(identifier)
        if result['status']=='completed':self.link(result)
        return result

    def link(self,operation):
        e=operation['expected'];decision=self.storage.get_pending_decision(e['decision_id'])
        if decision.context.get('native_linked'):return
        fresh=self.catalogue.snapshot(e['candidate']['tmdb'])
        event=self.completion.preserved(e['before'],fresh,operation['id'],finished_on=e['finished_on'],reuse=e['reuse'])
        native_key='ryot:'+fresh['movie']['metadata']['id']
        account,verified,reviews=self.native.event(native_key,event['id'],e['source'])
        if 'tmdb:'+e['candidate']['tmdb'] not in account['movie_identities']:raise AdapterError('Linked movie identity changed.')
        context=deepcopy(decision.context)
        context.update(native_linked=True,confirmed=True,watch_state='verified',media_key=native_key,event_id=event['id'],
                       finished_on=event.get('finishedOn'),questions=['rating'],identities=sorted(account['movie_identities']),
                       reused_existing=bool(e['reuse']))
        from curatarr.services.viewing_workflow import ViewingWorkflow
        existing=ViewingWorkflow._existing_rating(account,verified,reviews)
        context['existing_rating']=str(existing/20) if existing is not None else None
        context.setdefault('delivery',{})['web_refresh']=True
        with self.journal.transaction():
            cursor=self.db.execute("UPDATE pending_decision SET context_json=? WHERE id=? AND context_json=? AND status='open'",
                                   (encoded(context),decision.decision_id,encoded(decision.context)))
            if cursor.rowcount!=1:raise StorageError('The viewing request changed before linking.')
            old=self.db.execute('SELECT * FROM viewing_event WHERE source=? AND event_id=?',(e['source'],event['id'])).fetchone()
            if old is None:
                self.db.execute('INSERT INTO viewing_event(source,event_id,media_key,payload_json,decision_id) VALUES(?,?,?,?,?)',
                    (e['source'],event['id'],native_key,encode({'event_id':event['id'],'media_key':native_key,'source':'curatarr'}),decision.decision_id))
            elif old['media_key']!=native_key:raise StorageError('The native viewing ledger changed identity.')
            self.db.execute('INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)',
                ('system','direct_viewing_native_verified',decision.media_key,encode({'decision_id':decision.decision_id,'event_id':event['id'],'operation_id':operation['id']})))

    def tick(self):
        self.publish()
        for decision in self.storage.viewing_followups():
            if decision.context.get('direct_candidate') and decision.context.get('confirmed_by') is not None and not decision.context.get('native_linked'):
                self.resume(decision.decision_id)


class CandidateViewingAdapter:
    """Existing workflow/rating API, with local validation before native creation."""
    def __init__(self,native,direct):self.native,self.direct,self.reader=native,direct,native.reader
    def account(self):return self.native.account()
    def scan(self):return self.native.scan()
    def validate(self,context):
        if context.get('direct_candidate') and not context.get('native_linked'):return self.direct.validate(context)
        return self.native.validate(context)
    def rate(self,*args,**kwargs):return self.native.rate(*args,**kwargs)
