"""TMDB authentication must support API keys and read-access bearer tokens."""

import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from curatarr.integrations.tmdb import TmdbClient


class TmdbAuthenticationTests(unittest.TestCase):
    def request_for(self, credential):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"results": []}'
        with patch("curatarr.integrations.tmdb.client.urlopen", return_value=response) as send:
            TmdbClient(credential).search_movie("Spider-Man")
        return send.call_args.args[0]

    def test_api_key_is_sent_as_query_parameter(self):
        request = self.request_for("test-api-key")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["api_key"], ["test-api-key"])
        self.assertIsNone(request.get_header("Authorization"))

    def test_jwt_and_explicit_bearer_are_sent_in_header_only(self):
        for value, token in (("eyJtest.payload.signature", "eyJtest.payload.signature"),
                             ("Bearer test-token", "test-token")):
            with self.subTest(value=value):
                request = self.request_for(value)
                self.assertEqual(request.get_header("Authorization"), f"Bearer {token}")
                self.assertNotIn("api_key", parse_qs(urlsplit(request.full_url).query))
                self.assertNotIn(token, request.full_url)

    def test_client_repr_does_not_expose_credential(self):
        self.assertNotIn("private-credential", repr(TmdbClient("private-credential")))
