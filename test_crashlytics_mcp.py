import unittest

import bot
from crashlytics_mcp import FirebaseMcp


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}


class FakeClient(FirebaseMcp):
    def __init__(self, issue_id='a' * 32, event='- exceptions: "sample"'):
        self.issue_id = issue_id
        self.event = event
        self.calls = []

    def tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'crashlytics_get_issue':
            return {'structuredContent': {'id': self.issue_id,
                    'name': f"projects/123/apps/{ALERT['app_id']}/issues/{self.issue_id}"}}
        if name == 'crashlytics_list_events':
            return {'content': [{'type': 'text', 'text': self.event}]}
        raise AssertionError(name)


class McpTest(unittest.TestCase):
    def test_fetch_requires_exact_app_and_issue_and_sample(self):
        client = FakeClient()
        issue, event = client.fetch(ALERT)
        self.assertEqual(issue['id'], ALERT['issue_id'])
        self.assertIn('sample', event)
        self.assertEqual(client.calls[1][1]['filter']['issueId'], ALERT['issue_id'])
        for bad in (FakeClient(issue_id='b' * 32), FakeClient(event='[]')):
            with self.assertRaises(RuntimeError):
                bad.fetch(ALERT)

    def test_unknown_app_never_calls_mcp(self):
        client = FakeClient()
        with self.assertRaises(ValueError):
            client.fetch({**ALERT, 'project_id': 'other'})
        self.assertFalse(client.calls)

    def test_only_read_tools_are_allowed(self):
        with self.assertRaises(ValueError):
            FirebaseMcp().tool('crashlytics_update_issue', {})


if __name__ == '__main__':
    unittest.main()
