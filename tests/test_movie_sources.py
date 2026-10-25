"""Wanted list boundaries, source policy fencing, and household suggestion API."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from curatarr.adapters import AdapterError
from curatarr.services.acquisition import MovieAcquisition
from curatarr.services.source_intake import DiscoveryFilters
from curatarr.services.sources import MovieSources
from curatarr.storage import Storage, StorageError
from curatarr.web.app import create_app
import test_miniapp as mini
from test_movie_acquisition import Catalogue, Arr
from test_source_intake import movie, native


class Metadata:
    def __init__(self): self.missing=False; self.conflict=False; self.calls=[]
    def movie_genres(self): return {'genres':[{'id':18,'name':'Drama'},{'id':35,'name':'Comedy'}]}
    def discover_movies(self, params):
        self.calls.append(params)
        return {'page':1,'total_pages':1,'total_results':1,'results':[native(558, poster_path='/fixture.jpg')]}
    def find_imdb(self, imdb):
        self.calls.append(imdb)
        return {'movie_results':[] if self.missing else [{'id':558}], 'tv_results':[{'id':99}] if self.missing else []}
    def movie_external_ids(self, tmdb): return {'id':558,'imdb_id':'tt0000002' if not self.conflict else 'tt9999999'}
    def movie_details(self, tmdb): return {'id':558,'title':'Example','release_date':'2026-01-01'}


class SourceServiceTests(unittest.TestCase):
    def setUp(self):
        self.storage=Storage(':memory:'); self.storage.initialize(); self.addCleanup(self.storage.close)
        self.cat,self.arr,self.metadata=Catalogue(),Arr(),Metadata()
        self.acquisition=MovieAcquisition(self.storage,self.cat,self.arr)
        self.now=1000; self.sources=MovieSources(self.storage,self.metadata,self.acquisition,lambda:set(),clock=lambda:self.now)
        self.actor='telegram:user:1'
    def configure(self, *, mode='add_search', enabled=True):
        return self.sources.save_wanted('ur123',{'enabled':enabled,'mode':mode,'profile_id':2 if mode=='add_search' else None,'root_id':1 if mode=='add_search' else None},revision=0,actor=self.actor)
    def imported(self, source, identifiers):
        return self.sources.import_wanted(source['id'],revision=source['revision'],content='Const\n'+'\n'.join(identifiers)+'\n',count=len(identifiers),complete=True,actor=self.actor)
    def ready(self, **kwargs):
        source=self.configure(**kwargs); self.imported(source,['tt0000001']); self.imported(source,['tt0000001','tt0000002']); return source
    def operation(self):
        row=self.storage.connection.execute('SELECT id FROM coordination_operation').fetchone()
        return self.acquisition.journal.operation(row[0]) if row else None

    def test_current_entries_are_historical_even_when_enabled_and_removed_then_reappear(self):
        source=self.configure(); self.imported(source,['tt0000001','tt0000002'])
        self.sources.wanted_tick(); self.imported(source,['tt0000001']); self.imported(source,['tt0000001','tt0000002']); self.sources.wanted_tick()
        self.assertEqual(self.metadata.calls,[]); self.assertIsNone(self.operation()); self.assertEqual(self.arr.calls,[])

    def test_new_entry_creates_one_operation_and_replayed_snapshot_never_searches_again(self):
        source=self.ready(); self.sources.wanted_tick(); operation=self.operation()
        self.assertEqual(operation['authorization']['basis'],'trusted_wanted_source')
        self.assertEqual(self.arr.calls,[])
        self.acquisition.run(operation['id']); self.sources.wanted_tick()
        self.imported(source,['tt0000001','tt0000002']); self.sources.wanted_tick()
        self.assertEqual(self.arr.calls,['add','search'])
        self.assertEqual(self.storage.connection.execute("SELECT state FROM wanted_request WHERE item_key='imdb:tt0000002'").fetchone()[0],'fulfilled')

    def test_catalogue_policy_has_no_radarr_calls_or_acquisition_flags(self):
        self.ready(mode='catalogue_only'); self.sources.wanted_tick(); self.acquisition.run(self.operation()['id']); self.sources.wanted_tick()
        self.assertEqual(self.arr.calls,[]); self.assertEqual(set(self.cat.value['collections']),{'Library','Curatarr'})

    def test_disabled_policy_never_resolves_or_executes_new_entries(self):
        self.ready(enabled=False); self.sources.wanted_tick()
        self.assertEqual(self.metadata.calls,[]); self.assertIsNone(self.operation())

    def test_tv_only_or_not_yet_available_metadata_stays_waiting(self):
        self.ready(); self.metadata.missing=True; self.sources.wanted_tick()
        request=self.sources.requests()[0]; self.assertEqual(request['status'],'waiting'); self.assertIsNone(request['candidate'])
        self.assertIsNone(self.operation()); self.assertEqual(self.arr.calls,[])
        self.metadata.missing=False; self.now+=86401; self.sources.wanted_tick(); self.assertIsNotNone(self.operation())

    def test_export_title_is_displayed_while_waiting_without_becoming_a_title_match(self):
        source=self.configure();self.imported(source,['tt0000001'])
        self.sources.import_wanted(source['id'],revision=source['revision'],content='Const,Title\ntt0000001,Old\ntt0000002,An announced movie\n',count=2,complete=True,actor=self.actor)
        self.metadata.missing=True;self.sources.wanted_tick();request=self.sources.requests()[0]
        self.assertEqual(request['title'],'An announced movie');self.assertIsNone(request['candidate']);self.assertIsNone(self.operation())

    def test_conflicting_ids_are_not_replaced_by_title_or_tv_search(self):
        self.ready(); self.metadata.conflict=True; self.sources.wanted_tick()
        self.assertIsNone(self.operation()); self.assertEqual(self.sources.requests()[0]['status'],'needs_input')

    def test_withdrawn_request_survives_future_source_presence_without_archiving(self):
        source=self.ready(); self.metadata.missing=True; self.sources.wanted_tick(); request=self.sources.requests()[0]
        self.sources.withdraw(source['id'],request['item_key'],revision=request['revision'],actor=self.actor)
        self.imported(source,['tt0000001','tt0000002']); self.metadata.missing=False; self.now+=86401; self.sources.wanted_tick()
        self.assertIsNone(self.operation()); self.assertEqual(self.sources.requests(),[]); self.assertIsNone(self.cat.value)

    def test_archived_movie_requires_explicit_user_reactivation(self):
        self.ready(); self.cat.register_library('558','Example','household','fixture'); self.cat.value['collections']['Archived']={}
        self.sources.wanted_tick(); self.assertIsNone(self.operation()); request=self.sources.requests()[0]
        self.assertEqual(request['candidate']['tmdb'],'558'); self.assertEqual(self.arr.calls,[])

    def test_changed_profile_name_and_same_id_blocks_unattended_download(self):
        self.ready(); self.arr.profile['name']='Different profile'; self.sources.wanted_tick()
        self.assertIsNone(self.operation()); self.assertEqual(self.sources.requests()[0]['status'],'needs_input')

    def test_policy_changed_after_authorization_blocks_before_any_effect(self):
        source=self.ready(); self.sources.wanted_tick(); operation=self.operation()
        settings=json.loads(source['config_json']); settings['enabled']=False
        self.sources.journal.source('wanted_list',source['identity'],config=settings,expected_revision=source['revision'])
        result=self.acquisition.run(operation['id']); self.assertEqual(result['status'],'blocked')
        self.assertEqual(self.arr.calls,[]); self.assertEqual(self.cat.calls,[])

    def test_uncertain_search_is_not_duplicated_by_list_processor_or_restart(self):
        self.ready(); self.sources.wanted_tick(); operation=self.operation(); self.arr.lost=True
        self.assertEqual(self.acquisition.run(operation['id'])['status'],'uncertain')
        self.sources.wanted_tick(); self.sources.wanted_tick(); self.assertEqual(self.arr.calls,['add','search'])
        self.assertEqual(self.storage.connection.execute('SELECT count(*) FROM coordination_operation').fetchone()[0],1)

    def test_reconnect_settings_never_replaces_historical_baseline(self):
        source=self.configure(); self.imported(source,['tt0000001'])
        digest=self.sources.journal.get_source(source['id'])['baseline_hash']
        changed=self.sources.save_wanted('ur123',{'enabled':False,'mode':'catalogue_only','profile_id':None,'root_id':None},revision=source['revision'],actor=self.actor)
        self.assertEqual(changed['baseline_hash'],digest)
        self.assertEqual(self.sources.public()['wanted']['baseline_complete'],True)
        self.assertNotIn('root_path',str(self.sources.public()))

    def test_complete_export_count_and_revision_are_required(self):
        source=self.configure()
        with self.assertRaises(ValueError): self.sources.import_wanted(source['id'],revision=source['revision'],content='<html>challenge</html>',count=0,complete=True,actor=self.actor)
        with self.assertRaises(StorageError): self.sources.import_wanted(source['id'],revision=99,content='Const\n',count=0,complete=True,actor=self.actor)
        self.assertFalse(self.sources.public()['wanted']['baseline_complete'])

    def test_malformed_export_title_does_not_claim_and_lock_the_source(self):
        source=self.configure()
        with self.assertRaises(ValueError):
            self.sources.import_wanted(source['id'],revision=source['revision'],content='Const,Title\ntt0000001,bad\x00title\n',count=1,complete=True,actor=self.actor)
        self.imported(source,['tt0000001'])
        self.assertTrue(self.sources.public()['wanted']['baseline_complete'])

    def test_discovery_daily_schedule_no_unchanged_poll_spam_and_failed_polls_retry(self):
        initial=self.sources.discovery_source(); values=json.loads(initial['config_json']); values['enabled']=True
        self.sources.save_discovery(values,revision=initial['revision'],actor=self.actor)
        self.assertTrue(self.sources.collect()); self.assertFalse(self.sources.collect()); self.assertEqual(len(self.metadata.calls),1)
        self.now+=86401; self.assertTrue(self.sources.collect()); self.assertEqual(len(self.metadata.calls),2)

    def test_filter_edit_hides_retained_backlog_without_erasing_no(self):
        source=self.sources.discovery_source(); claim=self.sources.intake.begin(source['id'])
        self.sources.intake.discovery(claim,[movie(558)],DiscoveryFilters(),known_keys=set())
        values=json.loads(source['config_json']); values['excluded_genres']=[18]
        self.sources.save_discovery(values,revision=0,actor=self.actor)
        self.assertEqual(self.sources.suggestions()['items'],[])
        self.assertEqual(self.storage.connection.execute('SELECT decision FROM discovery_candidate').fetchone()[0],'pending')

    def test_explicit_yes_stays_available_for_choices_after_discovery_filter_edits(self):
        source=self.sources.discovery_source()
        self.sources.intake.discovery(self.sources.intake.begin(source['id']),[movie(558)],DiscoveryFilters(),known_keys=set())
        self.sources.intake.decide('tmdb:558',revision=0,actor=self.actor,accept=True)
        values=json.loads(source['config_json']);values['excluded_genres']=[18]
        self.sources.save_discovery(values,revision=0,actor=self.actor)
        self.assertEqual(self.sources.suggestions()['items'][0]['decision'],'selected')

    def test_user_can_choose_catalogue_only_instead_of_unattended_download_policy(self):
        source=self.ready(); self.arr.profile['name']='Changed profile'; self.sources.wanted_tick()
        origin={'kind':'wanted','source_id':source['id'],'item_key':'imdb:tt0000002','revision':source['revision']}
        candidate={'tmdb':'558','imdb':'tt0000002','title':'Example','year':2026}
        preview=self.acquisition.preview(candidate,actor=self.actor,mode='catalogue_only',origin=origin)
        operation=self.acquisition.confirm(preview['decision_id'],actor=self.actor,version=preview['version'])
        self.assertEqual(operation['authorization']['basis'],'confirmed_preview')
        self.assertEqual(self.acquisition.run(operation['id'])['status'],'completed')
        self.now+=3601;self.sources.wanted_tick();self.assertEqual(self.arr.calls,[])
        self.assertEqual(self.storage.connection.execute("SELECT state FROM wanted_request WHERE item_key='imdb:tt0000002'").fetchone()[0],'fulfilled')

    def test_selecting_a_different_wanted_source_stops_unstarted_old_source_operation(self):
        self.ready();self.sources.wanted_tick();operation=self.operation()
        self.sources.save_wanted('ur999',{'enabled':False,'mode':'catalogue_only','profile_id':None,'root_id':None},revision=0,actor=self.actor)
        self.assertEqual(self.acquisition.run(operation['id'])['status'],'blocked')
        self.assertEqual(self.cat.calls,[]);self.assertEqual(self.arr.calls,[])

    def test_saved_policy_can_be_disabled_during_provider_outage_without_guessing_new_choices(self):
        source=self.configure();self.arr.selection=lambda *args: (_ for _ in ()).throw(AdapterError('Provider unavailable'))
        settings={'enabled':False,'mode':'add_search','profile_id':2,'root_id':1}
        saved=self.sources.save_wanted('ur123',settings,revision=source['revision'],actor=self.actor)
        self.assertFalse(json.loads(saved['config_json'])['enabled'])

    def test_conflicting_metadata_is_monitorable_but_ordinary_missing_metadata_is_not_an_error(self):
        from curatarr.services.system_maintenance import SystemMaintenance
        self.ready();self.metadata.conflict=True;self.sources.wanted_tick()
        monitor=SystemMaintenance(self.storage,None,None,None)
        self.assertIn('wanted_source_request_unverified',[i['kind'] for i in monitor.local_issues()])
        self.metadata.conflict=False;self.metadata.missing=True;self.now+=3601;self.sources.wanted_tick()
        self.assertNotIn('wanted_source_request_unverified',[i['kind'] for i in monitor.local_issues()])


class SourceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        fixture=mini.MiniAppTests();fixture.setUp();self.fixture=fixture;self.headers=fixture.headers
        self.config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True))
        self.path=Path(self.temp.name)/'state.sqlite';self.cat,self.arr,self.metadata=Catalogue(),Arr(),Metadata()
        self.acq=lambda config,storage:MovieAcquisition(storage,self.cat,self.arr)
        self.sources=lambda config,storage,acquisition:MovieSources(storage,self.metadata,acquisition,lambda:set())
        self.client=TestClient(create_app(self.config,database=self.path,service=fixture.service,acquisition_factory=self.acq,source_factory=self.sources))
        self.user={'Authorization':'tma '+mini.signed_data(2)}
        s=Storage(self.path)
        sources=self.sources(self.config,s,self.acq(self.config,s));source=sources.discovery_source()
        sources.intake.discovery(sources.intake.begin(source['id']),[movie(558,title='<script>unsafe</script>')],DiscoveryFilters(),known_keys=set());s.close()
    def answer(self, action, revision=0, headers=None):
        return self.client.post('/api/suggestions/558',headers=headers or self.headers,json={'action':action,'revision':revision})
    def test_standard_user_can_review_but_cannot_change_source_policy(self):
        self.assertEqual(self.client.get('/api/suggestions',headers=self.user).status_code,200)
        self.assertEqual(self.client.get('/api/settings/sources',headers=self.user).status_code,403)
        self.assertEqual(self.client.post('/api/settings/sources/check',headers=self.user).status_code,403)
        self.assertEqual(self.client.get('/api/suggestions').status_code,401)
    def test_yes_opens_shared_normal_movie_choices_without_downloading(self):
        result=self.answer('yes');self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['candidates'][0]['identities'],['tmdb:558'])
        self.assertEqual(self.arr.calls,[]);self.assertEqual(self.cat.calls,[])
        self.assertEqual(self.client.get('/api/suggestions',headers=self.user).json()['items'][0]['decision'],'selected')
        self.assertEqual(self.answer('no',headers=self.user).status_code,409)
    def test_cancelling_choices_preserves_selected_proposal_and_can_reopen_later(self):
        self.answer('yes');result=self.answer('open',revision=1,headers=self.user)
        self.assertEqual(result.status_code,200);self.assertEqual(self.arr.calls,[])
    def test_no_is_shared_persistent_and_reconsider_does_not_create_provider_state(self):
        self.assertEqual(self.answer('no').status_code,200)
        self.assertEqual(self.client.get('/api/suggestions',headers=self.user).json()['items'],[])
        self.assertEqual(len(self.client.get('/api/suggestions?view=declined',headers=self.user).json()['items']),1)
        self.assertEqual(self.answer('reopen',revision=1,headers=self.user).status_code,200)
        self.assertEqual(self.cat.calls,[])
    def test_explicit_catalogue_confirmation_closes_suggestion_without_radarr(self):
        selected=self.answer('yes').json()
        preview=self.client.post('/api/search/'+selected['search_id']+'/0/acquisition-preview',headers=self.headers,json={'mode':'catalogue_only'})
        self.assertEqual(preview.status_code,200,preview.text)
        result=self.client.post('/api/acquisition/confirm',headers=self.headers,json={**{k:preview.json()[k] for k in ['decision_id','version']},'confirm':True})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.client.get('/api/suggestions',headers=self.headers).json()['items'],[])
        self.assertEqual(self.arr.calls,[])
    def test_two_household_previews_cannot_authorize_two_operations(self):
        first=self.answer('yes').json();second=self.answer('open',revision=1,headers=self.user).json();previews=[]
        for result,headers in ((first,self.headers),(second,self.user)):
            previews.append(self.client.post('/api/search/'+result['search_id']+'/0/acquisition-preview',headers=headers,json={'mode':'add_search','profile_id':2,'root_id':1}).json())
        for index,(preview,headers) in enumerate(zip(previews,(self.headers,self.user))):
            response=self.client.post('/api/acquisition/confirm',headers=headers,json={**{k:preview[k] for k in ['decision_id','version']},'confirm':True})
            self.assertEqual(response.status_code,200 if index==0 else 409)
        self.assertEqual(self.arr.calls,['add','search'])
    def test_settings_list_native_genres_and_omit_native_paths(self):
        response=self.client.get('/api/settings/sources',headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['genres'][0]['name'],'Comedy');self.assertNotIn('/movies',response.text)
    def test_snapshot_import_baselines_without_download_and_rejects_incomplete_file(self):
        settings=self.client.post('/api/settings/sources/wanted',headers=self.headers,json={'expected_revision':0,'imdb_user':'ur123','enabled':True,'mode':'add_search','profile_id':2,'root_id':1})
        self.assertEqual(settings.status_code,200,settings.text);wanted=settings.json()['wanted']
        values={'source_id':wanted['id'],'expected_revision':wanted['revision'],'content':'Const\ntt0000001\n','count':1,'complete':True}
        result=self.client.post('/api/settings/sources/imdb-import',headers=self.headers,json=values)
        self.assertEqual(result.status_code,200,result.text);self.assertTrue(result.json()['historical_baseline'])
        values['count']=2;self.assertEqual(self.client.post('/api/settings/sources/imdb-import',headers=self.headers,json=values).status_code,400)
        self.assertEqual(self.arr.calls,[])
    def test_disabled_coordinator_hides_sources_and_does_not_poll(self):
        client=TestClient(create_app(self.fixture.config,database=self.path,service=self.fixture.service,acquisition_factory=self.acq,source_factory=self.sources))
        self.assertEqual(client.get('/api/suggestions',headers=self.headers).status_code,404)
        self.assertEqual(self.metadata.calls,[])
    def test_invalid_paging_and_client_provider_payload_rejected(self):
        self.assertEqual(self.client.get('/api/suggestions?after=wrong',headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/suggestions/558',headers=self.headers,json={'revision':0,'action':'yes','tmdb':999}).status_code,422)

    def test_source_settings_remain_readable_when_genres_and_profiles_are_unavailable(self):
        self.metadata.movie_genres=lambda: (_ for _ in ()).throw(AdapterError('secret'))
        self.arr.options=lambda: (_ for _ in ()).throw(AdapterError('secret'))
        result=self.client.get('/api/settings/sources',headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(result.json()['genres_available']);self.assertFalse(result.json()['choices_available'])
        self.assertNotIn('secret',result.text)


if __name__=='__main__':unittest.main()
