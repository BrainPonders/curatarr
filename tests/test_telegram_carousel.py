"""Poster navigation and list switching preserve selection and callback protections."""

import unittest
from dataclasses import replace

from curatarr.domain import MediaType
import test_telegram_callbacks as callback_fixtures
from test_telegram_service import _movie


class TelegramCarouselTests(unittest.TestCase):
    def setUp(self):
        # Reuse the fixture setup without inheriting its test cases.
        callback_fixtures.TelegramCallbackTests.setUp(self)
        self.first = replace(_movie(), poster_url="https://image.tmdb.org/t/p/w500/first.jpg", overview="A first movie.")
        self.second = replace(_movie(title="Second movie"), poster_url="https://image.tmdb.org/t/p/w500/second.jpg")
        self.service.metadata_adapter.candidates = (self.first, self.second)

    def search(self):
        return self.command.search_choices("movie", MediaType.MOVIE, audience=self.audience)

    def click(self, data, audience=None):
        return self.callbacks.handle(data, audience=audience or self.audience)

    def test_defaults_to_first_poster_and_selection_is_explicit(self):
        prompt = self.search()
        self.assertEqual(prompt.photo_url, self.first.poster_url)
        self.assertIn("Result 1 of 2", prompt.text)
        self.assertIn("A first movie.", prompt.text)
        self.assertNotIn("< Previous", [button.label for button in prompt.buttons])
        self.assertIn("Next >", [button.label for button in prompt.buttons])
        self.assertIn("Select movie", [button.label for button in prompt.buttons])
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")

    def test_navigation_changes_poster_without_reading_media_services(self):
        prompt = self.search()
        self.ryot.get_item = lambda _: self.fail("Browsing read Ryot")
        self.arr.lookup_movie = lambda _: self.fail("Browsing read Radarr")
        second = self.click(f"browse:{prompt.pending_decision_id}:1").prompt
        self.assertEqual(second.photo_url, self.second.poster_url)
        self.assertIn("Result 2 of 2", second.text)
        self.assertNotIn("Next >", [button.label for button in second.buttons])
        first = self.click(f"browse:{prompt.pending_decision_id}:0").prompt
        self.assertEqual(first.photo_url, self.first.poster_url)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")

    def test_list_mode_and_return_preserve_current_index(self):
        prompt = self.search()
        listing = self.click(f"list:{prompt.pending_decision_id}:1").prompt
        self.assertIsNone(listing.photo_url)
        self.assertEqual(len(listing.buttons), 3)
        self.assertEqual(listing.buttons[-1].label, "Poster view")
        poster = self.click(listing.buttons[-1].callback_data).prompt
        self.assertEqual(poster.photo_url, self.second.poster_url)
        self.assertIn("Result 2 of 2", poster.text)

    def test_list_selects_requested_movie_and_closes_navigation(self):
        prompt = self.search()
        listing = self.click(f"list:{prompt.pending_decision_id}:0").prompt
        selected = self.click(listing.buttons[1].callback_data)
        self.assertIn("Second movie", selected.prompt.text)
        self.assertIsNone(selected.prompt.photo_url)
        self.assertIsNone(self.click(f"browse:{prompt.pending_decision_id}:0").prompt)

    def test_missing_poster_keeps_navigation_and_select(self):
        self.service.metadata_adapter.candidates = (_movie(),)
        prompt = self.search()
        self.assertIsNone(prompt.photo_url)
        self.assertIn("Poster unavailable", prompt.text)
        self.assertEqual([button.label for button in prompt.buttons], ["Select movie", "List view"])

    def test_navigation_is_authorized_and_bounded(self):
        prompt = self.search()
        for kind in ("browse", "list"):
            for index in ("-1", "2", "no", "999"):
                self.assertIsNone(self.click(f"{kind}:{prompt.pending_decision_id}:{index}").prompt)
            self.assertIsNone(self.click(f"{kind}:{prompt.pending_decision_id}:0", "telegram:user:2").prompt)
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "open")

    def test_navigation_expiry_retires_prompt(self):
        prompt = self.search()
        self.storage.connection.execute("UPDATE pending_decision SET expires_at = '2000-01-01 00:00:00'")
        self.storage.connection.commit()
        result = self.click(f"browse:{prompt.pending_decision_id}:1")
        self.assertEqual(result.prompt.buttons, ())
        self.assertEqual(self.storage.get_pending_decision(prompt.pending_decision_id).status, "expired")
