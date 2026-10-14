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
from repair_ci import WORKFLOW, Actions
from repair_project import app_frame_paths, verify_and_commit


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}
ROOT = 'module/src/main/kotlin/com/yeobee/Foo.kt'
TEST = 'module/src/test/kotlin/com/yeobee/FooTest.kt'


def command(path, *args):
    result = subprocess.run(['git', *args], cwd=path, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class FakeActions:
    """Simulates the verify workflow from the committed tree of the pushed SHA."""

    def __init__(self, checkout, tamper=None):
        self.checkout, self.tamper, self.calls = checkout, tamper or {}, []

    def verify(self, branch, sha):
        self.calls.append((branch, sha))
        files = command(self.checkout, 'ls-tree', '-r', '--name-only', sha).splitlines()
        fixed = 'fixed' in command(self.checkout, 'show', f'{sha}:{ROOT}')
        reports = []
        if TEST in files:
            failure = '' if fixed else ('<failure type="java.lang.IllegalStateException" '
                                        'message="Foo.kt:2">at Foo.kt:2</failure>')
            reports.append(('<testsuite><testcase classname="com.yeobee.FooTest" name="regression">'
                            + failure + '</testcase></testsuite>').encode())
        result = {'conclusion': 'success' if fixed else 'failure', 'head_sha': sha,
                  'url': f'https://example.test/runs/{sha[:7]}', 'reports': reports}
        stage = 'before' if '-before-' in branch else 'after'
        result.update(self.tamper.get(stage, {}))
        return result


def fixture(path):
    root = Path(path)
    (root / ROOT).parent.mkdir(parents=True)
    (root / TEST).parent.mkdir(parents=True)
    (root / ROOT).write_text('package com.yeobee\nclass Foo { val value = "broken" }\n')
    (root / '.gitignore').write_text('**/build/\n')
    (root / WORKFLOW).parent.mkdir(parents=True)
    (root / WORKFLOW).write_text('name: Crash Repair Verify\n')
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
    def verify(self, temp, tamper=None):
        proposal = fixture(temp)
        ci = FakeActions(temp, tamper)
        event = 'java.lang.IllegalStateException: broken\nat com.yeobee.Foo.crash (Foo.kt:2)'
        result = verify_and_commit('yeobee', ALERT, 561, temp,
                                   {'sampleEvent': 'sample-event', 'title': 'IllegalStateException'},
                                   event, proposal, ci=ci)
        return result, ci

    def test_ci_observes_test_fail_then_exact_head_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            result, ci = self.verify(temp)
            base = command(temp, 'rev-parse', 'HEAD~1')
            self.assertEqual(command(temp, 'rev-list', '--count', 'HEAD'), '2')
            self.assertEqual(result['head_sha'], command(temp, 'rev-parse', 'HEAD'))
            before_branch, before_sha = ci.calls[0]
            after_branch, after_sha = ci.calls[1]
            self.assertTrue(before_branch.startswith('crash-verify/yeobee-561-before-'))
            self.assertTrue(after_branch.startswith('crash-verify/yeobee-561-after-'))
            self.assertEqual(after_sha, result['head_sha'])
            self.assertEqual(command(temp, 'rev-parse', before_sha + '~1'), base)
            self.assertEqual(sorted(result['changed_files']), sorted([ROOT, TEST]))
            self.assertEqual(result['after_run_url'], f'https://example.test/runs/{after_sha[:7]}')
            self.assertEqual(command(temp, 'status', '--porcelain'), '')

    def test_failing_after_run_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'did not pass'):
                self.verify(temp, {'after': {'conclusion': 'failure'}})

    def test_run_for_other_sha_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'did not pass'):
                self.verify(temp, {'after': {'head_sha': 'c' * 40}})

    def test_passing_before_run_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'did not reproduce'):
                self.verify(temp, {'before': {'conclusion': 'success'}})

    def test_unrelated_before_failure_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'did not reproduce'):
                self.verify(temp, {'before': {'reports': [
                    b'<testsuite><testcase classname="com.yeobee.Other" name="x">'
                    b'<failure type="java.lang.IllegalStateException">Foo.kt:2</failure>'
                    b'</testcase></testsuite>']}})

    def test_wrong_exception_before_failure_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'did not reproduce'):
                self.verify(temp, {'before': {'reports': [
                    b'<testsuite><testcase classname="com.yeobee.FooTest" name="x">'
                    b'<failure type="java.lang.NullPointerException">Foo.kt:2</failure>'
                    b'</testcase></testsuite>']}})

    def test_after_run_without_regression_report_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'did not run and pass'):
                self.verify(temp, {'after': {'reports': []}})

    def test_missing_verify_workflow_stops_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            proposal = fixture(temp)
            command(temp, 'rm', '-q', WORKFLOW)
            command(temp, 'commit', '-qm', 'drop workflow')
            event = 'java.lang.IllegalStateException: broken\nat com.yeobee.Foo.crash (Foo.kt:2)'
            ci = FakeActions(temp)
            with self.assertRaisesRegex(ValueError, 'verify workflow'):
                verify_and_commit('yeobee', ALERT, 561, temp,
                                  {'sampleEvent': 'sample-event', 'title': 'IllegalStateException'},
                                  event, proposal, ci=ci)
            self.assertEqual(command(temp, 'rev-list', '--count', 'HEAD'), '2')
            self.assertFalse(ci.calls)

    def test_actions_wait_accepts_only_exact_workflow_branch_and_sha(self):
        sha = 'd' * 40
        branch = 'crash-verify/yeobee-561-after-dddddddddddd'
        responses = [
            {'workflow_runs': [{'path': '.github/workflows/android-ci.yml', 'head_branch': branch,
                                'head_sha': sha, 'status': 'completed', 'conclusion': 'success'}]},
            {'workflow_runs': [{'path': WORKFLOW, 'head_branch': branch, 'head_sha': sha,
                                'status': 'in_progress'}]},
            {'workflow_runs': [{'path': WORKFLOW, 'head_branch': branch, 'head_sha': sha,
                                'status': 'completed', 'conclusion': 'failure'}]},
        ]
        class Gh:
            def api(self, path):
                self.path = path
                return responses.pop(0)
        gh = Gh()
        run = Actions(gh, 'YeoBee-official/YeoBee-Android', '.', sleep=lambda _: None).wait(branch, sha)
        self.assertEqual(run['conclusion'], 'failure')
        self.assertIn(f'head_sha={sha}', gh.path)
        self.assertFalse(responses)

    def test_actions_wait_rejects_ambiguous_runs(self):
        sha = 'd' * 40
        branch = 'crash-verify/x'
        row = {'path': WORKFLOW, 'head_branch': branch, 'head_sha': sha, 'status': 'completed'}
        class Gh:
            def api(self, _):
                return {'workflow_runs': [row, dict(row)]}
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            Actions(Gh(), 'r', '.', sleep=lambda _: None).wait(branch, sha)

    def test_shell_test_stops_before_codex_or_patch(self):
        gh = FakeGitHub()
        def unexpected(*_):
            raise AssertionError('Codex must not run for shell test')
        result = execute(ALERT, 561, '.', '.', mcp_factory=FakeMcp,
                         proposer=unexpected, gh=gh)
        self.assertEqual(result['status'], 'issue-only-shell-test')
        self.assertEqual(len(gh.comments), 1)

    def test_insufficient_evidence_posts_analysis_without_worktree_or_pr(self):
        gh = FakeGitHub()
        def no_fix(*_):
            return {'decision': 'no_code_fix'}
        def unexpected(*_, **__):
            raise AssertionError('No repair step may run without evidence')
        with patch('repair_executor.prepare_worktree', side_effect=unexpected) as worktree:
            result = execute(ALERT, 561, '.', '.', mcp_factory=NormalMcp, proposer=no_fix,
                             verifier=unexpected, publisher=unexpected, gh=gh,
                             reviewer='LeeOhHyung')
        self.assertEqual(result['status'], 'issue-only-insufficient-evidence')
        self.assertEqual(len(gh.comments), 1)
        worktree.assert_not_called()

    def test_base_without_verify_workflow_stops_before_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            origin = Path(temp) / 'origin'
            origin.mkdir()
            command(origin, 'init', '-q', '-b', 'develop')
            command(origin, 'config', 'user.name', 'Fixture')
            command(origin, 'config', 'user.email', 'fixture@example.test')
            (origin / 'README.md').write_text('base\n')
            command(origin, 'add', '.')
            command(origin, 'commit', '-qm', 'base')
            source = Path(temp) / 'source'
            subprocess.run(['git', 'clone', '-q', str(origin), str(source)], check=True)
            command(source, 'remote', 'set-url', 'origin',
                    'git@github-easyhooon:YeoBee-official/YeoBee-Android.git')
            work = Path(temp) / 'work'
            work.mkdir()
            from repair_executor import prepare_worktree
            real = subprocess.run
            def no_network(args, **kwargs):
                if args[:2] == ['git', 'fetch']:
                    return subprocess.CompletedProcess(args, 0)
                return real(args, **kwargs)
            with patch('repair_executor.subprocess.run', side_effect=no_network):
                with self.assertRaisesRegex(ValueError, 'verify workflow'):
                    prepare_worktree('yeobee', source, work, 561)
            self.assertEqual(list(work.iterdir()), [])

    def test_external_stack_cannot_become_app_fix(self):
        with tempfile.TemporaryDirectory() as temp:
            proposal = fixture(temp)
            event = ('java.lang.IllegalStateException: WebView failure\n'
                     'at android.webkit.WebView.loadUrl (WebView.java:42)')
            with self.assertRaisesRegex(ValueError, 'app-owned frame'):
                verify_and_commit('yeobee', ALERT, 561, temp,
                                  {'sampleEvent': 'sample-event', 'title': 'IllegalStateException'},
                                  event, proposal, ci=FakeActions(temp))
            self.assertEqual(command(temp, 'status', '--porcelain'), '')

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
                            proposer=proposal, verifier=verifier, publisher=publisher, gh=gh,
                            reviewer='LeeOhHyung')
                result = execute(ALERT, 561, source, root, mcp_factory=NormalMcp,
                                 proposer=proposal, verifier=verifier, publisher=publisher, gh=gh,
                                 reviewer='LeeOhHyung')
            self.assertEqual(result['status'], 'existing-draft-pr')
            self.assertEqual(calls, {'proposal': 1, 'verification': 1, 'publish': 2})
            self.assertFalse((root / '.yeobee-561.evidence.json').stat().st_mode & 0o077)


if __name__ == '__main__':
    unittest.main()
