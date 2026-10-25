"""Removal hints must not delete history, trust an outage, or repeat uncertain writes."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from curatarr.adapters import AdapterError
from curatarr.config import ConfigError, parse_config
from curatarr.integrations.jellyfin.removals import JellyfinRemovalVerifier, removal_hint
from curatarr.integrations.radarr.removals import RadarrRemovalWriter
from curatarr.integrations.ryot.removals import RyotRemovalWriter
from curatarr.services.removals import MovieRemovals, RemovalStore
from curatarr.services.viewing_workflow import bot_key
from curatarr.storage import Storage, StorageError
from curatarr.storage.viewing import ViewingStore
from curatarr.web.app import create_app
from test_config import VALID_CONFIG
import test_miniapp as mini_fixture


ITEM = 'a' * 32
HINT = {'item_id': ITEM, 'title': 'Example', 'year': 2020, 'tmdb': '123', 'imdb': 'tt123', 'path': '/movies/Example/movie.mkv'}
PAYLOAD = {'Event': 'ItemRemoved', 'Item': {'Id': ITEM, 'Type': 'Movie', 'Name': 'Example', 'ProductionYear': 2020,
            'Path': HINT['path'], 'ProviderIds': {'Tmdb': '123', 'Imdb': 'tt123'}}}


class Jelly:
    outcome = 'absent'
    reappeared = False
    def verify(self, hint):
        if isinstance(self.outcome, Exception): raise self.outcome
        return self.outcome
    def present(self, hint): return self.reappeared


class Ryot:
    names = {k: k for k in ['library', 'archived', 'deleted', 'previously_acquired', 'radarr']}
    def __init__(self):
        self.value = {'id': 'movie', 'history': [{'id': 'seen'}], 'reviews': [{'id': 'review'}],
                      'collections': {k: {'collectionName': k} for k in ['library', 'radarr', 'previously_acquired']}}
        self.calls = []
        self.uncertain = False
        self.delay = False
    def find(self, hint): return deepcopy(self.value)
    def snapshot(self, identifier, hint): return deepcopy(self.value)
    def preserved(self, before, after):
        if before['history'] != after['history'] or before['reviews'] != after['reviews']:
            raise AdapterError('History changed')
    def change(self, snapshot, key, add):
        self.calls.append((key, add))
        if not self.delay:
            if add: self.value['collections'][key] = {'collectionName': key}
            else: self.value['collections'].pop(key, None)
        if self.uncertain:
            self.uncertain = False
            raise AdapterError('Lost reply')


class Arr:
    def __init__(self):
        self.value = {'id': 8, 'tmdbId': 123, 'imdbId': 'tt123', 'monitored': True,
                      'movieFileId': 9, 'hasFile': True, 'qualityProfileId': 2, 'path': '/movies/Example',
                      'movieFile': {'id': 9, 'size': 12345, 'relativePath': 'movie.mkv'}}
        self.calls = []
        self.busy = False
        self.uncertain = False
    def movie(self, hint): return deepcopy(self.value)
    def ensure_idle(self, movie):
        if self.busy: raise AdapterError('Downloading')
    def write(self, method, movie):
        self.calls.append(method)
        if method == 'DELETE': self.value = None
        else: self.value['monitored'] = True
        if self.uncertain:
            self.uncertain = False
            raise AdapterError('Lost reply')


class Telegram:
    token = 'bot-test'
    def __init__(self): self.sent = []; self.fail = False
    def send_prompt(self, chat_id, prompt, **kw):
        self.sent.append((chat_id, prompt, kw))
        if self.fail: raise AdapterError('Lost delivery reply')
        return {'message_id': len(self.sent)}


class RemovalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(':memory:'); self.storage.initialize(); self.addCleanup(self.storage.close)
        self.jelly, self.ryot, self.arr, self.telegram = Jelly(), Ryot(), Arr(), Telegram()
        self.service = MovieRemovals(self.storage, self.jelly, self.ryot, self.arr, self.telegram, 'source')
        ViewingStore(self.storage).bind({'bot_key': bot_key(self.telegram.token), 'chat_id': 7, 'enabled': True})
        self.identifier = self.service.store.enqueue(deepcopy(HINT), 'source', now=time.time()-301)
    def decision(self): return self.storage.get_pending_decision(self.identifier)
    def open(self):
        self.service.tick(); return self.decision()
    def answer(self, action='archive'):
        d=self.decision()
        return self.service.respond(f'remove:{d.decision_id}:confirm_{action}', 1, 7, d.context['delivery']['message_id'])
    def finish(self):
        for _ in range(9): self.service.tick()
    def test_webhook_only_creates_durable_hint_and_duplicate_is_same(self):
        self.assertEqual(self.service.store.enqueue(HINT, 'source'), self.identifier)
        self.assertEqual(self.telegram.sent, []); self.assertEqual(self.arr.calls, [])
        self.assertEqual(self.decision().status, 'open'); self.assertIsNone(self.decision().expires_at)
    def test_grace_period_prevents_early_prompt(self):
        d=self.decision();c=deepcopy(d.context);c['not_before']=time.time()+60;self.service.store.update(d,c,'test_delay')
        self.service.tick(); self.assertEqual(self.telegram.sent, [])
    def test_question_has_banner_and_separate_confirmation(self):
        self.open(); text=self.telegram.sent[0][1].text
        self.assertIn('MOVIE REMOVAL',text)
        p=self.service.respond(f'remove:{self.identifier}:archive',1,7,1)
        self.assertIn('Confirm',[b.label for b in p.buttons]); self.assertEqual(self.arr.calls,[])
    def test_archive_preserves_history_ratings_and_nonactive_memberships(self):
        before=deepcopy(self.ryot.value);self.open();self.answer();self.finish()
        self.assertEqual(self.decision().status,'resolved');self.assertIsNone(self.arr.value)
        self.assertEqual(self.arr.calls,['DELETE'])
        self.assertEqual(set(self.ryot.value['collections']),{'library','previously_acquired','archived','deleted'})
        self.assertEqual(self.ryot.value['history'],before['history']);self.assertEqual(self.ryot.value['reviews'],before['reviews'])
        self.assertEqual(len(self.ryot.calls),3)
    def test_keep_wanted_enables_monitoring_without_changing_profile(self):
        self.arr.value['monitored']=False;self.open();self.answer('wanted');self.finish()
        self.assertEqual(self.decision().context['phase'],'wanted');self.assertEqual(self.arr.calls,['PUT'])
        self.assertEqual(self.arr.value['qualityProfileId'],2);self.assertTrue(self.arr.value['monitored'])
        self.assertEqual(self.ryot.calls,[])
    def test_unmanaged_restore_blocked_before_saving_choice(self):
        self.arr.value=None;self.open()
        with self.assertRaises(AdapterError): self.answer('wanted')
        self.assertIsNone(self.decision().context['operation']);self.assertEqual(self.ryot.calls,[])
    def test_alternate_copy_or_rescan_does_not_archive(self):
        self.jelly.outcome='present';self.open()
        self.assertEqual(self.decision().status,'resolved');self.assertEqual(self.telegram.sent,[]);self.assertEqual(self.arr.calls,[])
    def test_storage_outage_and_active_upgrade_retain_hint_without_prompt(self):
        for failure in ['storage','download']:
            if failure=='storage':self.jelly.outcome=AdapterError('Offline')
            else:self.jelly.outcome='absent';self.arr.busy=True
            d=self.decision();c=deepcopy(d.context);c['retry_after']=0;self.service.store.update(d,c,'test_retry')
            self.service.tick();self.assertEqual(self.decision().status,'open');self.assertEqual(self.telegram.sent,[])
            self.assertTrue(self.decision().context['needs_review'])
    def test_copy_reappearing_before_confirmation_closes_without_writes(self):
        self.open();self.jelly.outcome='present';self.answer()
        self.assertEqual(self.decision().context['phase'],'copy_present');self.assertEqual(self.ryot.calls,[])
    def test_new_arr_file_or_profile_change_blocks_stale_confirmation(self):
        self.open();self.arr.value['movieFileId']=99
        with self.assertRaises(AdapterError):self.answer()
        self.assertEqual(self.arr.calls,[]);self.assertIsNone(self.decision().context['operation'])
    def test_history_change_blocks_archive(self):
        self.open();self.ryot.value['history'].append({'id':'new'})
        with self.assertRaises(AdapterError):self.answer()
        self.assertEqual(self.arr.calls,[])
    def test_uncertain_async_job_is_read_back_and_never_resubmitted(self):
        self.open();self.ryot.uncertain=True
        with self.assertRaises(AdapterError):self.answer()
        self.finish();self.assertEqual(self.decision().status,'resolved')
        self.assertEqual(self.ryot.calls.count(('archived',True)),1)
    def test_accepted_but_unapplied_job_stays_open_without_repeat_write(self):
        self.open();self.ryot.delay=True;self.answer();self.finish()
        self.assertEqual(self.decision().status,'open');self.assertEqual(self.ryot.calls,[('archived',True)])
        self.assertEqual(self.arr.calls,[])
    def test_uncertain_radarr_delete_is_read_back_without_second_delete(self):
        self.open();self.arr.uncertain=True;self.answer();self.finish()
        # Deferred failures retry after five minutes; permit deterministic recovery.
        d=self.decision();c=deepcopy(d.context);c['retry_after']=0;self.service.store.update(d,c,'test_retry');self.finish()
        self.assertEqual(self.arr.calls,['DELETE']);self.assertEqual(self.decision().status,'resolved')
    def test_wrong_chat_or_message_cannot_confirm(self):
        self.open()
        for chat,msg in [(8,1),(7,99)]:
            with self.assertRaises(StorageError):self.service.respond(f'remove:{self.identifier}:confirm_archive',1,chat,msg)
        self.assertEqual(self.ryot.calls,[])
    def test_first_household_confirmation_wins(self):
        self.open();self.answer();self.answer('wanted');self.finish()
        self.assertEqual(self.decision().context['choice'],'archive');self.assertEqual(self.arr.calls,['DELETE'])
    def test_lost_telegram_reply_is_not_automatically_sent_again(self):
        self.telegram.fail=True;self.open();self.finish();self.assertEqual(len(self.telegram.sent),1)
        self.assertEqual(self.decision().context['delivery']['state'],'uncertain')
    def test_reopen_is_explicit_and_invalidates_old_message_buttons(self):
        self.open();self.service.reopen(7)
        self.assertEqual(len(self.telegram.sent),2)
        with self.assertRaises(StorageError):self.service.respond(f'remove:{self.identifier}:confirm_archive',1,7,1)
    def test_completed_event_rearms_only_after_item_reappears(self):
        self.jelly.outcome='present';self.open()
        self.assertEqual(self.service.store.enqueue(HINT,'source'),self.identifier)
        self.jelly.reappeared=True;self.service.tick()
        self.assertNotEqual(self.service.store.enqueue(HINT,'source'),self.identifier)
    def test_unknown_source_never_prompts(self):
        self.service.source='different';self.open();self.assertEqual(self.telegram.sent,[])


class RemovalVerifierTests(unittest.TestCase):
    def fixture(self, physical=True, missing=False, alternate=False):
        def get(path,params):
            if path=='/Items':
                if params.get('Ids'):return {'TotalRecordCount':0,'Items':[]}
                rows=[{'Id':'b'*32,'ProviderIds':{'Tmdb':'999'},'MediaSources':[{'Path':'/movies/Anchor/anchor.mkv'}]}]
                if alternate:rows.append({'Id':'c'*32,'ProviderIds':{'Tmdb':'123'}})
                return {'TotalRecordCount':len(rows),'Items':rows}
            if path=='/Library/VirtualFolders':return [{'Locations':['/movies']}]
            if path=='/Environment/DirectoryContents':
                if missing:return []  # A readable empty mount still must not prove deletion.
                if params['path']=='/movies':return [{'Path':'/movies/Anchor','Type':'Directory'}]+([{'Path':'/movies/Example','Type':'Directory'}] if physical else [])
                if params['path']=='/movies/Anchor':return [{'Path':'/movies/Anchor/anchor.mkv','Type':'File'}]
                if params['path']=='/movies/Example':return [{'Path':'/movies/Example/movie.mkv','Type':'File'}]
            raise AssertionError((path,params))
        return JellyfinRemovalVerifier('http://jelly','key',get)
    def test_actual_absence_requires_anchor_on_available_root(self):self.assertEqual(self.fixture(physical=False).verify(HINT),'absent')
    def test_index_only_removal_with_file_present_does_not_archive(self):self.assertEqual(self.fixture().verify(HINT),'present')
    def test_empty_or_offline_mount_never_proves_deletion(self):
        with self.assertRaises(AdapterError):self.fixture(missing=True).verify(HINT)
    def test_alternate_version_never_archives_title(self):self.assertEqual(self.fixture(alternate=True).verify(HINT),'present')
    def test_wrong_root_rejected(self):
        with self.assertRaises(AdapterError):self.fixture().verify({**HINT,'path':'/outside/file.mkv'})
    def test_parser_ignores_nonmovie_and_rejects_weak_identity_and_paths(self):
        self.assertEqual(removal_hint(PAYLOAD),HINT)
        self.assertIsNone(removal_hint({**PAYLOAD,'Event':'Play'}))
        for changes in [{'ProviderIds':{}},{'Path':'/movies/../outside/file.mkv'},{'Id':'x'}]:
            with self.assertRaises((ValueError,AdapterError)):removal_hint({**PAYLOAD,'Item':{**PAYLOAD['Item'],**changes}})


class RemovalEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        fixture=mini_fixture.MiniAppTests();fixture.setUp()
        self.config=replace(fixture.config,jellyfin=replace(fixture.config.jellyfin,removal_webhook_token='x'*43))
        self.database=Path(self.temp.name)/'state.sqlite'
        self.client=TestClient(create_app(self.config,database=self.database,service=fixture.service))
        self.url='/hooks/jellyfin/removals?token='+'x'*43
    def test_machine_auth_is_separate_from_telegram_auth(self):
        self.assertEqual(self.client.post('/hooks/jellyfin/removals',json=PAYLOAD,headers={'Authorization':'tma '+mini_fixture.signed_data()}).status_code,404)
        self.assertEqual(self.client.post(self.url,json=PAYLOAD).status_code,202)
        self.assertEqual(self.client.post(self.url,json=PAYLOAD).status_code,202)
        storage=Storage(self.database);self.addCleanup(storage.close)
        self.assertEqual(len(RemovalStore(storage).decisions()),1)
    def test_invalid_and_oversized_payloads_cannot_enqueue(self):
        self.assertEqual(self.client.post(self.url,content=b'x'*262145).status_code,413)
        self.assertEqual(self.client.post(self.url,content=b'bad-json').status_code,400)
        self.assertEqual(self.client.post(self.url,json={'Event':'Play'}).json()['queued'],False)
    def test_token_requires_high_entropy_format_and_is_not_in_config_repr(self):
        data=yaml.safe_load(VALID_CONFIG);data['jellyfin']['removal_webhook_token']='short'
        with self.assertRaises(ConfigError):parse_config(data)
        self.assertNotIn('x'*43,repr(self.config.jellyfin))


class NativeRetentionAdapterTests(unittest.TestCase):
    def test_radarr_duplicate_or_conflicting_identity_is_blocked(self):
        class Client:
            def movies(self):return [{'id':1,'tmdbId':123,'imdbId':'tt999','monitored':True}]
        with self.assertRaises(AdapterError):RadarrRemovalWriter(Client()).movie(HINT)
    def test_active_download_is_blocked_even_when_queue_has_no_warning(self):
        class Client:
            def _get(self,path,params):return {'totalRecords':1,'records':[{'id':6,'movieId':8,'status':'downloading'}]}
        with self.assertRaises(AdapterError):RadarrRemovalWriter(Client()).ensure_idle({'id':8})
    def test_incomplete_queue_does_not_establish_idle(self):
        class Client:
            def _get(self,path,params):return {'totalRecords':1,'records':[]}
        with self.assertRaises(AdapterError):RadarrRemovalWriter(Client()).ensure_idle({'id':8})
    def test_ryot_checks_account_collection_ownership(self):
        from curatarr.integrations.ryot.adapter import RyotStateCollections
        class Client:
            def execute(self,query,variables=None):
                return {'userDetails':{'id':'mine'},'userCollectionsList':{'response':[{'name':n,'id':n,'isDefault':False,'creator':{'id':'other'}} for n in ['Library','Archived','Deleted','Previously acquired','Radarr']]}}
        class Reader:client=Client();collections=RyotStateCollections()
        with self.assertRaises(AdapterError):RyotRemovalWriter(Reader()).account()
    def test_ryot_preserves_all_unrelated_memberships_and_existing_history(self):
        from curatarr.integrations.ryot.adapter import RyotStateCollections
        class Reader:client=None;collections=RyotStateCollections()
        adapter=RyotRemovalWriter(Reader())
        before={'account':{'id':'mine'},'metadata':{'id':'movie'},'history':[{'id':'seen'}],'reviews':[{'id':'review'}],
                'collections':{'Library':{'id':'lib'},'Previously acquired':{'id':'acq'},'Custom':{'id':'custom'}}}
        after=deepcopy(before);after['collections']['Archived']={'id':'archive'};adapter.preserved(before,after)
        for field in ['history','reviews','collections']:
            after=deepcopy(before)
            if field=='collections':after[field].pop('Custom')
            else:after[field]=[]
            with self.assertRaises(AdapterError):adapter.preserved(before,after)
