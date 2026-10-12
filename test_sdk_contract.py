"""Use the installed official SDK discovery document without network calls."""
import importlib.util
import json
import unittest

from gmail_push import GmailApi


@unittest.skipUnless(importlib.util.find_spec('googleapiclient'), 'push dependencies not installed')
class SdkContractTest(unittest.TestCase):
    def test_gmail_sdk_methods_and_pubsub_subscribe_shape(self):
        from importlib.resources import files
        from urllib.parse import parse_qs, urlsplit
        from googleapiclient.discovery import build_from_document
        from google.cloud import pubsub_v1
        from httplib2 import Response
        import inspect

        class MemoryHttp:
            def __init__(self):
                self.calls = []

            def request(self, uri, method='GET', body=None, headers=None, **kwargs):
                self.calls.append((uri, method, body))
                return Response({'status': '200'}), b'{"historyId":"101","history":[],"expiration":"9999999999999"}'

        http = MemoryHttp()
        discovery = files('googleapiclient.discovery_cache.documents').joinpath('gmail.v1.json').read_text()
        api = GmailApi(build_from_document(discovery, http=http))
        api.history_page('100')
        api.metadata_message('a1')
        api.full_message('a1')
        api.watch('projects/example/topics/personal')
        history, metadata, full, watch = http.calls
        self.assertEqual(parse_qs(urlsplit(history[0]).query)['startHistoryId'], ['100'])
        self.assertEqual(parse_qs(urlsplit(history[0]).query)['historyTypes'], ['messageAdded'])
        self.assertEqual(parse_qs(urlsplit(metadata[0]).query)['format'], ['metadata'])
        self.assertEqual(parse_qs(urlsplit(metadata[0]).query)['metadataHeaders'], ['From'])
        self.assertEqual(parse_qs(urlsplit(full[0]).query)['format'], ['full'])
        self.assertEqual(watch[1], 'POST')
        self.assertEqual(json.loads(watch[2])['topicName'], 'projects/example/topics/personal')
        self.assertIn('callback', inspect.signature(pubsub_v1.SubscriberClient.subscribe).parameters)


if __name__ == '__main__':
    unittest.main()
