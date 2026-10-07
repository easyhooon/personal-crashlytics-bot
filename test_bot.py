import copy
import multiprocessing
import sqlite3
import subprocess
import threading
import time
import unittest
import tempfile
from pathlib import Path
import mail_receiver
import bot


class FakeGitHub:
    def __init__(self, actor='easyhooon'):
        self.actor = actor
        self.issues = []
        self.prs = []
        self.writes = []
        self.has_branch = False
        self.content = '# App\n'

    def pages(self, path):
        return iter(copy.deepcopy(self.prs if '/pulls?' in path else self.issues))

    def api(self, path, method='GET', data=None):
        if method != 'GET':
            self.writes.append((path, method, data))
        if path == 'user':
            return {'login': self.actor}
        if method == 'GET' and path.count('/') == 2:
            return {'full_name': path[6:], 'permissions': {'push': True}}
        if path.endswith('/issues') and method == 'POST':
            row = dict(data, number=len(self.issues) + 1, state='open', html_url='https://example.test/issue')
            self.issues.append(row)
            return copy.deepcopy(row)
        if '/issues/' in path and method == 'PATCH':
            row = self.issues[int(path.rsplit('/', 1)[1]) - 1]
            row.update(data)
            return copy.deepcopy(row)
        if '/git/ref/heads/' in path:
            return {'object': {'sha': 'base'}}
        if '/git/matching-refs/' in path:
            return [{'ref': self.ref}] if self.has_branch else []
        if path.endswith('/git/refs'):
            self.has_branch, self.ref = True, data['ref']
            return {}
        if '/contents/README.md' in path:
            if method == 'PUT':
                self.content = bot.base64.b64decode(data['content']).decode()
                return {}
            return {'content': bot.base64.b64encode(self.content.encode()).decode(), 'sha': 'readme'}
        if '/compare/' in path:
            return {'ahead_by': 1, 'files': [{'filename': 'README.md'}]}
        if '/contents/.github/PULL_REQUEST_TEMPLATE.md' in path:
            return {'content': bot.base64.b64encode(b'<!-- keep -->\n- Close #\n\n-\n').decode()}
        if path.endswith('/pulls') and method == 'POST':
            row = dict(data, head={'ref': data['head']}, base={'ref': data['base']}, html_url='https://example.test/pr')
            self.prs.append(row)
            return copy.deepcopy(row)
        raise AssertionError((path, method))


class FileGitHub:
    def __init__(self, remote_file, message_id):
        self.remote_file = Path(remote_file)
        self.message_id = message_id

    def pages(self, path):
        return iter(bot.json.loads(self.remote_file.read_text()))

    def api(self, path, method='GET', data=None):
        if path == 'user':
            return {'login': 'easyhooon'}
        if method == 'GET' and path.count('/') == 2:
            return {'full_name': path[6:], 'permissions': {'push': True}}
        if path.endswith('/issues') and method == 'POST':
            (self.remote_file.parent / ('post_' + self.message_id)).touch()
            deadline = time.monotonic() + 8
            while not (self.remote_file.parent / 'release').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not (self.remote_file.parent / 'release').exists():
                raise RuntimeError('test process timeout')
            issues = bot.json.loads(self.remote_file.read_text())
            row = dict(data, number=len(issues) + 1, state='open', html_url='https://example.test/issue')
            issues.append(row)
            self.remote_file.write_text(bot.json.dumps(issues))
            return row
        raise AssertionError((path, method))


def deliver_in_process(remote_file, state, message_id):
    result_file = Path(remote_file).parent / ('result_' + message_id)
    try:
        result = mail_receiver.receive(FileGitHub(remote_file, message_id),
                                       gmail_message(message_id=message_id), state)
    except Exception as error:
        result = {'error': str(error)}
    result_file.write_text(bot.json.dumps(result))


def alert(name='yeobee', release='1.0.0'):
    target = bot.TARGETS[name]
    return {'project_id': target[0], 'app_id': target[1], 'issue_id': 'a' * 32, 'release': release}


