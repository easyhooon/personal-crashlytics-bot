"""Read a single Crashlytics issue and sample event using Firebase's MCP server.

Only the two read-only tools are exposed. Raw event content stays in memory and
must not be written to GitHub, logs or Codex prompts without separate review.
"""

import json
import select
import subprocess
import time

import bot


class FirebaseMcp:
    def __init__(self, command=('firebase', 'mcp', '--tools',
                                'crashlytics_get_issue,crashlytics_list_events'), timeout=30):
        self.command = command
        self.timeout = timeout
        self.process = None
        self.next_id = 0

    def __enter__(self):
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, bufsize=1)
        try:
            self._call('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                      'clientInfo': {'name': 'personal-crashlytics-bot', 'version': '1'}})
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def _send(self, message):
        self.process.stdin.write(json.dumps(message, separators=(',', ':')) + '\n')
        self.process.stdin.flush()

    def _call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError('Firebase MCP read timed out')
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError('Firebase MCP closed before responding')
            response = json.loads(line)
            if response.get('id') != request_id:
                continue
            if response.get('error') or response.get('result', {}).get('isError'):
                raise RuntimeError('Firebase MCP read failed')
            return response.get('result', {})

    def tool(self, name, arguments):
        if name not in ('crashlytics_get_issue', 'crashlytics_list_events'):
            raise ValueError('Only Crashlytics read tools are permitted')
        return self._call('tools/call', {'name': name, 'arguments': arguments})

    def fetch(self, alert):
        """Return bounded verified metadata; retain raw content only for local analysis."""
        _, target, _, _ = bot.validate(alert)
        app_id, issue_id = target[1], alert['issue_id']
        result = self.tool('crashlytics_get_issue', {'appId': app_id, 'issueId': issue_id})
        issue = result.get('structuredContent')
        if (not isinstance(issue, dict) or issue.get('id') != issue_id
                or f'/apps/{app_id}/issues/{issue_id}' not in issue.get('name', '')):
            raise RuntimeError('Crashlytics issue identity mismatch')
        result = self.tool('crashlytics_list_events', {
            'appId': app_id, 'filter': {'issueId': issue_id}, 'pageSize': 1})
        content = result.get('content', [])
        event_text = '\n'.join(part.get('text', '') for part in content if part.get('type') == 'text')
        if not event_text or event_text.strip() in ('[]', "'[]'"):
            raise RuntimeError('Crashlytics sample event unavailable')
        # The MCP response may include user logs and breadcrumbs. Never log it.
        return issue, event_text
