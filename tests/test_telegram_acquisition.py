"""Chat profile/catalogue choices invoke the same guarded acquisition service."""
import unittest
from curatarr.storage import Storage, StorageError
from curatarr.services.acquisition import MovieAcquisition
from curatarr.services.telegram_acquisition import TelegramAcquisition
from test_movie_acquisition import Catalogue, Arr


class TelegramAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.storage=Storage(':memory:');self.storage.initialize();self.addCleanup(self.storage.close)
        self.cat,self.arr=Catalogue(),Arr();self.service=MovieAcquisition(self.storage,self.cat,self.arr)
        self.ui=TelegramAcquisition(self.service);self.actor='telegram:user:1'
        self.decision=self.storage.create_pending_decision(media_key='tmdb:123',decision_type='search_add',audience=self.actor,state_fingerprint='f',
            context={'candidate':{'title':'Example','year':2020,'identities':['tmdb:123','imdb:tt123']}})
    def test_catalogue_only_has_explicit_confirmation_and_no_download(self):
        prompt=self.ui.open(self.decision,self.actor);button=next(b for b in prompt.buttons if b.label=='Catalogue only')
        preview=self.ui.handle(button.callback_data,self.actor)
        self.assertEqual(self.cat.calls,[]);self.assertEqual(self.arr.calls,[])
        result=self.ui.handle(preview.buttons[0].callback_data,self.actor)
        self.assertIn('Saved successfully',result.text);self.assertEqual(self.arr.calls,[])
    def test_profile_then_root_then_confirm_starts_one_search(self):
        prompt=self.ui.open(self.decision,self.actor);roots=self.ui.handle(prompt.buttons[0].callback_data,self.actor)
        preview=self.ui.handle(roots.buttons[0].callback_data,self.actor);self.assertIn('Native profile',preview.text)
        self.assertEqual(self.arr.calls,[]);self.ui.handle(preview.buttons[0].callback_data,self.actor)
        self.ui.handle(preview.buttons[0].callback_data,self.actor)
        self.assertEqual(self.arr.calls,['add','search'])
    def test_other_user_and_unoffered_profile_cannot_acquire(self):
        prompt=self.ui.open(self.decision,self.actor)
        with self.assertRaises(StorageError):self.ui.handle(prompt.buttons[0].callback_data,'telegram:user:2')
        bad=prompt.buttons[0].callback_data.rsplit(':',1)[0]+':999'
        with self.assertRaises(ValueError):self.ui.handle(bad,self.actor)
        self.assertEqual(self.arr.calls,[])
    def test_uncertain_outcome_status_can_refresh_without_second_search(self):
        self.arr.lost=True;prompt=self.ui.open(self.decision,self.actor)
        roots=self.ui.handle(prompt.buttons[0].callback_data,self.actor)
        preview=self.ui.handle(roots.buttons[0].callback_data,self.actor)
        pending=self.ui.handle(preview.buttons[0].callback_data,self.actor)
        self.assertIn('being checked',pending.text);self.ui.handle(pending.buttons[0].callback_data,self.actor)
        self.assertEqual(self.arr.calls,['add','search'])
