"""Personal Gmail watch/Pub/Sub receiver. No cloud resources are created here."""
import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

import bot
import mail_receiver


class HistoryExpired(Exception):
    pass


def history_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,30}', value):
        raise ValueError('invalid-gmail-history-id')
    return value


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ('gmail_email', 'topic_name', 'subscription_name'):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError('invalid-push-config')
    if not re.fullmatch(r'projects/[^/]+/topics/[^/]+', config['topic_name']):
        raise ValueError('invalid-push-topic')
    if not re.fullmatch(r'projects/[^/]+/subscriptions/[^/]+', config['subscription_name']):
        raise ValueError('invalid-push-subscription')
    return config


@contextmanager
def writer_lock():
    path = Path(tempfile.gettempdir()) / 'personal-crashlytics-bot.lock'
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class GmailApi:
    def __init__(self, service):
        self.users = service.users()

    def profile(self):
        return self.users.getProfile(userId='me').execute()

    def watch(self, topic_name):
        return self.users.watch(userId='me', body={'topicName': topic_name}).execute()

    def history_page(self, start, page_token=None):
        args = {'userId': 'me', 'startHistoryId': start, 'historyTypes': ['messageAdded'], 'maxResults': 500}
        if page_token:
            args['pageToken'] = page_token
        try:
            return self.users.history().list(**args).execute()
        except Exception as error:
            if getattr(getattr(error, 'resp', None), 'status', None) == 404:
                raise HistoryExpired() from error
            raise

    def search_page(self, query, page_token=None):
        args = {'userId': 'me', 'q': query, 'maxResults': 100}
        if page_token:
            args['pageToken'] = page_token
        return self.users.messages().list(**args).execute()

    def full_message(self, message_id):
        return self.users.messages().get(userId='me', id=message_id, format='full').execute()

    def metadata_message(self, message_id):
        return self.users.messages().get(userId='me', id=message_id, format='metadata',
                                         metadataHeaders=['From']).execute()


class Receiver:
    def __init__(self, gmail, gh, push_config, runtime_config, state_file):
        self.gmail = gmail
        self.gh = gh
        self.config = load_config(push_config)
        self.runtime_config = Path(runtime_config)
        self.state_file = mail_receiver.prepare_state(state_file)
        with sqlite3.connect(self.state_file) as state:
            state.execute('CREATE TABLE IF NOT EXISTS gmail_push_state '
                          '(singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor TEXT, watch_expiration INTEGER)')

    def _cursor(self):
        with sqlite3.connect(self.state_file) as state:
            row = state.execute('SELECT cursor FROM gmail_push_state WHERE singleton=1').fetchone()
            return row[0] if row else None

    def _save(self, cursor, expiration=None):
        history_id(cursor)
        with sqlite3.connect(self.state_file) as state:
            state.execute('INSERT INTO gmail_push_state(singleton,cursor,watch_expiration) VALUES (1,?,?) '
                          'ON CONFLICT(singleton) DO UPDATE SET cursor=excluded.cursor, '
                          'watch_expiration=COALESCE(excluded.watch_expiration,gmail_push_state.watch_expiration)',
                          (cursor, expiration))

    def _verify_mailbox(self):
        if self.gmail.profile().get('emailAddress', '').lower() != self.config['gmail_email'].lower():
            raise ValueError('gmail-account-mismatch')

    def _process_ids(self, ids):
        # Cursor is not advanced until all messages and earlier pending IDs succeed.
        for message_id in dict.fromkeys(ids):
            if mail_receiver.enqueue(message_id, self.state_file)['status'] == 'already-processed':
                continue
            metadata = self.gmail.metadata_message(message_id)
            if metadata.get('id') != message_id:
                raise ValueError('fetched-gmail-message-id-mismatch')
            message = (self.gmail.full_message(message_id) if mail_receiver.firebase_sender(metadata)
                       else metadata)
            mail_receiver.receive(self.gh, message, self.state_file, expected_id=message_id)

    def _retry_pending(self):
        self._process_ids(mail_receiver.pending_ids(self.state_file))

    def _search_recovery(self):
        plan = mail_receiver.poll_query(self.runtime_config, self.state_file)
        self._retry_pending()
        page_token = None
        seen_tokens = set()
        while True:
            page = self.gmail.search_page(plan['query'], page_token)
            self._process_ids([item['id'] for item in page.get('messages', [])])
            page_token = page.get('nextPageToken')
            if not page_token:
                break
            if page_token in seen_tokens:
                raise RuntimeError('repeated-gmail-page-token')
            seen_tokens.add(page_token)
        mail_receiver.checkpoint(self.runtime_config, plan['scan_started_epoch'], self.state_file)

    def renew_watch(self):
        with writer_lock():
            self._verify_mailbox()
            response = self.gmail.watch(self.config['topic_name'])
            watch_cursor = history_id(response['historyId'])
            expiration = int(response['expiration'])
            if expiration <= int(time.time() * 1000):
                raise ValueError('invalid-watch-expiration')
            if self._cursor() is None:
                # Watch first: arrivals during the search remain visible from this cursor.
                self._search_recovery()
                self._save(watch_cursor, expiration)
            else:
                with sqlite3.connect(self.state_file) as state:
                    state.execute('UPDATE gmail_push_state SET watch_expiration=? WHERE singleton=1',
                                  (expiration,))
            return {'status': 'watch-renewed', 'expiration': expiration}

    def reconcile(self):
        with writer_lock():
            self._verify_mailbox()
            cursor = self._cursor()
            if cursor is None:
                raise RuntimeError('gmail-watch-not-initialized')
            self._retry_pending()
            page_token = None
            seen_tokens = set()
            newest = cursor
            try:
                while True:
                    page = self.gmail.history_page(cursor, page_token)
                    ids = [added['message']['id'] for record in page.get('history', [])
                           for added in record.get('messagesAdded', [])]
                    self._process_ids(ids)
                    newest = history_id(page['historyId'])
                    page_token = page.get('nextPageToken')
                    if not page_token:
                        break
                    if page_token in seen_tokens:
                        raise RuntimeError('repeated-gmail-page-token')
                    seen_tokens.add(page_token)
            except HistoryExpired:
                # Capture a new baseline before the full search; later arrivals
                # remain in history even if they occur during search pagination.
                newest = history_id(self.gmail.profile()['historyId'])
                self._search_recovery()
                result = 'history-expired-recovered'
            else:
                result = 'history-reconciled'
            if mail_receiver.pending_ids(self.state_file):
                raise RuntimeError('pending-gmail-messages')
            if int(newest) < int(cursor):
                raise ValueError('gmail-history-regressed')
            self._save(newest)
            return {'status': result}

    def handle_notification(self, data):
        notice = json.loads(data)
        if notice.get('emailAddress', '').lower() != self.config['gmail_email'].lower():
            raise ValueError('gmail-notification-account-mismatch')
        history_id(notice.get('historyId'))
        return self.reconcile()


