"""Acquisition transport must bind search ownership, confirmation and safe public results."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from curatarr.services.acquisition import MovieAcquisition
from curatarr.storage import Storage
from curatarr.web.app import create_app
import test_miniapp as mini
from test_movie_acquisition import Catalogue, Arr


class AcquisitionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        fixture=mini.MiniAppTests();fixture.setUp();self.fixture=fixture
        self.config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True))
        self.path=Path(self.temp.name)/'state.sqlite';self.catalogue,self.arr=Catalogue(),Arr()
        def factory(config,storage):return MovieAcquisition(storage,self.catalogue,self.arr)
        self.factory=factory
        self.client=TestClient(create_app(self.config,database=self.path,service=fixture.service,acquisition_factory=factory))
        self.headers=fixture.headers
        data=self.client.get('/api/search?q=example',headers=self.headers).json();self.search=data['search_id']
    def preview(self,mode='add_search',headers=None):
        return self.client.post(f'/api/search/{self.search}/0/acquisition-preview',headers=headers or self.headers,
                                json={'mode':mode,**({'profile_id':2,'root_id':1} if mode=='add_search' else {})})
    def confirm(self,p,headers=None):
        return self.client.post('/api/acquisition/confirm',headers=headers or self.headers,json={**{k:p[k] for k in ['decision_id','version']},'confirm':True})
    def test_catalogue_viewing_input_is_authenticated_validated_and_journaled(self):
        from curatarr.services.catalogue_watch import CatalogueWatch
        from test_catalogue_watch import Completion, Client
        from test_direct_viewing import Native
        self.catalogue.client = Client(self.catalogue)
        completion = Completion(self.catalogue)
        def factory(config, storage):
            return MovieAcquisition(storage, self.catalogue, self.arr,
                CatalogueWatch(storage, self.catalogue, Native(self.catalogue), completion))
        self.client = TestClient(create_app(self.config, database=self.path, service=self.fixture.service, acquisition_factory=factory))
        data = self.client.get('/api/search?q=Example', headers=self.headers).json()
        url = f"/api/search/{data['search_id']}/0/acquisition-preview"
        for invalid in ({'rating': 6}, {'rating': True}, {'watched': 'yes'}, {'watched_on': 'bad'}):
            self.assertEqual(self.client.post(url, headers=self.headers, json={'mode':'catalogue_only', **invalid}).status_code, 422)
        response = self.client.post(url, headers=self.headers, json={'mode':'catalogue_only', 'watched':True, 'rating':4})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.catalogue.calls, [])
        result = self.confirm(response.json()); self.assertEqual(result.status_code, 200)
        op = self.client.get('/api/acquisition/' + result.json()['operation_id'], headers=self.headers).json()
        self.assertEqual(op['status'], 'completed'); self.assertEqual(self.arr.calls, [])
        self.assertEqual(self.catalogue.value['history'][0]['reviewId'], 'review-new')
        self.assertIn('Rated by Test', self.catalogue.value['reviews'][0]['textOriginal'])

    def test_machine_webhook_auth_does_not_authorize_user_acquisition(self):
        self.assertEqual(self.client.get('/api/acquisition/options').status_code,401)
    def test_native_options_do_not_expose_paths_or_select_an_invalid_default(self):
        result=self.client.get('/api/acquisition/options',headers=self.headers).json()
        self.assertEqual(result['profiles'],[{'id':2,'name':'Native profile'}]);self.assertIsNone(result['default_profile'])
        self.assertEqual(result['roots'],[{'id':1,'name':'movies'}]);self.assertNotIn('path',str(result))
    def test_catalogue_only_and_search_are_separate_verified_actions(self):
        preview=self.preview('catalogue_only');self.assertEqual(preview.status_code,200)
        self.assertEqual(self.catalogue.calls,[]);response=self.confirm(preview.json());self.assertEqual(response.status_code,200)
        opid=response.json()['operation_id'];result=self.client.get('/api/acquisition/'+opid,headers=self.headers)
        self.assertEqual(result.json()['status'],'completed');self.assertEqual(self.arr.calls,[])
        self.assertNotIn('expected',result.json());self.assertNotIn('authorization',result.json())
    def test_search_ownership_and_operation_ownership_are_enforced(self):
        other={'Authorization':'tma '+mini.signed_data(2)}
        self.assertEqual(self.preview(headers=other).status_code,404)
        p=self.preview().json();self.assertEqual(self.confirm(p,other).status_code,409)
        opid=self.confirm(p).json()['operation_id']
        self.assertEqual(self.client.get('/api/acquisition/'+opid,headers=other).status_code,404)
    def test_repeat_confirmation_is_idempotent_and_does_not_repeat_search(self):
        p=self.preview().json();self.assertEqual(self.confirm(p).status_code,200);self.assertEqual(self.confirm(p).status_code,200)
        self.assertEqual(self.arr.calls,['add','search'])
    def test_client_cannot_supply_native_paths_payloads_or_skip_explicit_confirmation(self):
        url=f'/api/search/{self.search}/0/acquisition-preview'
        self.assertEqual(self.client.post(url,headers=self.headers,json={'mode':'add_search','profile_id':2,'root_id':1,'rootFolderPath':'/unsafe'}).status_code,422)
        p=self.preview().json();self.assertEqual(self.client.post('/api/acquisition/confirm',headers=self.headers,json={'decision_id':p['decision_id'],'version':p['version'],'confirm':False}).status_code,422)
        self.assertEqual(self.arr.calls,[])
    def test_disabled_coordinator_cannot_execute_acquisition(self):
        client=TestClient(create_app(self.fixture.config,database=self.path,service=self.fixture.service,acquisition_factory=self.factory))
        self.assertEqual(client.get('/api/acquisition/options',headers=self.headers).status_code,404)
        self.assertEqual(self.arr.calls,[])
    def test_pending_outcomes_are_recoverable_after_page_reload(self):
        self.arr.lost=True;p=self.preview().json();self.confirm(p)
        # Fixture candidate is tmdb:558; query is scoped to this user/movie.
        result=self.client.get('/api/acquisition/pending?tmdb=558',headers=self.headers)
        self.assertEqual(result.status_code,200);self.assertEqual(len(result.json()['operations']),1)
        self.assertEqual(result.json()['operations'][0]['status'],'uncertain')
    def test_unconfirmed_operation_is_in_technical_maintenance_not_a_repair_button(self):
        self.arr.lost=True;p=self.preview().json();self.confirm(p)
        from curatarr.services.system_maintenance import SystemMaintenance
        s=Storage(self.path);self.addCleanup(s.close)
        monitor=SystemMaintenance(s,None,None,None)
        self.assertTrue(any(i['kind']=='coordination_operation_unconfirmed' for i in monitor.local_issues()))
