import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import gmail_push
import mail_receiver
from test_bot import FakeGitHub, gmail_message


class FakeGmail:
    def __init__(self):
        self.email = 'person@example.test'
        self.profile_history = '200'
        self.watch_history = '100'
        self.history_pages = [{'history': [], 'historyId': '100'}]
        self.search_pages = [{'messages': []}]
        self.messages = {}
        self.history_calls = []
        self.watch_calls = []
        self.full_calls = []

    def profile(self):
        return {'emailAddress': self.email, 'historyId': self.profile_history}

    def watch(self, topic):
        self.watch_calls.append(topic)
        return {'historyId': self.watch_history, 'expiration': '9999999999999'}

    def history_page(self, start, token=None):
        self.history_calls.append((start, token))
        if isinstance(self.history_pages, Exception):
            raise self.history_pages
        return self.history_pages[0 if token is None else int(token)]

    def search_page(self, query, token=None):
        return self.search_pages[0 if token is None else int(token)]

    def full_message(self, message_id):
        self.full_calls.append(message_id)
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value

    def metadata_message(self, message_id):
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return {'id': message_id, 'payload': {'headers': [header for header in value['payload']['headers']
                                                        if header['name'].lower() == 'from']}}


class FakePubsubMessage:
    def __init__(self, history_id='101', email='person@example.test'):
        self.data = json.dumps({'emailAddress': email, 'historyId': history_id}).encode()
        self.acks = self.nacks = 0

    def ack(self):
        self.acks += 1

    def nack(self):
        self.nacks += 1


