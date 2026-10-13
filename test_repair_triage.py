import unittest

import bot
import repair_triage


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}


class FakeGitHub:
    def __init__(self):
        self.comments = []
        self.writes = []
        self.fail_after_write = False

    def api(self, path, method='GET', data=None):
        if path == 'user':
            return {'login': 'easyhooon'}
        if path == 'repos/YeoBee-official/YeoBee-Android':
            return {'full_name': 'YeoBee-official/YeoBee-Android', 'permissions': {'push': True}}
        if path == 'repos/YeoBee-official/YeoBee-Android/issues/561':
            return {'number': 561, 'state': 'open', 'body': bot.validate(ALERT)[2]}
        if path.endswith('/comments') and method == 'POST':
            self.writes.append(data)
            self.comments.append(data)
            if self.fail_after_write:
                self.fail_after_write = False
                raise RuntimeError('response lost')
            return {'id': 1}
        raise AssertionError((path, method))

    def pages(self, path):
        self.assert_path = path
        return iter(self.comments)


class TriageTest(unittest.TestCase):
    def test_shell_crash_is_issue_only_and_comment_is_idempotent(self):
        kind = repair_triage.classify({'title': 'CrashedByAdbException'},
                                      'exceptions: CrashedByAdbException')
        self.assertEqual(kind, 'shell-test-no-code-fix')
        gh = FakeGitHub()
        self.assertEqual(repair_triage.post_analysis(gh, ALERT, 561, kind)['status'], 'analysis-posted')
        self.assertEqual(repair_triage.post_analysis(gh, ALERT, 561, kind)['status'], 'existing-analysis')
        self.assertEqual(len(gh.writes), 1)
        self.assertIn('수정 PR은 만들지 않습니다', gh.writes[0]['body'])
        self.assertNotIn('exceptions:', gh.writes[0]['body'])
        self.assertEqual(repair_triage.post_analysis(gh, ALERT, 561, 'needs-investigation')['status'],
                         'analysis-posted')
        self.assertEqual(len(gh.writes), 2)

    def test_unknown_cause_is_recorded_as_unconfirmed(self):
        kind = repair_triage.classify({'title': 'WebView failed'}, 'some stack')
        self.assertEqual(kind, 'needs-investigation')
        body = repair_triage.analysis_body(ALERT, kind)
        self.assertIn('가설과 미확인 사항', body)
        self.assertIn('제조사·OS·WebView', body)

    def test_retry_after_lost_comment_response_finds_existing_note(self):
        gh = FakeGitHub()
        gh.fail_after_write = True
        with self.assertRaises(RuntimeError):
            repair_triage.post_analysis(gh, ALERT, 561, 'needs-investigation')
        self.assertEqual(repair_triage.post_analysis(gh, ALERT, 561, 'needs-investigation')['status'],
                         'existing-analysis')
        self.assertEqual(len(gh.writes), 1)

    def test_unknown_app_and_invalid_issue_number_stop_before_write(self):
        gh = FakeGitHub()
        with self.assertRaises(ValueError):
            repair_triage.post_analysis(gh, {**ALERT, 'project_id': 'other'}, 561,
                                        'shell-test-no-code-fix')
        with self.assertRaises(ValueError):
            repair_triage.post_analysis(gh, ALERT, True, 'shell-test-no-code-fix')
        self.assertFalse(gh.writes)


if __name__ == '__main__':
    unittest.main()
