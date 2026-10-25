"""Acquisition is a saved, versioned intent; catalogue-only cannot start downloads."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from curatarr.adapters import AdapterError
from curatarr.integrations.radarr.acquisition import RadarrAcquisitionWriter
from curatarr.services.acquisition import MovieAcquisition
from curatarr.storage import Storage, StorageError


class Catalogue:
    marker='Curatarr'
    reader=SimpleNamespace(collections=SimpleNamespace(library='Library',radarr='Radarr',archived='Archived'))
    def __init__(self):self.value=None;self.calls=[];self.marked=False;self.lost=False;self.partial=False
    def snapshot(self,tmdb):return {'account_id':'household','movie':deepcopy(self.value)}
    def preserved(self,before,after,allow_reactivate=False):
        old,new=before['movie'],after['movie']
        if new is None or old and (old['history']!=new['history'] or old['reviews']!=new['reviews']):raise AdapterError('History changed')
    def register_library(self,tmdb,title,account,operation):
        self.calls.append('register')
        self.value={'metadata':{'id':'movie','title':title,'identifier':tmdb,'source':'TMDB','lot':'MOVIE','isPartial':self.partial},
                    'collections':{'Library':{}},'history':[],'reviews':[]}
        if self.lost:self.lost=False;raise AdapterError('Import reply lost')
        return {'import_accepted':True}
    def refresh_metadata(self,snapshot):self.calls.append('refresh');self.value['metadata']['isPartial']=False;return {'refresh_accepted':True}
    def account(self):return {'id':'household','collections':{'Curatarr':{}} if self.marked else {}}
    def ensure_marker(self):self.calls.append('marker');self.marked=True;return {'collection_id':'marker'}
    def membership(self,snapshot,name,add):
        self.calls.append((name,add))
        if add:self.value['collections'][name]={}
        else:self.value['collections'].pop(name,None)
        return {'accepted':True}


class Arr:
    def __init__(self):self.value=None;self.calls=[];self.profile={'id':2,'name':'Native profile'};self.root={'id':1,'path':'/movies'};self.exclude=False;self.lost=False;self.commands={}
    def options(self):return {"profiles":[deepcopy(self.profile)],"roots":[deepcopy(self.root)]}
    def movie(self,tmdb,imdb=None):return deepcopy(self.value)
    def selection(self,profile,root):
        if profile!=self.profile['id'] or root!=self.root['id']:raise AdapterError('Choose native options')
        return deepcopy(self.profile),deepcopy(self.root)
    def excluded(self,tmdb):return self.exclude
    def add(self,tmdb,imdb,profile,root):
        self.calls.append('add');self.value={'id':10,'tmdbId':int(tmdb),'imdbId':imdb,'qualityProfileId':profile['id'],'monitored':True,'path':root['path']+'/Example'}
        return {'movie_id':10}
    def search(self,movie):
        self.calls.append('search');self.commands[8]=movie['id']
        if self.lost:raise AdapterError('Search reply lost')
        return {'command_id':8}
    def command(self,identifier,movie):
        if self.commands.get(identifier)!=movie:raise AdapterError('Command identity mismatch')
        return {'command_id':identifier,'status':'queued'}


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.s=Storage(':memory:');self.s.initialize();self.addCleanup(self.s.close)
        self.cat,self.arr=Catalogue(),Arr();self.service=MovieAcquisition(self.s,self.cat,self.arr)
        self.candidate={'tmdb':'123','imdb':'tt123','title':'Example','year':2020}
        self.now=1000;self.service.journal.clock=lambda:self.now
    def preview(self,mode='add_search',**kwargs):
        return self.service.preview(self.candidate,actor='telegram:1',mode=mode,
                                    **({'profile_id':2,'root_id':1} if mode=='add_search' else {}),**kwargs)
    def confirm(self,preview=None):
        p=preview or self.preview();return self.service.confirm(p['decision_id'],actor='telegram:1',version=p['version'])
    def test_preview_and_confirmation_do_not_execute_native_changes(self):
        p=self.preview();self.confirm(p)
        self.assertEqual(self.cat.calls,[]);self.assertEqual(self.arr.calls,[])
    def test_catalogue_only_never_adds_arr_or_searches_or_records_acquisition(self):
        op=self.confirm(self.preview('catalogue_only'));result=self.service.run(op['id'])
        self.assertEqual(result['status'],'completed');self.assertEqual(self.arr.calls,[])
        self.assertEqual(set(self.cat.value['collections']),{'Library','Curatarr'})
        self.assertEqual(self.cat.value['history'],[]);self.assertEqual(self.cat.value['reviews'],[])
    def test_add_and_search_runs_once_and_marks_only_after_native_steps(self):
        p=self.preview();op=self.confirm(p);result=self.service.run(op['id'])
        self.assertEqual(result['status'],'completed');self.assertEqual(self.arr.calls,['add','search'])
        self.assertEqual(set(self.cat.value['collections']),{'Library','Radarr','Curatarr'})
        self.assertEqual(self.confirm(p)['id'],op['id']);self.service.run(op['id']);self.assertEqual(self.arr.calls,['add','search'])
    def test_explicit_catalogue_only_cannot_smuggle_in_a_download_profile(self):
        with self.assertRaises(ValueError):self.service.preview(self.candidate,actor='telegram:1',mode='catalogue_only',profile_id=2)
        self.assertEqual(self.arr.calls,[])
    def test_stale_profile_preview_cannot_be_confirmed(self):
        p=self.preview();self.arr.profile['name']='Changed'
        with self.assertRaises(AdapterError):self.confirm(p)
        self.assertEqual(self.cat.calls,[])
    def test_preview_owned_by_another_user_or_changed_version_cannot_confirm(self):
        p=self.preview()
        for actor,version in [('telegram:2',p['version']),('telegram:1','stale')]:
            with self.assertRaises(StorageError):self.service.confirm(p['decision_id'],actor=actor,version=version)
        self.assertEqual(self.cat.calls,[])
    def test_active_movie_and_native_exclusion_block_new_acquisition(self):
        self.arr.exclude=True
        with self.assertRaises(ValueError):self.preview()
        self.arr.exclude=False;self.arr.add('123','tt123',self.arr.profile,self.arr.root)
        with self.assertRaises(ValueError):self.preview()
    def test_archived_movie_requires_explicit_reactivation(self):
        self.cat.register_library('123','Example','household','0'*32);self.cat.value['collections']['Archived']={}
        with self.assertRaises(ValueError):self.preview()
        p=self.preview(reactivate=True);op=self.confirm(p);self.assertEqual(self.service.run(op['id'])['status'],'completed')
        self.assertNotIn('Archived',self.cat.value['collections'])
    def test_existing_history_and_reviews_preserved(self):
        self.cat.register_library('123','Example','household','0'*32);self.cat.value['history']=[{'id':'old'}];self.cat.value['reviews']=[{'id':'rating'}]
        before=deepcopy(self.cat.value);op=self.confirm();self.assertEqual(self.service.run(op['id'])['status'],'completed')
        for field in ['history','reviews']:self.assertEqual(self.cat.value[field],before[field])
    def test_lost_import_reply_is_reconciled_without_second_import(self):
        self.cat.lost=True;op=self.confirm();self.assertEqual(self.service.run(op['id'])['status'],'uncertain')
        self.now+=301;self.assertEqual(self.service.run(op['id'])['status'],'completed');self.assertEqual(self.cat.calls.count('register'),1)
    def test_lost_search_reply_never_issues_a_second_search_or_claims_complete(self):
        self.arr.lost=True;op=self.confirm();self.assertEqual(self.service.run(op['id'])['status'],'uncertain')
        self.now+=301;self.assertEqual(self.service.run(op['id'])['status'],'uncertain')
        self.assertEqual(self.arr.calls,['add','search']);self.assertNotIn('Curatarr',self.cat.value['collections'])
    def test_partial_metadata_is_refreshed_before_acquisition(self):
        self.cat.partial=True;op=self.confirm();result=self.service.run(op['id'])
        self.assertEqual(result['status'],'completed');self.assertIn('refresh',self.cat.calls)
        self.assertFalse(self.cat.value['metadata']['isPartial'])
    def test_two_confirmed_previews_cannot_duplicate_the_same_acquisition(self):
        p1,p2=self.preview(),self.preview();a,b=self.confirm(p1),self.confirm(p2)
        self.assertEqual(self.service.run(a['id'])['status'],'completed')
        self.assertEqual(self.service.run(b['id'])['status'],'blocked')
        self.assertEqual(self.arr.calls,['add','search'])
    def test_unrelated_history_change_during_job_halts_remaining_writes(self):
        self.cat.lost=True;op=self.confirm();self.service.run(op['id']);self.cat.value['history']=[{'id':'new-manual-watch'}]
        # New association originally had no history; native preservation port must
        # reject this in production. Inject that preservation check into the fake.
        def preserve(before,after,allow_reactivate=False):
            if before['movie'] is None and after['movie']['history']:raise AdapterError('New unrelated history')
        self.cat.preserved=preserve;self.now+=301
        self.assertEqual(self.service.run(op['id'])['status'],'uncertain');self.assertEqual(self.arr.calls,[])


class RadarrWriterTests(unittest.TestCase):
    def client(self):
        class Client:
            url='http://native';api_key='secret'
            def movies(self):return []
            def _get(self,path,params):
                if path.endswith('qualityprofile'):return [{'id':2,'name':'Native'}]
                if path.endswith('rootfolder'):return [{'id':1,'path':'/movies'}]
                if path.endswith('exclusions'):return []
                if path.endswith('lookup'):return [{'tmdbId':123,'imdbId':'tt123','title':'Example','titleSlug':'example-123','year':2020,'images':[]}]
                raise AssertionError(path)
        return Client()
    def test_native_add_never_searches_implicitly(self):
        calls=[]
        def post(path,payload):calls.append((path,payload));return {'id':8,'tmdbId':123}
        writer=RadarrAcquisitionWriter(self.client(),post)
        writer.add('123','tt123',{'id':2,'name':'Native'},{'id':1,'path':'/movies'})
        self.assertEqual(calls[0][1]['addOptions'],{'searchForMovie':False})
        self.assertEqual(calls[0][1]['rootFolderPath'],'/movies')
    def test_root_payload_cannot_override_the_selected_native_root(self):
        calls=[];writer=RadarrAcquisitionWriter(self.client(),lambda *args:calls.append(args))
        with self.assertRaises(AdapterError):writer.add('123','tt123',{'id':2,'name':'Native'},{'id':1,'path':'/elsewhere'})
        self.assertEqual(calls,[])


class CatalogueBoundaryTests(unittest.TestCase):
    def writer(self):
        from curatarr.integrations.ryot.adapter import RyotStateCollections
        from curatarr.integrations.ryot.catalogue import RyotCatalogueWriter
        class Client:
            url='https://native/backend/graphql';api_key='never-print'
            def __init__(self):self.calls=[]
            def execute(self,query,variables=None):
                self.calls.append((query,variables))
                if 'CuratarrCatalogueAccount' in query:
                    return {'userDetails':{'id':'household'},'userCollectionsList':{'response':[
                        {'id':name,'name':name,'isDefault':False,'creator':{'id':'household'}} for name in ['Library','Radarr','Archived']]}}
                if 'CuratarrRegisterCatalogue' in query:return {'deployImportJob':True}
                if 'CuratarrCreateMarker' in query:return {'createOrUpdateCollection':{'id':'marker'}}
                raise AssertionError(query)
        client=Client();reader=SimpleNamespace(client=client,collections=RyotStateCollections())
        payloads=[]
        def upload(payload,operation):payloads.append(json.loads(payload));return '/native/temp/import.json'
        return RyotCatalogueWriter(reader,upload),client,payloads
    def test_exact_library_import_has_no_watch_review_or_other_lifecycle_payload(self):
        writer,client,payloads=self.writer();writer.register_library('123','Example','household','a'*32)
        item=payloads[0]['metadata'][0]
        self.assertEqual((item['lot'],item['source'],item['identifier']),('movie','tmdb','123'))
        self.assertEqual(item['seen_history'],[]);self.assertEqual(item['reviews'],[])
        self.assertEqual([c['collection_name'] for c in item['collections']],['Library'])
        self.assertNotIn('Radarr',json.dumps(payloads));self.assertNotIn('CuratarrCreateMarker',[q for q,_ in client.calls])
    def test_wrong_account_or_untyped_identifier_never_uploads(self):
        writer,client,payloads=self.writer()
        for tmdb,account in [('123','other'),('123/evil','household')]:
            with self.assertRaises(AdapterError):writer.register_library(tmdb,'Example',account,'a'*32)
        self.assertEqual(payloads,[])
    def test_provenance_collection_is_not_a_native_or_lifecycle_alias(self):
        from curatarr.integrations.ryot.catalogue import RyotCatalogueWriter
        writer,_,_=self.writer()
        for name in ['Owned','Custom','Library','Archived']:
            with self.assertRaises(ValueError):RyotCatalogueWriter(writer.reader,marker=name)
    def test_initial_new_record_preservation_rejects_invented_history_or_flags(self):
        writer,_,_=self.writer();before={'account_id':'household','movie':None}
        after={'account_id':'household','movie':{'metadata':{'id':'movie'},'history':[],'reviews':[],'collections':{'Library':{}}}}
        writer.preserved(before,after)
        for field in ['history','reviews','collections']:
            bad=deepcopy(after)
            if field=='collections':bad['movie'][field]['Previously acquired']={}
            else:bad['movie'][field]=[{'id':'unexpected'}]
            with self.assertRaises(AdapterError):writer.preserved(before,bad)
