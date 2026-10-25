"""Offline installed-webhook playback and first-Yes native completion contracts."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from curatarr.adapters import AdapterError
from curatarr.integrations.jellyfin.viewing import viewing_hint, JellyfinViewingVerifier
from curatarr.integrations.ryot.completion import RyotCompletionWriter
from curatarr.services.direct_viewing import DirectViewing, CandidateViewingAdapter
from curatarr.services.jellyfin_viewing import DirectViewingSettings, JellyfinViewingIntake, INACTIVITY
from curatarr.services.viewing_workflow import ViewingWorkflow, ViewingResponseError, bot_key
from curatarr.services.viewing import visible_to, pending_movie_count, ViewingFollowupService
from curatarr.services.viewing_corrections import ViewingCorrections
from curatarr.storage import Storage, StorageError
from curatarr.storage.viewing import ViewingStore
from curatarr.web.app import create_app
import test_miniapp as mini
import test_movie_acquisition as acquisition

USER='a'*32
USER2='b'*32
ITEM='c'*32
SETUP={'server_id':'server','users':[{'id':USER,'name':'Erje','administrator':False},
                                    {'id':USER2,'name':'Admin','administrator':True}]}


def payload(event='Progress', position=95, *, session='living-room', count=0, date=None, user=USER):
    return {'Event':event,'Server':{'Id':'server'},'User':{'Id':user},'Session':{'Id':session,'PlayState':{'PositionTicks':position}},
            'Item':{'Id':ITEM,'Type':'Movie','Name':'Example','ProductionYear':2020,'ProviderIds':{'Tmdb':'123','Imdb':'tt0012345'},
                    'RunTimeTicks':100,'UserData':{'PlayCount':count,'LastPlayedDate':date}}}


class Verifier:
    def __init__(self):self.calls=[];self.fail=False
    def setup(self):return deepcopy(SETUP)
    def verify(self,hint):
        self.calls.append(hint)
        if self.fail:raise AdapterError('PRIVATE URL/token')
        return True


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.s=Storage(':memory:');self.s.initialize();self.addCleanup(self.s.close)
        self.now=1000.;self.verifier=Verifier();self.intake=JellyfinViewingIntake(self.s,self.verifier,clock=lambda:self.now)
        self.settings=self.intake.settings
        self.save()
    def save(self,**values):
        old=self.settings.get()
        return self.settings.save({'enabled':True,'minimum':5,'completed':95,'accounts':[USER,USER2],**values},
            revision=old['revision'],actor='admin',setup=SETUP)
    def send(self,*args,**kwargs):
        self.intake.receive(payload(*args,**kwargs));self.intake.tick()
    def candidates(self):
        return [dict(r) for r in self.s.connection.execute('SELECT * FROM playback_candidate')]
    def test_no_candidate_below_completion_and_no_native_writes(self):
        self.send('Play',0);self.send(position=94);self.send('Stop',94)
        self.assertEqual(self.candidates(),[])
    def test_completion_waits_for_stop_and_deduplicates_shared_users_retries_resume(self):
        self.send('Play',0);self.send(position=95)
        self.assertFalse(self.candidates())
        self.send('Stop',95);self.send('Stop',95);self.send('Stop',95,user=USER2)
        self.send('Resume',96);self.send('Stop',96)
        self.assertEqual(len(self.candidates()),1)
        hint=json.loads(self.candidates()[0]['payload_json'])
        self.assertEqual(hint['_policy_revision'],1);self.assertIsNone(hint['_finished_on'])
    def test_stop_with_no_position_settles_armed_progress(self):
        self.send(position=96);self.send('Stop',None)
        self.assertEqual(len(self.candidates()),1)
    def test_inactivity_settles_once_and_survives_a_new_worker(self):
        self.send(position=95);self.now+=INACTIVITY+1
        JellyfinViewingIntake(self.s,self.verifier,clock=lambda:self.now).tick();self.intake.tick()
        self.assertEqual(len(self.candidates()),1)
    def test_distinct_sessions_and_verified_count_advance_are_separate_watches(self):
        self.send('Play',0);self.send('Stop',100,count=1,date='2026-10-24T10:00:00Z')
        self.send('Play',0,count=1,date='2026-10-24T10:00:00Z')
        self.send('Stop',100,count=2,date='2026-10-24T12:00:00Z')
        self.send('Stop',100,session='bedroom')
        self.assertEqual(len(self.candidates()),3)
        hints=[json.loads(r['payload_json']) for r in self.candidates()]
        self.assertEqual(hints[0]['_finished_on'],'2026-10-24T10:00:00+00:00')
        self.assertEqual(hints[1]['_finished_on'],'2026-10-24T12:00:00+00:00')
        self.assertIsNone(hints[2]['_finished_on'])
    def test_stale_last_played_does_not_become_current_watch_date(self):
        self.send('Play',0,count=1,date='2020-01-01T00:00:00Z')
        self.send('Stop',100,count=1,date='2020-01-01T00:00:00Z')
        self.assertIsNone(json.loads(self.candidates()[0]['payload_json'])['_finished_on'])
    def test_out_of_order_old_count_does_not_create_a_repeat(self):
        self.send('Play',0,count=1);self.send('Stop',100,count=2)
        self.send('Play',0,count=0);self.send('Stop',99,count=0)
        self.assertEqual(len(self.candidates()),1)
    def test_threshold_edit_cannot_retroactively_complete_old_progress(self):
        self.send('Play',0);self.send(position=80);self.save(completed=75)
        self.send('Stop',80);self.now+=1000;self.intake.tick()
        self.assertFalse(self.candidates())
        self.send('Play',1);self.send('Stop',81)
        self.assertEqual(len(self.candidates()),1)
    def test_disabled_unselected_and_other_server_are_not_accepted(self):
        self.save(accounts=[USER]);self.send('Stop',100,user=USER2)
        bad=payload();bad['Server']['Id']='other'
        with self.assertRaises(ValueError):self.intake.receive(bad)
        self.save(enabled=False);self.send('Stop',100)
        self.assertFalse(self.candidates());self.assertFalse(self.verifier.calls)
    def test_failed_verification_is_durable_and_does_not_publish(self):
        self.verifier.fail=True;self.send('Stop',100)
        row=self.s.connection.execute('SELECT * FROM coordination_event').fetchone()
        self.assertEqual(row['status'],'deferred');self.assertNotIn('PRIVATE',row['error_code'])
        self.assertFalse(self.candidates())
        self.verifier.fail=False;self.now+=301;self.intake.tick();self.assertEqual(len(self.candidates()),1)
    def test_manual_confirmation_coalesces_before_worker_publication(self):
        self.intake.receive(payload('Stop',100));self.intake.receive(payload('MarkPlayed'))
        self.intake.tick();self.assertEqual(len(self.candidates()),1)
    def test_old_policy_event_does_not_publish_after_accounts_change(self):
        self.intake.receive(payload('Stop',100));self.save(accounts=[USER2]);self.intake.tick()
        self.assertFalse(self.candidates());self.assertFalse(self.verifier.calls)
    def test_mark_unplayed_is_evidence_only(self):
        self.send('MarkUnplayed');self.assertFalse(self.candidates())
        self.assertEqual(self.s.connection.execute('SELECT status FROM coordination_event').fetchone()[0],'processed')
    def test_settings_validation_revision_and_default_disabled(self):
        other=Storage(':memory:');other.initialize();self.addCleanup(other.close)
        self.assertFalse(DirectViewingSettings(other).get()['enabled'])
        for args in [{'minimum':95},{'completed':101},{'accounts':[]},{'accounts':['d'*32]},{'minimum':True}]:
            with self.subTest(args=args),self.assertRaises(ValueError):self.save(**args)
        with self.assertRaises(StorageError):
            self.settings.save({'enabled':True,'minimum':5,'completed':95,'accounts':[USER]},revision=0,actor='a',setup=SETUP)

    def test_same_event_after_policy_edit_is_a_retry_not_changed_content(self):
        self.send('Stop',100);self.save(completed=90)
        self.assertFalse(self.intake.receive(payload('Stop',100)))
        self.intake.tick();self.assertEqual(len(self.candidates()),1)
    def test_resuming_at_beginning_without_a_new_completion_anchor_does_not_duplicate(self):
        self.send('Play',0,count=0);self.send('Stop',100,count=1)
        self.send('Play',1,count=1);self.send('Stop',99,count=1)
        self.now+=1000;self.intake.tick();self.assertEqual(len(self.candidates()),1)


class ParserTests(unittest.TestCase):
    def test_installed_default_pascal_and_camel_format(self):
        raw=payload('MarkPlayed');raw['Session']=None
        expected=viewing_hint(raw)
        def camel(obj):
            return {k[0].lower()+k[1:]:camel(v) for k,v in obj.items()} if isinstance(obj,dict) else obj
        self.assertEqual(viewing_hint(camel(raw)),expected)
        self.assertIsNone(expected['duration']);self.assertIsNone(expected['session_id'])
    def test_config_viewing_token_is_optional_validated_and_hidden(self):
        import yaml
        from curatarr.config import parse_config,ConfigError
        raw=yaml.safe_load(mini.VALID_CONFIG)
        self.assertIsNone(parse_config(raw).jellyfin.viewing_webhook_token)
        for value in ['short',123,'x'*129,'x/'*30]:
            raw['jellyfin']['viewing_webhook_token']=value
            with self.assertRaises(ConfigError):parse_config(raw)
        raw['jellyfin']['viewing_webhook_token']='x'*43
        parsed=parse_config(raw)
        self.assertEqual(parsed.jellyfin.viewing_webhook_token,'x'*43)
        self.assertNotIn('x'*43,repr(parsed))
    def test_other_events_and_series_bypass_position_parser(self):
        self.assertIsNone(viewing_hint({'Event':'ItemRemoved'}))
        self.assertIsNone(viewing_hint({'Event':'Play','Item':{'Type':'Episode'}}))
    def test_malformed_conflicting_or_unscoped_payload_fails_closed(self):
        mutations=[lambda p:p.update(event='Stop'),lambda p:p['User'].update(Id='name'),
            lambda p:p['Item'].update(RunTimeTicks=0),lambda p:p['Session']['PlayState'].update(PositionTicks=True),
            lambda p:p['Item']['ProviderIds'].update(tmdb='456'),lambda p:p['Item']['ProviderIds'].update(Tmdb='0'),
            lambda p:p['Item']['UserData'].update(LastPlayedDate='2026-10-24'),lambda p:p.update(Session=None)]
        for mutate in mutations:
            raw=payload();mutate(raw)
            with self.subTest(raw=raw),self.assertRaises(ValueError):viewing_hint(raw)
    def test_verifier_rechecks_server_and_exact_tmdb_imdb(self):
        calls=[]
        verifier=JellyfinViewingVerifier('http://local','secret',SimpleNamespace(movie_external_ids=lambda _: {'id':123,'imdb_id':'tt0012345'}))
        def get(path):
            calls.append(path)
            if path=='/System/Info/Public':return {'Id':'server'}
            return {'Id':ITEM,'Type':'Movie','ProviderIds':{'Tmdb':'123','Imdb':'tt0012345'}}
        verifier.client.get=get
        self.assertTrue(verifier.verify(viewing_hint(payload())))
        self.assertEqual(len(calls),2)
        verifier.client.get=lambda _: {'Id':'other'}
        with self.assertRaises(AdapterError):verifier.verify(viewing_hint(payload()))

    def test_manual_mark_played_verifies_current_user_state(self):
        verifier=JellyfinViewingVerifier('http://local','secret',SimpleNamespace(movie_external_ids=lambda _: {'id':123,'imdb_id':'tt0012345'}))
        raw={'Id':ITEM,'Type':'Movie','ProviderIds':{'Tmdb':'123','Imdb':'tt0012345'},'UserData':{'Played':False}}
        verifier.client.get=lambda path: {'Id':'server'} if path=='/System/Info/Public' else raw
        with self.assertRaises(AdapterError):verifier.verify(viewing_hint(payload('MarkPlayed')))
        raw['UserData']['Played']=True
        self.assertTrue(verifier.verify(viewing_hint(payload('MarkPlayed'))))


class Completion(RyotCompletionWriter):
    def __init__(self,cat):super().__init__(cat);self.calls=[];self.lost=False;self.apply_effect=True;self.hook=None
    def create(self,snapshot,operation,finished_on):
        self.calls.append(operation)
        if self.apply_effect:
            self.catalogue.value['history'].append({'id':'seen-'+str(len(self.calls)), 'metadataId':'movie','userId':'household',
                'state':'COMPLETED','progress':'100','finishedOn':finished_on,'reviewId':None,'providersConsumedOn':[self.marker(operation)]})
            self.catalogue.value['collections']['Completed']={}
        if self.hook:self.hook()
        if self.lost:raise AdapterError('Lost response')
        return {'accepted':True}


class Native:
    def __init__(self,cat):self.cat=cat;self.reader=cat.reader;self.source='native-source'
    def account(self):return {'id':'household','source':self.source,'scale':'OUT_OF_FIVE'}
    def scan(self):
        return self.account(), [{'event_id':v['id'],'media_key':'ryot:movie','title':'Example','identities':['tmdb:123'],
            'finished_on':v.get('finishedOn')} for v in (self.cat.value or {}).get('history',[]) if v['state']=='COMPLETED']
    def event(self,key,event_id,source):
        if key!='ryot:movie' or source!=self.source:raise AdapterError('Wrong scope')
        event=next(v for v in self.cat.value['history'] if v['id']==event_id)
        return {**self.account(),'movie_identities':{'tmdb:123'}},deepcopy(event),deepcopy(self.cat.value['reviews'])
    def validate(self,context):return self.event(context['media_key'],context['event_id'],context['source'])


class DirectViewingTests(unittest.TestCase):
    def setUp(self):
        self.s=Storage(':memory:');self.s.initialize();self.addCleanup(self.s.close)
        self.intake=JellyfinViewingIntake(self.s,Verifier());self.intake.settings.save(
            {'enabled':True,'minimum':5,'completed':95,'accounts':[USER,USER2]},revision=0,actor='admin',setup=SETUP)
        self.cat=acquisition.Catalogue();self.completion=Completion(self.cat);self.native=Native(self.cat)
        self.direct=DirectViewing(self.s,self.cat,self.completion,self.native,participants=[1,2])
        self.direct.journal.clock=lambda:1000
        self.client=SimpleNamespace(token='test')
        self.workflow=ViewingWorkflow(self.s,CandidateViewingAdapter(self.native,self.direct),self.client,participants=[1,2],direct=self.direct)
        self.store=self.workflow.store
        self.store.bind({'enabled':True,'source':self.native.source,'bot_key':bot_key('test'),'chat_id':10})
        self.store.observe(self.native.account(),[],participants=[1,2])
    def candidate(self,event='Stop',session='living-room',count=0):
        self.intake.receive(payload(event,100,session=session,count=count));self.intake.tick();self.direct.publish()
        return self.s.viewing_followups()[-1]
    def answer(self,d,action,user=1,admin=False):
        return self.direct.answer(d,action,actor='miniapp:user:'+str(user),user_name='Alice',admin_review=admin)
    def test_publish_is_local_and_first_yes_creates_one_watch_then_rating(self):
        d=self.candidate();self.assertFalse(self.cat.calls);self.assertFalse(self.completion.calls)
        self.assertIn('Jellyfin reported',self.workflow.prompt(d).text)
        updated=self.answer(d,'yes')
        self.assertTrue(updated.context['native_linked']);self.assertTrue(updated.context['confirmed'])
        self.assertEqual(updated.context['questions'],['rating']);self.assertEqual(updated.media_key,'tmdb:123')
        self.assertEqual(updated.context['media_key'],'ryot:movie');self.assertIsNone(updated.context['finished_on'])
        self.assertFalse(visible_to(updated,2));self.assertTrue(visible_to(updated,1))
        self.assertEqual(len(self.cat.value['history']),1);self.assertIn('Curatarr',self.cat.value['collections'])
        self.direct.resume(updated.decision_id);self.assertEqual(len(self.completion.calls),1)
    def test_all_no_and_review_again_create_no_native_history(self):
        d=self.answer(self.candidate(),'no');self.assertTrue(visible_to(d,2));self.assertFalse(visible_to(d,1))
        d=self.answer(d,'no',user=2);self.assertTrue(d.context['declined_all'])
        self.assertFalse(self.cat.calls);self.assertEqual(pending_movie_count(self.s,1),0)
        corrections=ViewingCorrections(self.workflow)
        self.assertEqual([v['enabled'] for v in corrections.actions(d,'valid')],[True,True,False,False])
        corrections.review_again(d,user_id=1)
        fresh=self.s.get_pending_decision(d.decision_id);self.assertFalse(fresh.context['declined_all']);self.assertTrue(visible_to(fresh,1))
        self.assertFalse(self.cat.calls)
    def test_admin_can_confirm_an_all_no_record(self):
        d=self.answer(self.answer(self.candidate(),'no'),'no',user=2)
        result=self.answer(d,'yes',admin=True)
        self.assertTrue(result.context['native_linked']);self.assertEqual(len(self.completion.calls),1)
    def test_unauthorized_and_stale_household_answers_do_not_write(self):
        d=self.candidate()
        with self.assertRaises(ViewingResponseError):self.answer(d,'yes',user=3)
        self.answer(d,'no')
        with self.assertRaises(StorageError):self.answer(d,'yes')
        self.assertFalse(self.completion.calls)
    def test_existing_manual_mark_played_reuses_history_but_real_playback_adds_repeat(self):
        self.cat.register_library('123','Example','household','0'*32)
        old={'id':'trakt','metadataId':'movie','userId':'household','state':'COMPLETED','finishedOn':'2020-01-01T00:00:00Z',
             'reviewId':None,'providersConsumedOn':['Trakt']}
        self.cat.value['history']=[old];self.cat.value['reviews']=[{'id':'keep'}]
        d=self.answer(self.candidate('MarkPlayed'),'yes')
        self.assertTrue(d.context['reused_existing']);self.assertEqual(self.completion.calls,[])
        self.assertEqual(self.cat.value['history'],[old]);self.assertEqual(self.cat.value['reviews'],[{'id':'keep'}])
        d=self.answer(self.candidate('Stop',session='other'),'yes')
        self.assertFalse(d.context['reused_existing']);self.assertEqual(len(self.cat.value['history']),2)
        self.assertEqual(self.cat.value['history'][0],old)
    def test_lost_completion_reply_is_read_back_without_second_write_or_second_question(self):
        d=self.candidate();self.completion.lost=True
        self.completion.hook=lambda:self.store.observe(*self.native.scan(),participants=[1,2])
        d=self.answer(d,'yes');self.assertFalse(d.context['native_linked']);self.assertEqual(len(self.s.viewing_followups()),1)
        self.assertFalse(visible_to(d,2));self.assertIn('awaits verification',self.workflow.prompt(d).text)
        self.assertEqual([b.callback_data for b in self.workflow.prompt(d).buttons],[f'view:{d.decision_id}:retrywatch'])
        self.direct.journal.clock=lambda:1301;self.direct.resume(d.decision_id)
        self.assertEqual(len(self.completion.calls),1)
        d=self.s.get_pending_decision(d.decision_id);self.assertTrue(d.context['native_linked'])
        self.store.observe(*self.native.scan(),participants=[1,2]);self.assertEqual(len(self.s.viewing_followups()),1)
        self.assertEqual(self.s.connection.execute('SELECT decision_id FROM viewing_event').fetchone()[0],d.decision_id)
    def test_uncertain_no_effect_never_blindly_resubmits_or_offers_rating(self):
        self.completion.lost=True;self.completion.apply_effect=False
        d=self.answer(self.candidate(),'yes');self.direct.journal.clock=lambda:1301;self.direct.resume(d.decision_id)
        self.assertEqual(len(self.completion.calls),1);self.assertFalse(d.context['confirmed'])
        self.assertNotIn('pick',str(self.workflow.prompt(d).buttons))
    def test_duplicate_manual_event_coalesces_with_an_unanswered_playback(self):
        d=self.candidate();self.candidate('MarkPlayed')
        self.assertEqual(len(self.s.viewing_followups()),1)
    def test_native_history_changes_after_publication_are_preserved_at_confirmation(self):
        d=self.candidate();self.cat.register_library('123','Example','household','0'*32)
        old={'id':'another','userId':'household','metadataId':'movie','state':'COMPLETED','providersConsumedOn':[]}
        self.cat.value['history']=[old]
        self.answer(d,'yes');self.assertEqual(self.cat.value['history'][0],old);self.assertEqual(len(self.cat.value['history']),2)
    def test_failed_publication_is_visible_and_recoverable(self):
        self.cat.snapshot=lambda _: (_ for _ in ()).throw(AdapterError('No native read'))
        self.intake.receive(payload('Stop',100));self.intake.tick();self.direct.publish()
        self.assertEqual(self.s.connection.execute('SELECT error_code FROM playback_candidate').fetchone()[0],'candidate_publication_unverified')
        self.cat.snapshot=acquisition.Catalogue.snapshot.__get__(self.cat)
        self.direct.publish();self.assertEqual(len(self.s.viewing_followups()),1)
        self.assertIsNone(self.s.connection.execute('SELECT error_code FROM playback_candidate').fetchone()[0])
    def test_old_unpublished_policy_candidates_do_not_starve_the_new_policy(self):
        for index in range(21):
            self.intake.receive(payload('Stop',100,session='old-'+str(index)))
        self.intake.tick()
        self.intake.settings.save({'enabled':True,'minimum':5,'completed':90,'accounts':[USER]},revision=1,actor='admin',setup=SETUP)
        self.intake.receive(payload('Stop',100,session='new'));self.intake.tick();self.direct.publish()
        self.assertEqual(len(self.s.viewing_followups()),1)
        candidate=self.s.connection.execute('SELECT payload_json FROM playback_candidate WHERE decision_id IS NOT NULL').fetchone()
        self.assertEqual(json.loads(candidate[0])['session_id'],'new')
    def test_native_account_change_blocks_mutation(self):
        d=self.candidate();self.native.source='changed'
        # Authorization is retained, but exact operation guard blocks all writes.
        updated=self.answer(d,'yes');self.assertFalse(updated.context['native_linked']);self.assertFalse(self.cat.calls)
    def test_web_question_uses_local_validation_before_a_native_record_exists(self):
        d=self.candidate();result=self.workflow.web_question(d,user_id=1)
        self.assertEqual([v['action'] for v in result['actions']],['yes','no'])
        with patch.object(self.native,'validate',side_effect=AssertionError('Native event does not exist')):
            self.workflow._respond(d,'no',actor='miniapp:user:1')
        self.assertFalse(self.cat.calls)
    def test_local_inbox_can_display_a_movie_absent_from_ryot(self):
        self.candidate()
        reader=SimpleNamespace(get_item=lambda _:SimpleNamespace(value=None))
        result=ViewingFollowupService(self.s,reader,bot_key('test')).review(1)
        self.assertTrue(result['validated']);self.assertEqual(result['movie']['key'],'tmdb:123')
        self.assertIsNone(result['movie']['watch_count'])
    def test_completion_does_not_consume_watchlist_intent_silently(self):
        self.cat.register_library('123','Example','household','0'*32);self.cat.value['collections']['Watchlist']={}
        d=self.answer(self.candidate(),'yes');self.assertFalse(d.context['native_linked']);self.assertFalse(self.completion.calls)
        self.assertIn('Watchlist',self.cat.value['collections'])


class CompletionPortTests(unittest.TestCase):
    def test_native_mutation_uses_without_dates_or_exact_source_date_and_reference(self):
        calls=[]
        class Client:
            def execute(self,query,variables):calls.append(variables);return {'deployBulkMetadataProgressUpdate':True}
        cat=acquisition.Catalogue();cat.client=Client();cat.register_library('123','Example','household','0'*32)
        writer=RyotCompletionWriter(cat)
        writer.create(cat.snapshot('123'),'a'*32,None)
        writer.create(cat.snapshot('123'),'b'*32,'2026-10-24T10:00:00+00:00')
        first=calls[0]['input'][0]['change']['createNewCompleted']
        self.assertEqual(set(first),{'withoutDates'});self.assertEqual(first['withoutDates']['providersConsumedOn'],[writer.marker('a'*32)])
        self.assertEqual(calls[1]['input'][0]['change']['createNewCompleted']['finishedOnDate']['timestamp'],'2026-10-24T10:00:00+00:00')
    def test_ambiguous_extra_native_event_and_changed_reviews_fail_preservation(self):
        cat=acquisition.Catalogue();cat.register_library('123','Example','household','0'*32);writer=Completion(cat)
        before=cat.snapshot('123');writer.create(before,'a'*32,None)
        cat.value['history'].append({**cat.value['history'][0],'id':'extra','providersConsumedOn':[]})
        with self.assertRaises(AdapterError):writer.preserved(before,cat.snapshot('123'),'a'*32)
        cat.value['history'].pop();cat.value['reviews']=[{'id':'other'}]
        with self.assertRaises(AdapterError):writer.preserved(before,cat.snapshot('123'),'a'*32)


class DirectViewingApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture=mini.MiniAppTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        import tempfile
        from pathlib import Path
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'state.sqlite'
        self.config=replace(self.fixture.config,coordinator=replace(self.fixture.config.coordinator,enabled=True),
            jellyfin=replace(self.fixture.config.jellyfin,viewing_webhook_token='a'*43))
        self.factory=lambda config,storage:JellyfinViewingIntake(storage,Verifier())
        self.client=TestClient(create_app(self.config,database=self.path,service=self.fixture.service,intake_factory=self.factory))
        self.headers={'Authorization':'tma '+mini.signed_data(1)}
        self.standard={'Authorization':'tma '+mini.signed_data(2)}
    def enable(self):
        return self.client.post('/api/settings/viewing',headers=self.headers,json={'revision':0,'enabled':True,'minimum':5,'completed':95,'accounts':[USER]})
    def test_admin_settings_are_opt_in_and_revision_checked(self):
        initial=self.client.get('/api/settings/viewing',headers=self.headers)
        self.assertEqual(initial.status_code,200);self.assertFalse(initial.json()['policy']['enabled'])
        self.assertEqual(self.client.get('/api/settings/viewing',headers=self.standard).status_code,403)
        self.assertEqual(self.enable().status_code,200);self.assertEqual(self.enable().status_code,409)
    def test_machine_token_is_independent_of_telegram_and_bounded_payload(self):
        self.enable();url='/hooks/jellyfin/viewing'
        self.assertEqual(self.client.post(url,headers=self.headers,json=payload()).status_code,404)
        self.assertEqual(self.client.post(url+'?token='+('a'*43),content='x'*262145).status_code,413)
        self.assertEqual(self.client.post(url+'?token='+('a'*43),json=[]).status_code,400)
        with patch.object(Verifier,'verify',side_effect=AssertionError('No provider calls in request')):
            result=self.client.post(url+'?token='+('a'*43),json=payload('Stop',100))
        self.assertEqual(result.status_code,202);self.assertTrue(result.json()['queued'])
    def test_other_events_never_trigger_removal_or_position_parsing(self):
        self.enable();result=self.client.post('/hooks/jellyfin/viewing?token='+('a'*43),json={'Event':'ItemRemoved'})
        self.assertEqual(result.status_code,202);self.assertFalse(result.json()['queued'])
    def test_default_disabled_or_coordinator_off_queues_nothing(self):
        result=self.client.post('/hooks/jellyfin/viewing?token='+('a'*43),json=payload())
        self.assertFalse(result.json()['queued'])
        app=create_app(replace(self.config,coordinator=replace(self.config.coordinator,enabled=False)),database=self.path,service=self.fixture.service)
        self.assertEqual(TestClient(app).post('/hooks/jellyfin/viewing?token='+('a'*43),json=payload()).status_code,404)