def on_pubsub_message(receiver, message):
    try:
        receiver.handle_notification(message.data)
    except Exception:
        message.nack()
        return False
    message.ack()
    return True


def keychain_oauth_info(service, scope):
    result = subprocess.run(
        ['security', 'find-generic-password', '-s', service,
         '-a', 'personal-gmail', '-w'],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError('personal-gmail-keychain-unavailable')
    try:
        info = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError('invalid-personal-gmail-keychain-entry') from error
    if not isinstance(info, dict) or not isinstance(info.get('scopes'), list):
        raise ValueError('invalid-personal-oauth-keychain-entry')
    if set(info['scopes']) != {scope} or not info.get('refresh_token'):
        raise ValueError('personal-oauth-scope-or-refresh-token-mismatch')
    return info


def keychain_gmail_credentials():
    from google.oauth2.credentials import Credentials

    scope = 'https://www.googleapis.com/auth/gmail.readonly'
    info = keychain_oauth_info('personal-crashlytics-bot-oauth-user', scope)
    return Credentials.from_authorized_user_info(info, scopes=[scope])


def keychain_pubsub_credentials():
    from google.oauth2.credentials import Credentials

    scope = 'https://www.googleapis.com/auth/pubsub'
    info = keychain_oauth_info('personal-crashlytics-bot-pubsub-user', scope)
    return Credentials.from_authorized_user_info(info, scopes=[scope])


def live_receiver(args):
    # Personal Gmail and Pub/Sub OAuth are separate least-scope Keychain items.
    from googleapiclient.discovery import build
    from google.cloud import pubsub_v1
    credentials = keychain_gmail_credentials()
    gmail = GmailApi(build('gmail', 'v1', credentials=credentials, cache_discovery=False))
    receiver = Receiver(gmail, bot.GitHub(), args.config, args.runtime_config, args.state)
    return receiver, pubsub_v1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--serve', action='store_true')
    modes.add_argument('--renew-watch', action='store_true')
    modes.add_argument('--recover', action='store_true')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--runtime-config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    receiver, pubsub_v1 = live_receiver(args)
    if args.renew_watch:
        result = receiver.renew_watch()
    elif args.recover:
        result = receiver.reconcile()
    else:
        with pubsub_v1.SubscriberClient(credentials=keychain_pubsub_credentials()) as subscriber:
            future = subscriber.subscribe(receiver.config['subscription_name'],
                                          callback=lambda message: on_pubsub_message(receiver, message))
            try:
                future.result()
            finally:
                future.cancel()
        result = {'status': 'subscriber-stopped'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
