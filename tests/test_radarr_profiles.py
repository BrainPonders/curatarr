"""Narrow, actor-bound profile changes with exact native recovery and preservation."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from curatarr.adapters import AdapterError
from curatarr.integrations.radarr.profiles import RadarrProfileWriter
from curatarr.services.radarr_profiles import MovieProfileChanges
from curatarr.storage import Storage
from curatarr.web.app import create_app
from test_movie_acquisition import Catalogue
import test_miniapp as mini


def movie():
    return {'id':10,'tmdbId':558,'imdbId':'tt0316654','title':'Spider-Man 2','qualityProfileId':2,
            'monitored':False,'hasFile':True,'path':'/movies/Spider-Man 2 (2004)','rootFolderPath':'/movies',
            'minimumAvailability':'released','tags':[8,9],'movieFileId':30,
            'movieFile':{'id':30,'relativePath':'movie.mkv','size':100000,'quality':{'quality':{'name':'HD'}},
                         'dateAdded':'2025-01-01T00:00:00Z','qualityCutoffNotMet':False}}


class Native:
    url='http://native-radarr';api_key='private-test-key'
    def __init__(self):
        self.value=movie();self.profiles=[{'id':2,'name':'HD','upgradeAllowed':False},
            {'id':3,'name':'4K','upgradeAllowed':True},{'id':4,'name':'Remux'},{'id':5,'name':'Compact'}]
        self.calls=[];self.lost=False;self.effect=True;self.hook=None
    def movies(self):return [deepcopy(self.value)] if self.value else []
    def _get(self,path,params):
        if path=='/api/v3/qualityprofile':return deepcopy(self.profiles)
        raise AssertionError('Unexpected provider read '+path)
    def put(self,path,payload):
        self.calls.append((path,deepcopy(payload)))
        if self.effect:self.value['qualityProfileId']=payload['qualityProfileId']
        if self.hook:self.hook()
        if self.lost:raise AdapterError('Reply lost')
        return [deepcopy(self.value)]


def service(storage,native,cat):
    return MovieProfileChanges(storage,RadarrProfileWriter(native,native.put),cat)


class ProfileServiceTests(unittest.TestCase):
    def setUp(self):
        self.s=Storage(':memory:');self.s.initialize();self.addCleanup(self.s.close)
        self.native=Native();self.cat=Catalogue();self.cat.register_library('558','Spider-Man 2','household','a'*32);self.cat.calls=[]
        self.cat.value['history']=[{'id':'old-watch','finishedOn':'2020-01-01'}]
        self.cat.value['reviews']=[{'id':'old-review','rating':'80'}]
        self.profile=service(self.s,self.native,self.cat);self.now=1000;self.profile.journal.clock=lambda:self.now
        self.candidate={'tmdb':'558','imdb':'tt0316654','title':'Spider-Man 2','year':2004}
    def choices(self,actor='telegram:user:1'):return self.profile.choices(self.candidate,actor=actor)
    def confirm(self,choice=None,target=3,actor='telegram:user:1'):
        choice=choice or self.choices()
        return self.profile.confirm(choice['decision_id'],actor=actor,version=choice['version'],profile_id=target)
    def retry(self,op):self.now+=301;return self.profile.run(op['id'])
    def test_open_and_confirm_do_not_write_until_operation_runs(self):
        p=self.choices();self.assertEqual(p['current_profile_id'],2);self.assertEqual(len(p['profiles']),4)
        self.confirm(p);self.assertEqual(self.native.calls,[]);self.assertEqual(self.cat.calls,[])
    def test_success_changes_only_profile_and_marks_verified_provenance(self):
        before=deepcopy(self.native.value);history=deepcopy(self.cat.value['history']);reviews=deepcopy(self.cat.value['reviews'])
        op=self.confirm();result=self.profile.run(op['id']);self.assertEqual(result['status'],'completed')
        self.assertEqual(self.native.calls,[('/api/v3/movie/editor',{'movieIds':[10],'qualityProfileId':3})])
        expected=deepcopy(before);expected['qualityProfileId']=3;self.assertEqual(self.native.value,expected)
        self.assertEqual(self.cat.value['history'],history);self.assertEqual(self.cat.value['reviews'],reviews)
        self.assertIn('Curatarr',self.cat.value['collections'])
    def test_confirmation_retry_is_idempotent_and_cannot_change_target(self):
        p=self.choices();op=self.confirm(p);self.profile.run(op['id'])
        self.assertEqual(self.confirm(p)['id'],op['id']);self.profile.run(op['id']);self.assertEqual(len(self.native.calls),1)
        with self.assertRaises(ValueError):self.confirm(p,target=4)
    def test_same_or_unknown_profile_is_rejected(self):
        p=self.choices()
        for target in (2,999,True):
            with self.subTest(target=target),self.assertRaises(ValueError):self.confirm(p,target)
        self.assertEqual(self.native.calls,[])
    def test_wrong_actor_or_version_cannot_confirm(self):
        p=self.choices()
        with self.assertRaises(ValueError):self.confirm(p,actor='telegram:user:2')
        with self.assertRaises(ValueError):self.confirm({**p,'version':'0'*64})
    def test_expired_selection_cannot_confirm(self):
        p=self.choices()
        with self.s.connection:self.s.connection.execute("UPDATE pending_decision SET expires_at='2000-01-01 00:00:00' WHERE id=?",(p['decision_id'],))
        with self.assertRaises(ValueError):self.confirm(p)
    def test_stale_profile_monitoring_path_tags_or_file_blocks_confirmation(self):
        for key,value in [('qualityProfileId',4),('monitored',True),('path','/other/movie'),('tags',[9]),('movieFileId',99)]:
            original=deepcopy(self.native.value);p=self.choices();self.native.value[key]=value
            with self.subTest(key=key),self.assertRaises((AdapterError,ValueError)):self.confirm(p)
            self.native.value=original
        self.assertEqual(self.native.calls,[])
    def test_native_identity_conflict_and_partial_data_never_write(self):
        self.native.value['imdbId']='tt9999999'
        with self.assertRaises(AdapterError):self.choices()
        self.native.value=movie();self.native.value.pop('monitored')
        with self.assertRaises(AdapterError):self.choices()
    def test_profile_definition_change_blocks_confirmation(self):
        p=self.choices();self.native.profiles[1]['name']='Changed'
        with self.assertRaises(AdapterError):self.confirm(p)
    def test_original_profile_definition_change_also_blocks(self):
        p=self.choices();self.native.profiles[0]['upgradeAllowed']=True
        with self.assertRaises(AdapterError):self.confirm(p)
    def test_lost_reply_recovers_visible_effect_without_second_put(self):
        self.native.lost=True;op=self.confirm();self.assertEqual(self.profile.run(op['id'])['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'completed');self.assertEqual(len(self.native.calls),1)
    def test_lost_reply_without_effect_never_blindly_resubmits(self):
        self.native.lost=True;self.native.effect=False;op=self.confirm()
        self.assertEqual(self.profile.run(op['id'])['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'uncertain');self.assertEqual(len(self.native.calls),1)
    def test_another_profile_after_lost_reply_is_not_overwritten(self):
        self.native.lost=True;op=self.confirm();self.profile.run(op['id']);self.native.value['qualityProfileId']=4
        self.assertNotEqual(self.retry(op)['status'],'completed');self.assertEqual(len(self.native.calls),1)
    def test_movie_changed_after_confirmation_blocks_before_native_write(self):
        op=self.confirm();self.native.value['monitored']=True
        self.assertEqual(self.profile.run(op['id'])['status'],'blocked');self.assertEqual(self.native.calls,[])
    def test_catalogue_history_change_after_confirmation_blocks_before_write(self):
        op=self.confirm();self.cat.value['history'].append({'id':'new'})
        self.assertEqual(self.profile.run(op['id'])['status'],'blocked');self.assertEqual(self.native.calls,[])
    def test_marker_failure_cannot_repeat_successful_profile_save(self):
        original=self.cat.ensure_marker
        def lost():original();raise AdapterError('Reply lost')
        self.cat.ensure_marker=lost;op=self.confirm();self.assertEqual(self.profile.run(op['id'])['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'completed');self.assertEqual(len(self.native.calls),1)
    def test_household_competing_choices_are_rejected_when_one_is_pending(self):
        a=self.choices();b=self.choices(actor='telegram:user:2');self.confirm(a)
        with self.assertRaises(ValueError):self.confirm(b,actor='telegram:user:2')
    def test_shared_reservation_prevents_conflicting_movie_work(self):
        other=self.profile.journal.create_operation('other','tmdb:558','movie_acquisition',{'actor':'test','basis':'test'},{},['guard'])
        claim=self.profile.journal.claim_operation(other['id']);self.profile.journal.defer_operation(claim,'other_pending')
        op=self.confirm();self.assertEqual(self.profile.run(op['id'])['status'],'pending');self.assertEqual(self.native.calls,[])
    def test_source_change_after_confirmation_blocks(self):
        op=self.confirm();self.native.url='http://other-radarr'
        self.assertEqual(self.profile.run(op['id'])['status'],'blocked');self.assertEqual(self.native.calls,[])
    def test_archived_or_unregistered_movies_cannot_open_editor(self):
        self.cat.value['collections']['Archived']={}
        with self.assertRaises(ValueError):self.choices()
        self.cat.value=None
        with self.assertRaises(ValueError):self.choices()
    def test_profile_list_rejects_duplicates_or_bad_records(self):
        for profiles in ([self.native.profiles[0]]*2,[None],[]):
            self.native.profiles=profiles
            with self.assertRaises(AdapterError):self.choices()


class ProfileApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture=mini.MiniAppTests();self.fixture.setUp();self.addCleanup(self.fixture.client.close)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite';self.native=Native();self.cat=Catalogue()
        self.cat.register_library('558','Spider-Man 2','household','a'*32);self.cat.calls=[]
        self.config=replace(self.fixture.config,coordinator=replace(self.fixture.config.coordinator,enabled=True))
        self.factory=lambda config,storage:service(storage,self.native,self.cat)
        self.client=TestClient(create_app(self.config,database=self.path,service=self.fixture.service,profile_factory=self.factory));self.addCleanup(self.client.close)
        self.headers=self.fixture.headers;self.other={'Authorization':'tma '+mini.signed_data(2)}
        self.search=self.client.get('/api/search?q=Spider-Man',headers=self.headers).json()['search_id']
        self.url=f'/api/search/{self.search}/0/radarr-profile'
    def choices(self):return self.client.get(self.url,headers=self.headers)
    def confirm(self,p,**kwargs):
        return self.client.post('/api/radarr-profile/confirm',headers=kwargs.pop('headers',self.headers),json={
            'decision_id':p['decision_id'],'version':p['version'],'profile_id':3,'confirm':True,**kwargs})
    def test_auth_owner_and_disabled_coordinator(self):
        self.assertEqual(self.client.get(self.url).status_code,401)
        self.assertEqual(self.client.get(self.url,headers=self.other).status_code,404)
        off=TestClient(create_app(self.fixture.config,database=self.path,service=self.fixture.service,profile_factory=self.factory));self.addCleanup(off.close)
        search=off.get('/api/search?q=Movie',headers=self.headers).json()['search_id']
        self.assertEqual(off.get(f'/api/search/{search}/0/radarr-profile',headers=self.headers).status_code,404)
    def test_confirm_and_retry_return_safe_operation_without_native_paths(self):
        p=self.choices().json();self.assertNotIn('/movies',json.dumps(p))
        reply=self.confirm(p);self.assertEqual(reply.status_code,200)
        identifier=reply.json()['operation_id'];self.assertEqual(self.client.get('/api/radarr-profile/'+identifier,headers=self.headers).json()['status'],'completed')
        self.assertEqual(self.confirm(p).json()['operation_id'],identifier);self.assertEqual(len(self.native.calls),1)
        self.assertEqual(self.client.get('/api/radarr-profile/'+identifier,headers=self.other).status_code,404)
    def test_false_confirm_arbitrary_payload_and_boolean_profile_rejected(self):
        p=self.choices().json()
        for fields in ({'confirm':False},{'profile_id':True},{'monitored':True},{'rootFolderPath':'/other'}):
            self.assertEqual(self.confirm(p,**fields).status_code,422)
        self.assertEqual(self.native.calls,[])
    def test_pending_change_recovered_after_reload_and_not_duplicated(self):
        p=self.choices().json();self.native.effect=False;self.native.lost=True;reply=self.confirm(p)
        pending=self.choices().json();self.assertTrue(pending['busy']);self.assertEqual(pending['pending'][0]['operation_id'],reply.json()['operation_id'])
        self.assertEqual(len(self.native.calls),1)
