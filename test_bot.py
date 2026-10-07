import copy
import unittest
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


if __name__ == '__main__':
    unittest.main()
