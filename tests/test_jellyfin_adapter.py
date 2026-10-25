"""Playback readiness requires matched non-placeholder sources; subtitles are not audio."""

from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from curatarr.integrations.jellyfin import JellyfinError, JellyfinReadAdapter
from test_telegram_service import _movie


class JellyfinAdapterTests(unittest.TestCase):
    def setUp(self):
        self.item = {"Id": "item1", "Type": "Movie", "ProviderIds": {"Tmdb": "558"}, "LocationType": "FileSystem",
                     "MediaSources": [{"Path": "/media/movie.mkv", "MediaStreams": [
                         {"Type": "Audio", "Language": "eng"}, {"Type": "Audio", "Language": "fra"},
                         {"Type": "Subtitle", "Language": "spa"}]}]}

    def read(self, items):
        return JellyfinReadAdapter("http://test", "secret", lambda path, params: {
            "Items": items, "TotalRecordCount": len(items)}).playback_facts(_movie())

    def test_ready_source_exposes_only_audio_languages(self):
        facts = self.read([self.item])
        self.assertEqual(facts["status"], "ready")
        self.assertEqual(facts["versions"][0]["audio_languages"], ["eng", "fra"])
        self.assertTrue(facts["versions"][0]["audio_complete"])
        self.assertNotIn("/media", str(facts))

    def test_versions_are_not_merged_into_a_fictional_bilingual_copy(self):
        first = deepcopy(self.item)
        second = deepcopy(self.item)
        second["Id"] = "item2"
        first["MediaSources"][0]["MediaStreams"] = [{"Type": "Audio", "Language": "eng"}]
        second["MediaSources"][0]["MediaStreams"] = [{"Type": "Audio", "Language": "fra"}]
        facts = self.read([first, second])
        self.assertEqual(facts["version_count"], 2)
        self.assertEqual([version["audio_languages"] for version in facts["versions"]], [["eng"], ["fra"]])

    def test_unknown_language_is_not_inferred_from_subtitle_or_title(self):
        self.item["MediaSources"][0]["MediaStreams"] = [{"Type": "Audio"}, {"Type": "Subtitle", "Language": "eng"}]
        facts = self.read([self.item])
        self.assertEqual(facts["versions"][0]["audio_languages"], [])
        self.assertFalse(facts["versions"][0]["audio_complete"])

    def test_wrong_identity_virtual_and_placeholder_items_are_not_ready(self):
        self.item["ProviderIds"] = {"Tmdb": "999"}
        self.assertEqual(self.read([self.item])["status"], "not_present")
        self.item["ProviderIds"] = {"Tmdb": "558"}
        self.item["IsPlaceHolder"] = True
        self.assertEqual(self.read([self.item])["status"], "not_present")

    def test_missing_and_partial_payloads_fail_instead_of_claiming_absence(self):
        for payload in ({}, {"Items": []}, {"Items": [], "TotalRecordCount": 1}):
            with self.subTest(payload=payload), self.assertRaises(JellyfinError):
                JellyfinReadAdapter("http://test", "secret", lambda *_: payload).playback_facts(_movie())
        self.assertEqual(self.read([])["status"], "not_present")

    def test_catalogue_paginates_and_does_not_use_unsupported_provider_filter(self):
        calls = []
        first = [{"Id": str(index), "Type": "Movie", "ProviderIds": {"Tmdb": str(10000 + index)}} for index in range(500)]
        def transport(path, params):
            calls.append(params)
            self.assertNotIn("AnyProviderIdEquals", params)
            if "Ids" in params:
                return {"Items": [self.item], "TotalRecordCount": 1}
            return {"Items": first if params["StartIndex"] == "0" else [self.item], "TotalRecordCount": 501}
        adapter = JellyfinReadAdapter("http://test", "secret", transport)
        self.assertEqual(adapter.playback_facts(_movie())["status"], "ready")
        self.assertEqual([params.get("StartIndex") for params in calls], ["0", "500", None])
        adapter.playback_facts(_movie())
        self.assertEqual(len(calls), 4)  # Current source is re-read; catalogue is briefly cached.

    def test_repeated_page_is_not_silently_treated_as_a_complete_catalogue(self):
        page = [{"Id": str(index), "Type": "Movie", "ProviderIds": {}} for index in range(500)]
        with self.assertRaises(JellyfinError):
            JellyfinReadAdapter("http://test", "secret", lambda *_: {"Items": page, "TotalRecordCount": 501}).playback_facts(_movie())

    def test_authentication_is_header_only_and_http_errors_are_sanitized(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"Items": [], "TotalRecordCount": 0}'
        with patch("curatarr.integrations.jellyfin.urlopen", return_value=response) as send:
            JellyfinReadAdapter("https://test.invalid", "private-secret").playback_facts(_movie())
        request = send.call_args.args[0]
        self.assertNotIn("private-secret", request.full_url)
        self.assertEqual(request.get_header("X-emby-token"), "private-secret")
        error = HTTPError("https://test.invalid/private-secret", 401, "private-secret", {}, None)
        with patch("curatarr.integrations.jellyfin.urlopen", side_effect=error), self.assertRaises(JellyfinError) as raised:
            JellyfinReadAdapter("https://test.invalid", "private-secret").playback_facts(_movie())
        self.assertEqual(str(raised.exception), "Playback library HTTP error 401.")
