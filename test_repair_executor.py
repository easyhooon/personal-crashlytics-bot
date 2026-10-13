"""Isolated fixture checks; these do not replace target app Gradle checks."""

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bot
import mail_receiver
from repair_executor import execute
from repair_project import app_frame_paths, verify_and_commit


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}
ROOT = 'module/src/main/kotlin/com/yeobee/Foo.kt'
TEST = 'module/src/test/kotlin/com/yeobee/FooTest.kt'


def command(path, *args):
    result = subprocess.run(['git', *args], cwd=path, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def fixture(path):
    root = Path(path)
    (root / ROOT).parent.mkdir(parents=True)
    (root / TEST).parent.mkdir(parents=True)
    (root / ROOT).write_text('package com.yeobee\nclass Foo { val value = "broken" }\n')
    (root / '.gitignore').write_text('**/build/\n')
    gradle = root / 'gradlew'
    gradle.write_text('''#!/usr/bin/env python3
from pathlib import Path
import sys
root = Path(__file__).resolve().parent
fixed = 'fixed' in (root / 'module/src/main/kotlin/com/yeobee/Foo.kt').read_text()
if '--tests' in sys.argv:
    report = root / 'module/build/test-results/testDebugUnitTest/TEST-FooTest.xml'
    report.parent.mkdir(parents=True, exist_ok=True)
    failure = '' if fixed else '<failure type="java.lang.IllegalStateException" message="Foo.kt:2">at Foo.kt:2</failure>'
    report.write_text('<testsuite><testcase classname="com.yeobee.FooTest" name="regression">' + failure + '</testcase></testsuite>')
sys.exit(0 if fixed else 1)
''')
    gradle.chmod(0o755)
    command(root, 'init', '-q', '-b', 'main')
    command(root, 'config', 'user.name', 'Fixture')
    command(root, 'config', 'user.email', 'fixture@example.test')
    command(root, 'add', '.')
    command(root, 'commit', '-qm', 'base')
    (root / TEST).write_text('package com.yeobee\nclass FooTest {}\n')
    command(root, 'add', '-N', TEST)
    regression = command(root, 'diff', '--', TEST) + '\n'
    command(root, 'reset', '-q', '--', TEST)
    (root / TEST).unlink()
    (root / ROOT).write_text('package com.yeobee\nclass Foo { val value = "fixed" }\n')
    fix = command(root, 'diff', '--', ROOT) + '\n'
    command(root, 'restore', '--', ROOT)
    return {'decision': 'propose_fix', 'root_cause_file': ROOT, 'test_file': TEST,
            'regression_patch': regression, 'fix_patch': fix,
            'causal_explanation': 'Foo.kt throws the observed IllegalStateException.'}


class FakeMcp:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def fetch(self, _):
        return ({'id': ALERT['issue_id'], 'title': 'CrashedByAdbException',
                 'sampleEvent': 'sample-event'}, 'CrashedByAdbException')


class NormalMcp(FakeMcp):
    def fetch(self, _):
        return ({'id': ALERT['issue_id'], 'title': 'IllegalStateException',
                 'sampleEvent': 'sample-event'},
                'java.lang.IllegalStateException: broken\nat com.yeobee.Foo.crash (Foo.kt:2)')


class FakeGitHub:
    def __init__(self):
        self.comments = []

    def api(self, path, method='GET', data=None):
        if path == 'user':
            return {'login': 'easyhooon'}
        if path == 'repos/YeoBee-official/YeoBee-Android':
            return {'full_name': 'YeoBee-official/YeoBee-Android', 'permissions': {'push': True}}
        if path.endswith('/issues/561'):
            return {'number': 561, 'state': 'open', 'body': bot.validate(ALERT)[2]}
        if path.endswith('/comments') and method == 'POST':
            self.comments.append(data['body'])
            return {'id': 1}
        raise AssertionError(path)

    def pages(self, path):
        return iter([{'body': body} for body in self.comments])


class ExecutorTest(unittest.TestCase):
    def test_fixture_observes_new_test_fail_then_pass_and_project_check(self):
        with tempfile.TemporaryDirectory() as temp:
            proposal = fixture(temp)
            event = 'java.lang.IllegalStateException: broken\nat com.yeobee.Foo.crash (Foo.kt:2)'
            self.assertEqual(app_frame_paths('yeobee', event, Path(temp)), [ROOT])
            result = verify_and_commit('yeobee', ALERT, 561, temp,
                                       {'sampleEvent': 'sample-event', 'title': 'IllegalStateException'},
                                       event, proposal)
            self.assertEqual(result['verification']['regression_before_exit'], 1)
            self.assertEqual(result['verification']['regression_after_exit'], 0)
            self.assertEqual(result['verification']['project_check_exit'], 0)
            self.assertEqual(command(temp, 'status', '--porcelain'), '')

    def test_shell_test_stops_before_codex_or_patch(self):
        gh = FakeGitHub()
        def unexpected(*_):
            raise AssertionError('Codex must not run for shell test')
        result = execute(ALERT, 561, '.', '.', mcp_factory=FakeMcp,
                         proposer=unexpected, gh=gh)
        self.assertEqual(result['status'], 'issue-only-shell-test')
        self.assertEqual(len(gh.comments), 1)

    def test_formal_failure_with_wrong_exception_cannot_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            proposal = fixture(temp)
            proposal['causal_explanation'] = 'Foo.kt throws the observed NullPointerException.'
            event = 'java.lang.NullPointerException: broken\nat com.yeobee.Foo.crash (Foo.kt:2)'
            with self.assertRaises(RuntimeError):
                verify_and_commit('yeobee', ALERT, 561, temp,
                                  {'sampleEvent': 'sample-event', 'title': 'NullPointerException'},
                                  event, proposal)
            self.assertEqual(command(temp, 'rev-list', '--count', 'HEAD'), '1')

    def test_repair_queue_deduplicates_exact_github_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            state_file = mail_receiver.prepare_state(Path(temp) / 'state.sqlite')
            marker = bot.validate(ALERT)[2]
            result = {'status': 'created', 'issue_url':
                      'https://github.com/YeoBee-official/YeoBee-Android/issues/561'}
            with sqlite3.connect(state_file) as state:
                mail_receiver.enqueue_repair(state, ALERT, result, marker)
                mail_receiver.enqueue_repair(state, ALERT, result, marker)
                count = state.execute('SELECT count(*) FROM repair_jobs').fetchone()[0]
            self.assertEqual(count, 1)

    def test_pr_post_failure_resumes_with_persisted_verified_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            gh = FakeGitHub()
            calls = {'proposal': 0, 'verification': 0, 'publish': 0}
            def proposal(*_):
                calls['proposal'] += 1
                return {'decision': 'propose_fix'}
            def verifier(*_):
                calls['verification'] += 1
                return {'head_sha': 'b' * 40}
            def publisher(*_, **__):
                calls['publish'] += 1
                if calls['publish'] == 1:
                    raise RuntimeError('response lost')
                return {'status': 'existing-draft-pr', 'pr_url': 'https://example.test/pr'}
            def worktree(*_):
                dest = root / 'yeobee-561'
                dest.mkdir()
                return dest
            with patch('repair_executor.prepare_worktree', side_effect=worktree):
                with self.assertRaises(RuntimeError):
                    execute(ALERT, 561, source, root, mcp_factory=NormalMcp,
                            proposer=proposal, verifier=verifier, publisher=publisher, gh=gh)
                result = execute(ALERT, 561, source, root, mcp_factory=NormalMcp,
                                 proposer=proposal, verifier=verifier, publisher=publisher, gh=gh)
            self.assertEqual(result['status'], 'existing-draft-pr')
            self.assertEqual(calls, {'proposal': 1, 'verification': 1, 'publish': 2})
            self.assertFalse((root / '.yeobee-561.evidence.json').stat().st_mode & 0o077)


if __name__ == '__main__':
    unittest.main()