class EndToEndTest(unittest.TestCase):
    def test_issue_create_replay_update_preserves_human_notes(self):
        for name in bot.TARGETS:
            with self.subTest(app=name):
                gh = FakeGitHub()
                self.assertEqual(bot.sync(gh, alert(name))['status'], 'created')
                self.assertEqual(bot.sync(gh, alert(name))['status'], 'unchanged')
                gh.issues[0]['body'] += '\nHuman triage note'
                self.assertEqual(bot.sync(gh, alert(name, '1.0.1'))['status'], 'updated')
                self.assertIn('Human triage note', gh.issues[0]['body'])
                self.assertIn('1.0.1', gh.issues[0]['body'])
                incoming = alert(name)
                incoming.pop('release')
                self.assertEqual(bot.sync(gh, incoming)['status'], 'unchanged')
                self.assertIn('1.0.1', gh.issues[0]['body'])
                self.assertEqual(len(gh.issues), 1)

    def test_existing_manual_crashlytics_link_is_adopted(self):
        gh = FakeGitHub()
        link = f'https://console.firebase.google.com/project/yeobeeios/crashlytics/app/android:com.yeobee/issues/{"a" * 32}'
        gh.issues.append({'number': 1, 'state': 'open', 'html_url': 'https://example.test/issue', 'body': f'Human diagnosis\n[{link}]({link}?time=last-seven-days)'})
        self.assertEqual(bot.sync(gh, alert())['status'], 'adopted-existing')
        self.assertIn('Human diagnosis', gh.issues[0]['body'])
        self.assertEqual(bot.sync(gh, alert())['status'], 'unchanged')
        self.assertEqual(len(gh.issues), 1)

    def test_closed_issue_is_held(self):
        gh = FakeGitHub()
        bot.sync(gh, alert())
        gh.issues[0]['state'] = 'closed'
        count = len(gh.writes)
        self.assertEqual(bot.sync(gh, alert(release='2.0'))['status'], 'closed-held')
        self.assertEqual(len(gh.writes), count)

    def test_wrong_account_and_invalid_input_never_write(self):
        cases = [dict(alert(), repo='outside/anything'), dict(alert(), project_id='company'),
                 dict(alert(), release='$(command)'), dict(alert(), issue_id='bad')]
        for value in cases:
            gh = FakeGitHub()
            with self.assertRaises(ValueError):
                bot.sync(gh, value)
            self.assertFalse(gh.writes)
        gh = FakeGitHub('wrong-account')
        with self.assertRaises(ValueError):
            bot.sync(gh, alert())
        with self.assertRaises(ValueError):
            bot.probe(gh, 'yeobee')
        self.assertFalse(gh.writes)

    def test_dry_run_never_writes(self):
        gh = FakeGitHub()
        self.assertEqual(bot.sync(gh, alert(), True)['status'], 'validated-no-write')
        self.assertFalse(gh.writes)

    def test_duplicate_issues_stop_without_write(self):
        gh = FakeGitHub()
        bot.sync(gh, alert())
        gh.issues.append(copy.deepcopy(gh.issues[0]))
        count = len(gh.writes)
        with self.assertRaises(ValueError):
            bot.sync(gh, alert())
        self.assertEqual(len(gh.writes), count)

    def test_probe_creates_draft_pr_and_replay_reuses_it(self):
        for name, target in bot.TARGETS.items():
            with self.subTest(app=name):
                gh = FakeGitHub()
                result = bot.probe(gh, name)
                self.assertEqual(result['status'], 'created-pr')
                row = gh.prs[0]
                self.assertTrue(row['draft'])
                self.assertEqual(row['base']['ref'], target[3])
                self.assertIn('Close #1', row['body'])
                if name == 'yeobee':
                    self.assertIn('<!-- keep -->', row['body'])
                count = len(gh.writes)
                again = bot.probe(gh, name)
                self.assertEqual(again['status'], 'existing-pr')
                self.assertEqual(result['pr_url'], again['pr_url'])
                self.assertEqual(len(gh.writes), count)
                self.assertEqual(len(gh.issues), 1)
                self.assertEqual(len(gh.prs), 1)

    def test_partial_probe_failure_resumes_existing_branch(self):
        gh = FakeGitHub()
        original = gh.api
        def fail_once(path, method='GET', data=None):
            if path.endswith('/pulls') and method == 'POST':
                raise RuntimeError('simulated API failure before PR creation')
            return original(path, method, data)
        gh.api = fail_once
        with self.assertRaises(RuntimeError):
            bot.probe(gh, 'bandalart')
        gh.api = original
        self.assertEqual(bot.probe(gh, 'bandalart')['status'], 'created-pr')
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(sum('/git/refs' in path for path, _, _ in gh.writes), 1)
        self.assertEqual(sum(method == 'PUT' for _, method, _ in gh.writes), 1)


