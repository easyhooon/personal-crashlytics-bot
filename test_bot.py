import copy
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