class PushTest(unittest.TestCase):
    def test_keychain_oauth_info_requires_exact_scope_and_refresh_token(self):
        valid = {
            'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
            'refresh_token': 'fixture-refresh-token',
        }
        with patch.object(gmail_push.subprocess, 'run',
                          return_value=MagicMock(returncode=0, stdout=json.dumps(valid))) as run:
            self.assertEqual(gmail_push.keychain_oauth_info(
                'personal-crashlytics-bot-oauth-user',
                'https://www.googleapis.com/auth/gmail.readonly'), valid)
            run.assert_called_once_with(
                ['/usr/bin/swift', '-module-cache-path',
                 str(Path(gmail_push.tempfile.gettempdir()) / 'personal-crashlytics-swift-cache'),
                 str(Path(gmail_push.__file__).with_name('keychain_read.swift')),
                 'personal-crashlytics-bot-oauth-user'],
                capture_output=True, text=True)
        for wrong in ({**valid, 'scopes': ['https://www.googleapis.com/auth/cloud-platform']},
                      {**valid, 'refresh_token': ''}, []):
            with patch.object(gmail_push.subprocess, 'run',
                              return_value=MagicMock(returncode=0, stdout=json.dumps(wrong))):
                with self.assertRaises(ValueError):
                    gmail_push.keychain_oauth_info(
                        'personal-crashlytics-bot-oauth-user',
                        'https://www.googleapis.com/auth/gmail.readonly')

    def test_keychain_oauth_info_fails_closed(self):
        with patch.object(gmail_push.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                gmail_push.keychain_oauth_info(
                    'personal-crashlytics-bot-pubsub-user',
                    'https://www.googleapis.com/auth/gmail.readonly')
            run.assert_not_called()
        for result in (MagicMock(returncode=1, stdout=''),
                       MagicMock(returncode=0, stdout='not-json')):
            with patch.object(gmail_push.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError):
                    gmail_push.keychain_oauth_info(
                        'personal-crashlytics-bot-pubsub-user',
                        'https://www.googleapis.com/auth/pubsub')

    def make_receiver(self, tmp):
        root = Path(tmp)
        push_config = root / 'push.json'
        runtime = root / 'receiver.json'
        state = root / 'gmail.sqlite'
        push_config.write_text(json.dumps({
            'gmail_email': 'person@example.test',
            'topic_name': 'projects/example/topics/personal-crashes',
            'subscription_name': 'projects/example/subscriptions/personal-crashes',
        }))
        runtime.write_text(json.dumps({'gmail_link_id': 'personal-link', 'gmail_profile_id': 'personal-profile',
                                       'start_epoch': int(time.time()) - 3600}))
        gmail, gh = FakeGmail(), FakeGitHub()
        receiver = gmail_push.Receiver(gmail, gh, push_config, runtime, state)
        return receiver, gmail, gh, runtime, state

    def test_watch_bootstrap_pagination_ack_and_redelivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, _, _ = self.make_receiver(tmp)
            receiver.renew_watch()
            self.assertEqual(receiver._cursor(), '100')
            gmail.messages = {'a1': gmail_message(message_id='a1'), 'a2': gmail_message(message_id='a2')}
            gmail.history_pages = [
                {'history': [{'messagesAdded': [{'message': {'id': 'a1'}}]}],
                 'nextPageToken': '1', 'historyId': '102'},
                {'history': [{'messagesAdded': [{'message': {'id': 'a2'}}]}], 'historyId': '105'},
            ]
            first = FakePubsubMessage(105)
            self.assertTrue(gmail_push.on_pubsub_message(receiver, first))
            self.assertEqual((first.acks, first.nacks), (1, 0))
            self.assertEqual(receiver._cursor(), '105')
            self.assertEqual(gmail.history_calls, [('100', None), ('100', '1')])
            self.assertEqual(len(gh.issues), 1)
            second = FakePubsubMessage('105')
            self.assertTrue(gmail_push.on_pubsub_message(receiver, second))
            self.assertEqual(len(gh.issues), 1)

    def test_failed_fetch_nacks_and_retries_without_cursor_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, _, state = self.make_receiver(tmp)
            receiver.renew_watch()
            gmail.history_pages = [{'history': [{'messagesAdded': [{'message': {'id': 'a1'}}]}],
                                    'historyId': '101'}]
            gmail.messages['a1'] = RuntimeError('temporary Gmail error')
            failed = FakePubsubMessage()
            self.assertFalse(gmail_push.on_pubsub_message(receiver, failed))
            self.assertEqual((failed.acks, failed.nacks), (0, 1))
            self.assertEqual(receiver._cursor(), '100')
            self.assertEqual(mail_receiver.pending_ids(state), ['a1'])
            gmail.messages['a1'] = gmail_message(message_id='a1')
            self.assertTrue(gmail_push.on_pubsub_message(receiver, FakePubsubMessage()))
            self.assertEqual(receiver._cursor(), '101')
            self.assertEqual(len(gh.issues), 1)

    def test_history_404_search_recovery_and_watch_renewal(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, runtime, _ = self.make_receiver(tmp)
            receiver.renew_watch()
            gmail.watch_history = '300'
            receiver.renew_watch()
            self.assertEqual(receiver._cursor(), '100')
            gmail.history_pages = gmail_push.HistoryExpired()
            gmail.messages['a1'] = gmail_message(message_id='a1')
            gmail.search_pages = [
                {'messages': [{'id': 'a1'}], 'nextPageToken': '1'},
                {'messages': [{'id': 'a1'}]},
            ]
            self.assertEqual(receiver.reconcile()['status'], 'history-expired-recovered')
            self.assertEqual(receiver._cursor(), '200')
            self.assertIn('last_scan_epoch', json.loads(runtime.read_text()))
            self.assertEqual(len(gh.issues), 1)

    def test_wrong_account_and_unrelated_mail_never_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, _, _ = self.make_receiver(tmp)
            gmail.email = 'other@example.test'
            with self.assertRaisesRegex(ValueError, 'account-mismatch'):
                receiver.renew_watch()
            self.assertEqual(gmail.watch_calls, [])
            gmail.email = 'person@example.test'
            receiver.renew_watch()
            unrelated = gmail_message(message_id='a1')
            unrelated['payload']['headers'][0]['value'] = 'friend@example.test'
            gmail.messages['a1'] = unrelated
            gmail.history_pages = [{'history': [{'messagesAdded': [{'message': {'id': 'a1'}}]}],
                                    'historyId': '101'}]
            self.assertEqual(receiver.reconcile()['status'], 'history-reconciled')
            self.assertEqual(receiver._cursor(), '101')
            self.assertEqual(gh.writes, [])
            self.assertEqual(gmail.full_calls, [])

    def test_spoofed_firebase_mail_is_ignored_without_stalling_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, _, _ = self.make_receiver(tmp)
            receiver.renew_watch()
            spoof = gmail_message(message_id='a1')
            spoof['payload']['headers'][1]['value'] = 'mx.google.com; dkim=fail; dmarc=fail header.from=google.com'
            gmail.messages['a1'] = spoof
            gmail.history_pages = [{'history': [{'messagesAdded': [{'message': {'id': 'a1'}}]}],
                                    'historyId': '101'}]
            self.assertEqual(receiver.reconcile()['status'], 'history-reconciled')
            self.assertEqual(receiver._cursor(), '101')
            self.assertEqual(gh.writes, [])

    def test_concurrent_notifications_have_one_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, _, _ = self.make_receiver(tmp)
            receiver.renew_watch()
            gmail.messages['a1'] = gmail_message(message_id='a1')
            gmail.history_pages = [{'history': [{'messagesAdded': [{'message': {'id': 'a1'}}]}],
                                    'historyId': '101'}]
            entered, release = threading.Event(), threading.Event()
            original = gh.api
            def blocked(path, method='GET', data=None):
                if path.endswith('/issues') and method == 'POST':
                    entered.set()
                    self.assertTrue(release.wait(3))
                return original(path, method, data)
            gh.api = blocked
            first, second = FakePubsubMessage(), FakePubsubMessage()
            one = threading.Thread(target=lambda: gmail_push.on_pubsub_message(receiver, first))
            two = threading.Thread(target=lambda: gmail_push.on_pubsub_message(receiver, second))
            one.start()
            self.assertTrue(entered.wait(3))
            two.start()
            release.set()
            one.join(3)
            two.join(3)
            self.assertFalse(one.is_alive() or two.is_alive())
            self.assertEqual((first.acks, second.acks), (1, 1))
            self.assertEqual(len(gh.issues), 1)

    def test_interruption_after_remote_create_recovers_after_lease_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, gh, _, state = self.make_receiver(tmp)
            receiver.renew_watch()
            gmail.messages['a1'] = gmail_message(message_id='a1')
            gmail.history_pages = [{'history': [{'messagesAdded': [{'message': {'id': 'a1'}}]}],
                                    'historyId': '101'}]
            original = gh.api
            def interrupted(path, method='GET', data=None):
                result = original(path, method, data)
                if path.endswith('/issues') and method == 'POST':
                    raise KeyboardInterrupt()
                return result
            gh.api = interrupted
            with self.assertRaises(KeyboardInterrupt):
                receiver.reconcile()
            self.assertEqual(receiver._cursor(), '100')
            self.assertEqual(mail_receiver.pending_ids(state), ['a1'])
            self.assertEqual(len(gh.issues), 1)
            with sqlite3.connect(state) as db:
                db.execute('UPDATE pending SET lease_until=0 WHERE id=?', ('a1',))
            gh.api = original
            self.assertEqual(receiver.reconcile()['status'], 'history-reconciled')
            self.assertEqual(receiver._cursor(), '101')
            self.assertEqual(len(gh.issues), 1)

    def test_invalid_notification_nacks_without_gmail_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver, gmail, _, _, _ = self.make_receiver(tmp)
            receiver.renew_watch()
            wrong = FakePubsubMessage(email='other@example.test')
            self.assertFalse(gmail_push.on_pubsub_message(receiver, wrong))
            self.assertEqual((wrong.acks, wrong.nacks), (0, 1))
            self.assertEqual(gmail.history_calls, [])
            invalid = FakePubsubMessage(history_id=True)
            self.assertFalse(gmail_push.on_pubsub_message(receiver, invalid))
            self.assertEqual((invalid.acks, invalid.nacks), (0, 1))
            self.assertEqual(gmail.history_calls, [])

    def test_google_api_adapter_maps_history_404(self):
        service = MagicMock()
        error = RuntimeError('expired')
        error.resp = type('Response', (), {'status': 404})()
        service.users().history().list().execute.side_effect = error
        api = gmail_push.GmailApi(service)
        with self.assertRaises(gmail_push.HistoryExpired):
            api.history_page('100')
        service.users().history().list.assert_called_with(
            userId='me', startHistoryId='100', historyTypes=['messageAdded'], maxResults=500)
        api.metadata_message('a1')
        service.users().messages().get.assert_called_with(
            userId='me', id='a1', format='metadata', metadataHeaders=['From'])


if __name__ == '__main__':
    unittest.main()
