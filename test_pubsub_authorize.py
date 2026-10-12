import unittest

try:
    from oauthlib.oauth2 import WebApplicationClient
    from scripts.authorize_pubsub import (
        SCOPE, authorization_response_url, scope_set,
    )
except ModuleNotFoundError:
    SCOPE = authorization_response_url = scope_set = None


@unittest.skipIf(scope_set is None, 'OAuth runtime dependencies unavailable')
class PubsubAuthorizeTest(unittest.TestCase):
    def test_scope_grant_accepts_google_string_or_sequence(self):
        self.assertEqual({SCOPE}, scope_set(SCOPE))
        self.assertEqual({SCOPE}, scope_set([SCOPE]))

    def test_scope_grant_rejects_missing_or_broader_scope(self):
        self.assertNotEqual({SCOPE}, scope_set(None))
        self.assertNotEqual({SCOPE}, scope_set(SCOPE + ' extra-scope'))

    def test_loopback_response_passes_oauthlib_secure_parser(self):
        response = authorization_response_url(
            '/oauth2callback?code=fixture-code&state=fixture-state',
            58000, 'fixture-state')
        self.assertEqual('https://127.0.0.1:58000/oauth2callback?code=fixture-code&state=fixture-state',
                         response)
        parsed = WebApplicationClient('fixture-client').parse_request_uri_response(
            response, state='fixture-state')
        self.assertEqual('fixture-code', parsed['code'])

    def test_loopback_response_rejects_wrong_state_or_external_uri(self):
        for target in (
                '/oauth2callback?code=fixture-code&state=wrong',
                'http://example.test/oauth2callback?code=fixture-code&state=fixture-state',
                '//example.test/oauth2callback?code=fixture-code&state=fixture-state',
                '/other?code=fixture-code&state=fixture-state'):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    authorization_response_url(target, 58000, 'fixture-state')


if __name__ == '__main__':
    unittest.main()
