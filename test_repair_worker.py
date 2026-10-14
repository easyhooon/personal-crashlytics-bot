import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import bot
import mail_receiver
import repair_worker


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}


def setup(temp):
    root = Path(temp)
    state = mail_receiver.prepare_state(root / 'state.sqlite')
    config = root / 'repair.json'
    config.write_text(json.dumps({'yeobee_checkout': str(root),
                                  'bandalart_checkout': str(root), 'work_root': str(root),
                                  'start_epoch': int(time.time()) - 10}))
    config.chmod(0o600)
    with sqlite3.connect(state) as connection:
        mail_receiver.enqueue_repair(connection, ALERT, {
            'status': 'created',
            'issue_url': 'https://github.com/YeoBee-official/YeoBee-Android/issues/561',
        }, bot.validate(ALERT)[2])
    return state, config


class WorkerTest(unittest.TestCase):
    def test_log_lines_carry_outcome_without_error_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            state, config = setup(temp)
            output = io.StringIO()
            def secret_failure(*_):
                raise RuntimeError('crash text that must not be logged')
            with contextlib.redirect_stdout(output):
                repair_worker.drain(state, config, executor=secret_failure)
            line = json.loads(output.getvalue().splitlines()[0])
            self.assertEqual((line['repair_job'], line['issue_number'], line['error']),
                             ('failed', 561, 'RuntimeError'))
            self.assertNotIn('crash text', output.getvalue())
            with sqlite3.connect(state) as db:
                db.execute('UPDATE repair_jobs SET retry_after=0')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                repair_worker.drain(state, config, executor=lambda *_: {
                    'status': 'created-draft-pr', 'pr_url': 'https://example.test/pr'})
            line = json.loads(output.getvalue().splitlines()[0])
            self.assertEqual((line['repair_job'], line['status'], line['pr_url']),
                             ('done', 'created-draft-pr', 'https://example.test/pr'))

    def test_failure_cools_down_then_retry_completes_once(self):
        with tempfile.TemporaryDirectory() as temp:
            state, config = setup(temp)
            calls = []
            def fail(*args):
                calls.append(args)
                raise RuntimeError('fixture failure')
            self.assertEqual(repair_worker.drain(state, config, executor=fail),
                             {'completed': 0, 'failed': 1})
            self.assertEqual(repair_worker.drain(state, config, executor=fail),
                             {'completed': 0, 'failed': 0})
            with sqlite3.connect(state) as db:
                db.execute('UPDATE repair_jobs SET retry_after=0')
            self.assertEqual(repair_worker.drain(state, config, executor=lambda *args: calls.append(args)),
                             {'completed': 1, 'failed': 0})
            self.assertEqual(repair_worker.drain(state, config, executor=fail),
                             {'completed': 0, 'failed': 0})
            self.assertEqual(len(calls), 2)

    def test_interrupted_job_is_reclaimed_after_file_lock_released(self):
        with tempfile.TemporaryDirectory() as temp:
            state, config = setup(temp)
            with self.assertRaises(KeyboardInterrupt):
                repair_worker.drain(state, config, executor=lambda *_: (_ for _ in ()).throw(
                    KeyboardInterrupt()))
            with sqlite3.connect(state) as db:
                self.assertEqual(db.execute('SELECT state FROM repair_jobs').fetchone()[0], 'running')
            self.assertEqual(repair_worker.drain(state, config, executor=lambda *_: None),
                             {'completed': 1, 'failed': 0})

    def test_activation_epoch_excludes_existing_backlog(self):
        with tempfile.TemporaryDirectory() as temp:
            state, config = setup(temp)
            start = json.loads(config.read_text())['start_epoch']
            with sqlite3.connect(state) as db:
                db.execute('UPDATE repair_jobs SET created_at=?', (start - 1,))
            self.assertEqual(repair_worker.drain(state, config, executor=lambda *_: self.fail()),
                             {'completed': 0, 'failed': 0})


if __name__ == '__main__':
    unittest.main()
