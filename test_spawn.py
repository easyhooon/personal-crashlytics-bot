"""The receiver runs gRPC threads; its child processes must use posix_spawn, not fork."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bot


def record_posix_spawn():
    calls = []
    original = subprocess.Popen._posix_spawn

    def recorder(self, *args, **kwargs):
        calls.append(args[0])
        return original(self, *args, **kwargs)
    return calls, patch.object(subprocess.Popen, '_posix_spawn', recorder)


class SpawnTest(unittest.TestCase):
    def test_github_api_spawns_gh_without_fork(self):
        with tempfile.TemporaryDirectory() as temp:
            gh = Path(temp) / 'gh'
            gh.write_text('#!/bin/sh\necho "{\\"login\\": \\"easyhooon\\"}"\n')
            gh.chmod(0o755)
            env = {'GH_TOKEN': 'fixture', 'PATH': temp + os.pathsep + '/usr/bin:/bin'}
            with patch.dict(os.environ, env, clear=True):
                client = bot.GitHub()
            self.assertEqual(client.gh, str(gh))
            calls, recorder = record_posix_spawn()
            with recorder:
                self.assertEqual(client.api('user'), {'login': 'easyhooon'})
            self.assertEqual(len(calls), 1)

    def test_repair_worker_kick_arguments_select_posix_spawn(self):
        import gmail_push
        receiver = gmail_push.Receiver.__new__(gmail_push.Receiver)
        with tempfile.TemporaryDirectory() as temp:
            receiver.state_file = Path(temp) / 'state.sqlite'
            receiver.repair_config = Path(temp) / 'repair.json'
            with patch('gmail_push.subprocess.Popen') as popen:
                receiver.kick_repair()
            args, kwargs = popen.call_args
            self.assertTrue(os.path.isabs(args[0][0]))
            self.assertNotIn('start_new_session', kwargs)
            log = os.open(Path(temp) / 'probe.log', os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                kwargs.update(stdout=log, stderr=log)
                calls, recorder = record_posix_spawn()
                with recorder:
                    subprocess.Popen(['/usr/bin/true'], **kwargs).wait(10)
            finally:
                os.close(log)
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
