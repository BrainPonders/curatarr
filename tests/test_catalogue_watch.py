"""Past catalogue registrations preserve native history and recover exact effects."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from curatarr.adapters import AdapterError
from curatarr.services.acquisition import MovieAcquisition
from curatarr.services.catalogue_watch import CatalogueWatch, CatalogueCompletion
from curatarr.storage import Storage
from curatarr.storage.viewing import ViewingStore
from test_movie_acquisition import Catalogue, Arr
from test_direct_viewing import Native


class Completion(CatalogueCompletion):
    def __init__(self, cat):
        super().__init__(cat); self.calls=[]; self.lost=False; self.effect=True; self.hook=None
    def create(self, snapshot, operation, finished_on):
        self.calls.append(operation)
        if self.effect:
            self.catalogue.value['history'].append({'id':'seen-new','metadataId':'movie','userId':'household',
                'state':'COMPLETED','progress':'100','finishedOn':finished_on,'reviewId':None,'providersConsumedOn':[self.marker(operation)]})
            self.catalogue.value['collections']['Completed']={}
        if self.hook:self.hook()
        if self.lost:raise AdapterError('Lost response')
        return {'accepted':True}


class Client:
    def __init__(self,cat):self.cat=cat;self.calls=[];self.lost=None;self.effect=True;self.rating_factor=20
    def execute(self,query,variables):
        value=variables['input'];self.calls.append(deepcopy(value))
        if 'createOrUpdateReview' in query:
            if self.effect:
                self.cat.value['reviews'].append({'id':'review-new','rating':str(int(value['rating'])*self.rating_factor),
                    'textOriginal':value['text'],'visibility':'PRIVATE','postedBy':{'id':'household'}})
            if self.lost=='review':raise AdapterError('Lost response')
            return {'createOrUpdateReview':{'id':'review-new'}}
        if self.effect:
            next(e for e in self.cat.value['history'] if e['id']==value['seenId'])['reviewId']=value['reviewId']
        if self.lost=='link':raise AdapterError('Lost response')
        return {'updateSeenItem':True}


class CatalogueWatchTests(unittest.TestCase):
    def setUp(self):
        self.s=Storage(':memory:');self.s.initialize();self.addCleanup(self.s.close)
        self.cat=Catalogue();self.cat.client=Client(self.cat);self.arr=Arr();self.native=Native(self.cat)
        self.completion=Completion(self.cat);self.watch=CatalogueWatch(self.s,self.cat,self.native,self.completion)
        self.service=MovieAcquisition(self.s,self.cat,self.arr,self.watch);self.now=1000;self.service.journal.clock=lambda:self.now
        self.candidate={'tmdb':'123','title':'Example','year':2020}
    def preview(self,**kwargs):
        return self.service.preview(self.candidate,actor='telegram:user:1',mode='catalogue_only',watched=True,user_name='Alice',**kwargs)
    def confirm(self,p=None):
        p=p or self.preview();return self.service.confirm(p['decision_id'],actor='telegram:user:1',version=p['version'])
    def execute(self,op):return self.service.run(op['id'])
    def retry(self,op):self.now+=301;return self.execute(op)
    def existing(self,review=False):
        self.cat.register_library('123','Example','household','0'*32)
        self.cat.value['history']=[{'id':'old','metadataId':'movie','userId':'household','state':'COMPLETED','progress':'100',
            'finishedOn':'2020-01-02T12:34:00+00:00','reviewId':'old-review' if review else None,'providersConsumedOn':['Trakt']}]
        if review:self.cat.value['reviews']=[{'id':'old-review','rating':'80','visibility':'PRIVATE','textOriginal':'Existing review','postedBy':{'id':'household'}}]
    def test_unknown_date_creates_one_watch_without_download_or_prompt(self):
        store=ViewingStore(self.s);store.observe(self.native.account(),[],participants=[1,2])
        op=self.confirm();self.assertEqual(self.cat.calls,[])
        def observe():
            account,events=self.native.scan();self.assertEqual(store.observe(account,events,participants=[1,2])['created'],[])
        self.completion.hook=observe
        self.assertEqual(self.execute(op)['status'],'completed')
        self.assertIsNone(self.cat.value['history'][0]['finishedOn']);self.assertEqual(self.arr.calls,[])
        self.assertEqual(self.s.viewing_followups(),())
        account,events=self.native.scan();self.assertEqual(store.observe(account,events,participants=[1,2])['created'],[])
        self.execute(op);self.assertEqual(len(self.completion.calls),1)
    def test_date_and_rating_are_explicit_and_linked_with_readable_attribution(self):
        op=self.confirm(self.preview(watched_on='2020-01-02',rating=4))
        self.assertEqual(self.execute(op)['status'],'completed')
        event=self.cat.value['history'][0];self.assertEqual(event['finishedOn'],'2020-01-02T00:00:00+00:00')
        self.assertEqual(event['reviewId'],'review-new');self.assertEqual(self.cat.value['reviews'][0]['rating'],'80')
        self.assertIn('Rated by Alice',self.cat.value['reviews'][0]['textOriginal'])
        self.assertEqual(self.cat.client.calls[0]['rating'],'4');self.assertEqual(self.arr.calls,[])
    def test_hundred_scale_input_is_normalized_and_current_scale_is_fenced(self):
        self.native.account=lambda: {'id':'household','source':'native-source','scale':'OUT_OF_HUNDRED'}
        self.cat.client.rating_factor=1
        op=self.confirm(self.preview(rating=4));self.assertEqual(self.execute(op)['status'],'completed')
        self.assertEqual(self.cat.client.calls[0]['rating'],'80')
        self.assertEqual(self.cat.value['reviews'][0]['rating'],'80')

    def test_lost_review_with_no_visible_effect_is_not_submitted_again(self):
        self.cat.client.lost='review';self.cat.client.effect=False
        op=self.confirm(self.preview(rating=3));self.assertEqual(self.execute(op)['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'uncertain');self.assertEqual(len(self.cat.client.calls),1)

    def test_account_scale_change_after_preview_blocks_confirmation(self):
        p=self.preview(rating=4)
        self.native.account=lambda: {'id':'household','source':'native-source','scale':'OUT_OF_HUNDRED'}
        with self.assertRaises(AdapterError):self.confirm(p)
        self.assertEqual(self.cat.calls,[])

    def test_pending_history_reset_blocks_before_any_native_effect(self):
        with self.s.connection:
            self.s.connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('tmdb:123','viewing_reset','','{\"state\":\"pending\"}')")
        with self.assertRaises(ValueError):self.preview()
        self.assertEqual(self.cat.calls,[])

    def test_existing_watch_is_reused_and_existing_rating_never_replaced(self):
        self.existing(True);old=deepcopy(self.cat.value)
        op=self.confirm(self.preview(rating=4));self.assertEqual(self.execute(op)['status'],'completed')
        self.assertEqual(self.cat.value['history'],old['history']);self.assertEqual(self.cat.value['reviews'],old['reviews'])
        self.assertEqual(self.completion.calls,[]);self.assertEqual(self.cat.client.calls,[])
    def test_different_existing_rating_or_date_rejected_without_writes(self):
        self.existing(True);self.cat.calls=[]
        for choice in ({'rating':5},{'watched_on':'2019-01-01'}):
            with self.subTest(choice=choice),self.assertRaises(ValueError):self.preview(**choice)
        self.assertEqual(self.cat.calls,[])
    def test_add_rating_to_existing_unrated_watch_preserves_other_history(self):
        self.existing();self.cat.value['history'].append({**deepcopy(self.cat.value['history'][0]),'id':'older','finishedOn':'2010-01-02T12:34:00+00:00'})
        old=deepcopy(self.cat.value['history'][1]);op=self.confirm(self.preview(rating=3))
        self.assertEqual(self.execute(op)['status'],'completed');self.assertEqual(self.cat.value['history'][1],old)
        self.assertEqual(self.completion.calls,[])
    def test_lost_completed_reply_recovers_without_second_watch(self):
        self.completion.lost=True;op=self.confirm();self.assertEqual(self.execute(op)['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'completed');self.assertEqual(len(self.completion.calls),1)
    def test_no_effect_completed_reply_never_blindly_retries(self):
        self.completion.lost=True;self.completion.effect=False;op=self.confirm()
        self.assertEqual(self.execute(op)['status'],'uncertain');self.assertEqual(self.retry(op)['status'],'uncertain')
        self.assertEqual(len(self.completion.calls),1)
    def test_lost_review_reply_recovers_without_duplicate_review(self):
        self.cat.client.lost='review';op=self.confirm(self.preview(rating=5));self.assertEqual(self.execute(op)['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'completed');self.assertEqual(len(self.cat.value['reviews']),1)
        self.assertEqual(len(self.cat.client.calls),2)
    def test_lost_link_reply_recovers_without_repeating_any_provider_write(self):
        self.cat.client.lost='link';op=self.confirm(self.preview(rating=2));self.assertEqual(self.execute(op)['status'],'uncertain')
        self.assertEqual(self.retry(op)['status'],'completed');self.assertEqual(len(self.cat.client.calls),2)
    def test_unrelated_review_after_completion_halts_remaining_writes(self):
        def interfere():self.cat.value['reviews'].append({'id':'foreign'})
        self.completion.hook=interfere;op=self.confirm(self.preview(rating=3))
        self.assertEqual(self.execute(op)['status'],'uncertain');self.assertEqual(self.cat.client.calls,[])
    def test_automatic_source_actor_cannot_authorize_past_viewing(self):
        with self.assertRaises(ValueError):
            self.service.preview(self.candidate, actor='source:imdb', mode='catalogue_only', watched=True)
        self.assertEqual(self.cat.calls, [])

    def test_invalid_past_dates_and_scores_cannot_be_confirmed(self):
        for choice in ({'watched_on':'3000-01-01'},{'watched_on':'2020-02-30'},{'rating':0},{'rating':True}):
            with self.subTest(choice=choice),self.assertRaises(ValueError):self.preview(**choice)
        self.assertEqual(self.cat.calls,[])
    def test_partial_history_and_pending_reset_block_registration(self):
        self.existing();self.cat.value['history'][0]['state']='IN_PROGRESS'
        with self.assertRaises(ValueError):self.preview()
    def test_cannot_attach_viewing_to_download_or_non_watched_registration(self):
        for mode,watched,rating in [('add_search',True,None),('catalogue_only',False,4)]:
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                self.service.preview(self.candidate,actor='telegram:user:1',mode=mode,watched=watched,rating=rating)
    def test_existing_observer_question_resolves_when_manually_registered(self):
        store=ViewingStore(self.s);store.observe(self.native.account(),[],participants=[1,2]);self.existing()
        account,events=self.native.scan();report=store.observe(account,events,participants=[1,2]);self.assertEqual(len(report['created']),1)
        op=self.confirm();self.assertEqual(self.execute(op)['status'],'completed')
        self.assertEqual(self.s.get_pending_decision(report['created'][0]).status,'resolved')
