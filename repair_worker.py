"""Drain durable repair jobs after a Gmail notification; no periodic scan."""

import argparse
import fcntl
import json
from pathlib import Path
import sqlite3
import time

import bot
import mail_receiver
from repair_executor import execute


MAX_ATTEMPTS = 3
RETRY_SECONDS = 3600


def config_paths(config_path):
    path = Path(config_path).resolve(strict=True)
    if path.stat().st_mode & 0o077:
        raise ValueError('Repair config must be owner-only')
    config = json.loads(path.read_text())
    required = {'yeobee_checkout', 'bandalart_checkout', 'work_root', 'start_epoch'}
    if set(config) not in (required, required | {'yeobee_reviewer'}):
        raise ValueError('Repair config has unexpected keys')
    paths = {key: Path(config[key]).resolve(strict=True) for key in required - {'start_epoch'}}
    if not all(path.is_dir() for path in paths.values()):
        raise ValueError('Repair checkout or work root missing')
    if (not isinstance(config['start_epoch'], int) or isinstance(config['start_epoch'], bool)
            or config['start_epoch'] <= 0 or config['start_epoch'] > int(time.time())):
        raise ValueError('Invalid repair activation epoch')
    paths['start_epoch'] = config['start_epoch']
    if 'yeobee_reviewer' in config:
        if config['yeobee_reviewer'] != 'LeeOhHyung':
            raise ValueError('Unexpected YeoBee reviewer')
        paths['yeobee_reviewer'] = config['yeobee_reviewer']
    return paths


def claim_next(state_file, start_epoch):
    now = int(time.time())
    with sqlite3.connect(state_file, timeout=30) as state:
        state.execute('BEGIN IMMEDIATE')
        # A dead worker releases the file lock; its running job is eligible again.
        state.execute('UPDATE repair_jobs SET state="failed", retry_after=0 WHERE state="running"')
        row = state.execute('SELECT marker, alert_json, issue_number, attempts FROM repair_jobs '
                            'WHERE state IN ("queued", "failed") AND attempts < ? AND retry_after <= ? '
                            'AND created_at >= ? ORDER BY rowid LIMIT 1',
                            (MAX_ATTEMPTS, now, start_epoch)).fetchone()
        if row:
            state.execute('UPDATE repair_jobs SET state="running", attempts=attempts+1 '
                          'WHERE marker=?', (row[0],))
        return row


def finish(state_file, marker, success):
    with sqlite3.connect(state_file, timeout=30) as state:
        if success:
            state.execute('UPDATE repair_jobs SET state="done", retry_after=0 WHERE marker=?', (marker,))
        else:
            state.execute('UPDATE repair_jobs SET state="failed", retry_after=? WHERE marker=?',
                          (int(time.time()) + RETRY_SECONDS, marker))


def drain(state_file, config_path, executor=execute):
    state_file = mail_receiver.prepare_state(state_file)
    config = config_paths(config_path)
    lock_path = state_file.with_suffix('.repair.lock')
    lock_path.touch(mode=0o600, exist_ok=True)
    lock_path.chmod(0o600)
    completed = failed = 0
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while row := claim_next(state_file, config['start_epoch']):
            marker, serialized, issue_number, _ = row
            try:
                alert = json.loads(serialized)
                name = bot.validate(alert)[0]
                if name == 'yeobee' and 'yeobee_reviewer' in config:
                    result = executor(alert, issue_number, config[name + '_checkout'],
                                      config['work_root'], reviewer=config['yeobee_reviewer'])
                else:
                    result = executor(alert, issue_number, config[name + '_checkout'],
                                      config['work_root'])
            except Exception as error:
                finish(state_file, marker, False)
                failed += 1
                # Type only: messages can quote crash or command details.
                print(json.dumps({'at': int(time.time()), 'repair_job': 'failed',
                                  'issue_number': issue_number, 'error': type(error).__name__}),
                      flush=True)
            else:
                finish(state_file, marker, True)
                completed += 1
                result = result if isinstance(result, dict) else {}
                print(json.dumps({'at': int(time.time()), 'repair_job': 'done',
                                  'issue_number': issue_number, 'status': result.get('status'),
                                  'pr_url': result.get('pr_url')}), flush=True)
    return {'completed': completed, 'failed': failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(drain(args.state, args.config)))


if __name__ == '__main__':
    main()