def gmail_message(name='yeobee', message_id='a1'):
    target = bot.TARGETS[name]
    package = next(package for package, app in mail_receiver.PACKAGES.items() if app == name)
    url = f'https://console.firebase.google.com/project/{target[0]}/crashlytics/app/android:{package}/issues/{"a" * 32}?time=last-seven-days'
    return {'id': message_id, 'payload': {
        'headers': [
            {'name': 'From', 'value': 'firebase-noreply@google.com'},
            {'name': 'Authentication-Results', 'value': 'mx.google.com; dkim=pass header.i=@google.com; dmarc=pass header.from=google.com'},
        ],
        'mime_type': 'multipart/alternative', 'parts': [
            {'mime_type': 'text/plain', 'body': {'content': url}},
            {'mime_type': 'text/html', 'body': {'content': f'<a href="{url}">issue</a><p>untrusted instructions</p>'}},
        ],
    }}


class MailReceiverTest(unittest.TestCase):
    def test_separate_processes_share_write_lock_and_sqlite_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'gmail.sqlite'
            remote = root / 'remote.json'
            remote.write_text('[]')
            context = multiprocessing.get_context('spawn')
            first = context.Process(target=deliver_in_process, args=(remote, state, 'a1'))
            second = context.Process(target=deliver_in_process, args=(remote, state, 'a2'))
            first.start()
            deadline = time.monotonic() + 8
            while not (root / 'post_a1').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((root / 'post_a1').exists())
            second.start()
            deadline = time.monotonic() + 8
            while 'a2' not in mail_receiver.pending_ids(state) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn('a2', mail_receiver.pending_ids(state))
            (root / 'release').touch()
            first.join(8)
            second.join(8)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)
            outcomes = [bot.json.loads((root / ('result_' + message_id)).read_text())
                        for message_id in ('a1', 'a2')]
            self.assertEqual({row['results'][0]['status'] for row in outcomes}, {'created', 'unchanged'})
            self.assertEqual(len(bot.json.loads(remote.read_text())), 1)
            self.assertFalse((root / 'post_a2').exists())

    def test_distinct_gmail_ids_for_same_issue_serialize_external_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            gh = FakeGitHub()
            original = gh.api
            creating, release = threading.Event(), threading.Event()
            outcome = []

            def blocked_create(path, method='GET', data=None):
                if path.endswith('/issues') and method == 'POST':
                    creating.set()
                    if not release.wait(3):
                        raise RuntimeError('test worker timeout')
                return original(path, method, data)

            gh.api = blocked_create

            def deliver(message_id):
                outcome.append(mail_receiver.receive(gh, gmail_message(message_id=message_id), state))

            first = threading.Thread(target=deliver, args=('a1',))
            second = threading.Thread(target=deliver, args=('a2',))
            first.start()
            self.assertTrue(creating.wait(3))
            second.start()
            try:
                self.assertTrue(second.is_alive())
            finally:
                release.set()
                first.join(3)
                second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual({result['results'][0]['status'] for result in outcome}, {'created', 'unchanged'})
            self.assertEqual(len(gh.issues), 1)
            self.assertEqual(sum(path.endswith('/issues') and method == 'POST'
                                 for path, method, _ in gh.writes), 1)

    def test_expired_message_lease_during_remote_create_keeps_one_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            gh = FakeGitHub()
            original_api = gh.api
            creating, release = threading.Event(), threading.Event()
            outcomes = []
            original_lease = mail_receiver.LEASE_SECONDS
            mail_receiver.LEASE_SECONDS = 0

            def blocked_create(path, method='GET', data=None):
                if path.endswith('/issues') and method == 'POST':
                    creating.set()
                    if not release.wait(3):
                        raise RuntimeError('test worker timeout')
                return original_api(path, method, data)

            gh.api = blocked_create

            def deliver():
                try:
                    outcomes.append(mail_receiver.receive(gh, gmail_message(), state)['status'])
                except RuntimeError as error:
                    outcomes.append(str(error))

            first = threading.Thread(target=deliver)
            second = threading.Thread(target=deliver)
            try:
                first.start()
                self.assertTrue(creating.wait(3))
                with sqlite3.connect(state) as db:
                    first_owner = db.execute('SELECT lease_owner FROM pending WHERE id=?', ('a1',)).fetchone()[0]
                second.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with sqlite3.connect(state) as db:
                        owner = db.execute('SELECT lease_owner FROM pending WHERE id=?', ('a1',)).fetchone()[0]
                    if owner != first_owner:
                        break
                    time.sleep(0.02)
                self.assertNotEqual(owner, first_owner)
            finally:
                release.set()
                first.join(3)
                second.join(3)
                mail_receiver.LEASE_SECONDS = original_lease
            self.assertEqual(set(outcomes), {'gmail-message-lease-lost', 'processed'})
            self.assertEqual(len(gh.issues), 1)
            self.assertEqual(mail_receiver.pending_ids(state), [])

    def test_lost_create_response_reconciles_remote_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            gh = FakeGitHub()
            original = gh.api

            def lost_response(path, method='GET', data=None):
                result = original(path, method, data)
                if path.endswith('/issues') and method == 'POST':
                    raise RuntimeError('response lost after remote create')
                return result

            gh.api = lost_response
            result = mail_receiver.receive(gh, gmail_message(), state)
            self.assertEqual(result['results'][0]['status'], 'reconciled-after-create-error')
            self.assertEqual(len(gh.issues), 1)
            with sqlite3.connect(state) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM issue_intents').fetchone()[0], 0)

    def test_create_timeout_after_remote_commit_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub()
            original = gh.api

            def timeout_after_create(path, method='GET', data=None):
                result = original(path, method, data)
                if path.endswith('/issues') and method == 'POST':
                    raise subprocess.TimeoutExpired(cmd=['gh', 'api'], timeout=60)
                return result

            gh.api = timeout_after_create
            result = mail_receiver.receive(gh, gmail_message(), Path(tmp) / 'gmail.sqlite')
            self.assertEqual(result['results'][0]['status'], 'reconciled-after-create-error')
            self.assertEqual(len(gh.issues), 1)

    def test_uncertain_create_without_remote_match_blocks_second_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            gh = FakeGitHub()
            original = gh.api

            def timeout_before_create(path, method='GET', data=None):
                if path.endswith('/issues') and method == 'POST':
                    raise RuntimeError('unknown outcome')
                return original(path, method, data)

            gh.api = timeout_before_create
            with self.assertRaisesRegex(RuntimeError, 'outcome uncertain'):
                mail_receiver.receive(gh, gmail_message(), state)
            with sqlite3.connect(state) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM issue_intents').fetchone()[0], 1)
            gh.api = original
            with self.assertRaisesRegex(RuntimeError, 'outcome uncertain'):
                mail_receiver.receive(gh, gmail_message(), state)
            self.assertFalse(gh.issues)
            self.assertFalse(any(method == 'POST' for _, method, _ in gh.writes))

            # A delayed remote create becomes visible; retry reconciles without another POST.
            bot.sync(gh, alert())
            count = len(gh.writes)
            self.assertEqual(mail_receiver.receive(gh, gmail_message(), state)['results'][0]['status'], 'unchanged')
            self.assertEqual(len(gh.writes), count)
            self.assertEqual(len(gh.issues), 1)

    def test_same_gmail_id_is_claimed_once_during_concurrent_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            gh = FakeGitHub()
            original = gh.api
            entered, release = threading.Event(), threading.Event()
            outcome = []

            def blocked(path, method='GET', data=None):
                if path == 'user':
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('test worker timeout')
                return original(path, method, data)

            gh.api = blocked

            def first_delivery():
                outcome.append(mail_receiver.receive(gh, gmail_message(), state))

            worker = threading.Thread(target=first_delivery)
            worker.start()
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(mail_receiver.enqueue('a1', state)['status'], 'already-queued')
                with self.assertRaisesRegex(RuntimeError, 'in-progress'):
                    mail_receiver.receive(gh, gmail_message(), state)
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome[0]['status'], 'processed')
            self.assertEqual(mail_receiver.receive(gh, gmail_message(), state)['status'], 'already-processed')
            self.assertEqual(len(gh.issues), 1)

    def test_event_id_is_durable_before_gmail_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            config = Path(tmp) / 'receiver.json'
            now = int(mail_receiver.time.time())
            config.write_text(bot.json.dumps({
                'gmail_link_id': 'personal-link', 'gmail_profile_id': 'personal-profile',
                'start_epoch': now - 3600, 'last_scan_epoch': now - 60,
            }))
            self.assertEqual(mail_receiver.enqueue('a5', state)['status'], 'queued')
            self.assertEqual(mail_receiver.enqueue('a5', state)['status'], 'already-queued')
            with self.assertRaises(ValueError):
                mail_receiver.enqueue('not-a-gmail-id', state)
            plan = mail_receiver.poll_query(config, state)
            self.assertEqual(plan['pending_ids'], ['a5'])
            with self.assertRaisesRegex(RuntimeError, 'pending Gmail'):
                mail_receiver.checkpoint(config, plan['scan_started_epoch'], state)
            gh = FakeGitHub()
            self.assertEqual(mail_receiver.receive(gh, gmail_message(message_id='a5'), state)['status'], 'processed')
            self.assertEqual(mail_receiver.enqueue('a5', state)['status'], 'already-processed')
            mail_receiver.checkpoint(config, plan['scan_started_epoch'], state)

    def test_failed_event_stays_pending_until_retried_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            config = Path(tmp) / 'receiver.json'
            now = int(mail_receiver.time.time())
            config.write_text(bot.json.dumps({
                'gmail_link_id': 'personal-link', 'gmail_profile_id': 'personal-profile',
                'start_epoch': now - 100, 'last_scan_epoch': now - 50,
            }))
            gh = FakeGitHub()
            original = gh.api

            def fail(path, method='GET', data=None):
                if path == 'user':
                    raise RuntimeError('temporary GitHub failure')
                return original(path, method, data)

            gh.api = fail
            with self.assertRaises(RuntimeError):
                mail_receiver.receive(gh, gmail_message(message_id='a3'), state)
            plan = mail_receiver.poll_query(config, state)
            self.assertEqual(plan['pending_ids'], ['a3'])
            with self.assertRaisesRegex(RuntimeError, 'pending Gmail'):
                mail_receiver.checkpoint(config, plan['scan_started_epoch'], state)
            self.assertEqual(bot.json.loads(config.read_text())['last_scan_epoch'], now - 50)
            gh.api = original
            self.assertEqual(mail_receiver.receive(gh, gmail_message(message_id='a3'), state)['status'], 'processed')
            self.assertEqual(mail_receiver.pending_ids(state), [])
            mail_receiver.checkpoint(config, plan['scan_started_epoch'], state)
            self.assertEqual(bot.json.loads(config.read_text())['last_scan_epoch'], plan['scan_started_epoch'])

    def test_expired_lease_reconciles_remote_issue_after_local_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            gh = FakeGitHub()
            message = gmail_message(message_id='a4')
            bot.sync(gh, mail_receiver.normalize(message)[0])
            with sqlite3.connect(state) as db:
                db.execute('CREATE TABLE processed (id TEXT PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP)')
                db.execute('CREATE TABLE pending (id TEXT PRIMARY KEY, lease_owner TEXT, lease_until INTEGER NOT NULL)')
                db.execute('INSERT INTO pending VALUES (?, ?, ?)', ('a4', 'crashed-worker', 0))
            self.assertEqual(mail_receiver.receive(gh, message, state)['results'][0]['status'], 'unchanged')
            self.assertEqual(len(gh.issues), 1)
            self.assertEqual(mail_receiver.pending_ids(state), [])

    def test_real_mail_shape_normalizes_both_apps_without_raw_content(self):
        for name in bot.TARGETS:
            with self.subTest(app=name):
                message = gmail_message(name)
                normalized = mail_receiver.normalize(message)
                self.assertEqual(normalized, [
                    {'project_id': bot.TARGETS[name][0], 'app_id': bot.TARGETS[name][1], 'issue_id': 'a' * 32}])
                self.assertNotIn('untrusted', str(normalized))
                with tempfile.TemporaryDirectory() as tmp:
                    gh = FakeGitHub()
                    result = mail_receiver.receive(gh, message, Path(tmp) / 'state.sqlite', True)
                    self.assertEqual(result['alerts'], 1)
                    self.assertFalse(gh.writes)
                    self.assertFalse((Path(tmp) / 'state.sqlite').exists())

    def test_sender_only_trigger_ignores_non_crash_firebase_mail(self):
        message = gmail_message(message_id='b1')
        for part in message['payload']['parts']:
            part['body']['content'] = 'Firebase billing notice https://console.firebase.google.com/project/yeobeeios/usage'
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub()
            result = mail_receiver.receive(gh, message, Path(tmp) / 'gmail.sqlite')
            self.assertEqual(result['status'], 'ignored-non-target')
            self.assertFalse(gh.writes)

    def test_mail_replay_checkpoint_and_retry_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.sqlite'
            gh = FakeGitHub()
            first = mail_receiver.receive(gh, gmail_message(), state)
            self.assertEqual(first['results'][0]['status'], 'created')
            count = len(gh.writes)
            self.assertEqual(mail_receiver.receive(gh, gmail_message(), state)['status'], 'already-processed')
            self.assertEqual(len(gh.writes), count)
            self.assertEqual(mail_receiver.receive(gh, gmail_message(message_id='a2'), state)['results'][0]['status'], 'unchanged')
            self.assertEqual(len(gh.issues), 1)
            original = gh.api
            def fail(path, method='GET', data=None):
                if path == 'user':
                    raise RuntimeError('auth unavailable')
                return original(path, method, data)
            gh.api = fail
            with self.assertRaises(RuntimeError):
                mail_receiver.receive(gh, gmail_message(message_id='a3'), state)
            gh.api = original
            self.assertEqual(mail_receiver.receive(gh, gmail_message(message_id='a3'), state)['status'], 'processed')
            self.assertEqual(len(gh.issues), 1)

    def test_poll_cursor_overlaps_and_advances_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'receiver.json'
            now = int(mail_receiver.time.time())
            config.write_text(bot.json.dumps({'gmail_link_id': 'test-link', 'gmail_profile_id': 'test-profile', 'start_epoch': now - 200000, 'last_scan_epoch': now - 1000}))
            result = mail_receiver.poll_query(config)
            self.assertIn(f'after:{now - 87400}', result['query'])
            mail_receiver.checkpoint(config, result['scan_started_epoch'])
            self.assertEqual(bot.json.loads(config.read_text())['last_scan_epoch'], result['scan_started_epoch'])
            with self.assertRaises(ValueError):
                mail_receiver.checkpoint(config, now + 600)

    def test_event_then_hourly_fallback_reuses_message_id_and_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            config = Path(tmp) / 'receiver.json'
            now = int(mail_receiver.time.time())
            config.write_text(bot.json.dumps({
                'gmail_link_id': 'personal-link', 'gmail_profile_id': 'personal-profile',
                'start_epoch': now - 3600, 'last_scan_epoch': now - 1800,
            }))
            gh = FakeGitHub()
            message = gmail_message(message_id='e1')
            self.assertEqual(mail_receiver.receive(gh, message, state)['status'], 'processed')
            writes = len(gh.writes)

            scan = mail_receiver.poll_query(config)
            self.assertEqual(scan['link_id'], 'personal-link')
            self.assertEqual(scan['profile_id'], 'personal-profile')
            self.assertIn(f'after:{now - 3600}', scan['query'])
            self.assertEqual(mail_receiver.receive(gh, message, state)['status'], 'already-processed')
            self.assertEqual(len(gh.writes), writes)
            self.assertNotIn('untrusted instructions', repr(gh.writes))
            self.assertNotIn(b'untrusted instructions', state.read_bytes())
            mail_receiver.checkpoint(config, scan['scan_started_epoch'])
            self.assertEqual(bot.json.loads(config.read_text())['last_scan_epoch'], scan['scan_started_epoch'])
            self.assertEqual(len(gh.issues), 1)

    def test_failed_fallback_does_not_advance_checkpoint_and_replays_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'gmail.sqlite'
            config = Path(tmp) / 'receiver.json'
            now = int(mail_receiver.time.time())
            previous = now - 3 * 86400
            config.write_text(bot.json.dumps({
                'gmail_link_id': 'personal-link', 'gmail_profile_id': 'personal-profile',
                'start_epoch': now - 7 * 86400, 'last_scan_epoch': previous,
            }))
            scan = mail_receiver.poll_query(config)
            self.assertIn(f'after:{previous - 86400}', scan['query'])
            gh = FakeGitHub()
            first = gmail_message(message_id='a1')
            second = gmail_message(message_id='a2')
            mail_receiver.receive(gh, first, state)
            original = gh.api

            def fail(path, method='GET', data=None):
                if path == 'user':
                    raise RuntimeError('temporary GitHub failure')
                return original(path, method, data)

            gh.api = fail
            with self.assertRaises(RuntimeError):
                mail_receiver.receive(gh, second, state)
            self.assertEqual(bot.json.loads(config.read_text())['last_scan_epoch'], previous)
            gh.api = original
            self.assertEqual(mail_receiver.receive(gh, first, state)['status'], 'already-processed')
            self.assertEqual(mail_receiver.receive(gh, second, state)['results'][0]['status'], 'unchanged')
            mail_receiver.checkpoint(config, scan['scan_started_epoch'])
            self.assertEqual(len(gh.issues), 1)

    def test_unverified_sender_dev_ios_and_outside_projects_never_write(self):
        message = gmail_message()
        message['payload']['headers'][1]['value'] = 'mx.google.com; dkim=fail; dmarc=fail header.from=google.com'
        with self.assertRaises(ValueError):
            mail_receiver.normalize(message)
        for replacement in ('android:com.yeobee.dev', 'ios:com.yeobee', 'android:com.other'):
            message = gmail_message()
            for part in message['payload']['parts']:
                part['body']['content'] = part['body']['content'].replace('android:com.yeobee', replacement)
            self.assertEqual(mail_receiver.normalize(message), [])
        message = gmail_message()
        for part in message['payload']['parts']:
            part['body']['content'] = part['body']['content'].replace('/project/yeobeeios/', '/project/outside/')
        self.assertEqual(mail_receiver.normalize(message), [])


if __name__ == '__main__':
    unittest.main()
