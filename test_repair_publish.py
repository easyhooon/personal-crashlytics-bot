import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import bot
from repair_publish import publish


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}
HEAD = 'b' * 40
EVIDENCE = {'head_sha': HEAD, 'root_cause_file': 'module/src/main/Foo.kt',
            'test_class': 'com.yeobee.FooTest'}
TEMPLATE = ('## 🔗 관련 이슈\n- Close #\n\n## 📙 작업 설명\n-\n\n'
            '## 🧪 테스트 내역 (선택)\n\n## 📸 스크린샷 또는 시연 영상 (선택)\n')


class FakeGitHub:
    def __init__(self):
        self.prs = []
        self.posts = []

    def api(self, path, method='GET', data=None):
        if path == 'user':
            return {'login': 'easyhooon'}
        if path == 'repos/YeoBee-official/YeoBee-Android':
            return {'full_name': 'YeoBee-official/YeoBee-Android', 'permissions': {'push': True}}
        if path.endswith('/issues/561'):
            return {'state': 'open', 'body': bot.validate(ALERT)[2]}
        raise AssertionError((path, method))

    def pages(self, path):
        if '/labels?' in path:
            return iter([{'name': '🐞 fix'}])
        return iter(self.prs)


def fake_git(_checkout, *args):
    key = args[0]
    if key == 'status':
        return ''
    if key == 'branch':
        return 'YB-00/#561'
    if key == 'remote':
        return 'https://github.com/YeoBee-official/YeoBee-Android.git'
    if key == 'rev-parse':
        return HEAD if args[1] == 'HEAD' else 'a' * 40
    if key == 'merge-base':
        return 'a' * 40
    if key == 'rev-list':
        return '1'
    raise AssertionError(args)


class PublishTest(unittest.TestCase):
    def test_only_verified_commit_can_create_draft_once(self):
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / '.github' / 'PULL_REQUEST_TEMPLATE.md'
            template.parent.mkdir()
            template.write_text(TEMPLATE)
            gh = FakeGitHub()
            def fake_run(command, **_):
                if command[:3] == ['gh', 'pr', 'create']:
                    body = Path(command[command.index('--body-file') + 1]).read_text()
                    gh.posts.append(command)
                    gh.prs.append({'html_url': 'https://example.test/pr',
                                   'draft': True, 'base': {'ref': 'develop'},
                                   'head': {'ref': 'YB-00/#561'}, 'body': body})
                return subprocess.CompletedProcess(command, 0)
            with patch('repair_publish.git', side_effect=fake_git), \
                    patch('repair_publish.subprocess.run', side_effect=fake_run) as run:
                first = publish('yeobee', ALERT, 561, temp, EVIDENCE, gh=gh,
                                reviewer='LeeOhHyung')
                second = publish('yeobee', ALERT, 561, temp, EVIDENCE, gh=gh,
                                 reviewer='LeeOhHyung')
            self.assertEqual(first['status'], 'created-draft-pr')
            self.assertEqual(second['status'], 'existing-draft-pr')
            self.assertEqual(len(gh.posts), 1)
            self.assertIn('--draft', gh.posts[0])
            self.assertIn('--reviewer', gh.posts[0])
            self.assertIn('수정 전 실패', gh.prs[0]['body'])
            self.assertEqual(sum(1 for call in run.call_args_list
                                 if call.args[0][:2] == ['git', 'push']), 1)

    def test_wrong_commit_stops_before_remote_write(self):
        with tempfile.TemporaryDirectory() as temp:
            gh = FakeGitHub()
            with patch('repair_publish.git', side_effect=fake_git), \
                    patch('repair_publish.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    publish('yeobee', ALERT, 561, temp, {**EVIDENCE, 'head_sha': 'wrong'},
                            gh=gh, reviewer='LeeOhHyung')
            run.assert_not_called()
            self.assertFalse(gh.posts)

    def test_yeobee_reviewer_gate_precedes_remote_write(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('repair_publish.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    publish('yeobee', ALERT, 561, temp, EVIDENCE, gh=FakeGitHub())
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
