"""Normalize trusted Gmail Crashlytics messages and sync personal GitHub issues."""
import argparse
import base64
import fcntl
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
from email.utils import parseaddr
from urllib.parse import unquote, urlsplit

import bot

PACKAGES = {package: name for name, package in bot.ANDROID_PACKAGES.items()}
GMAIL_QUERY = 'from:firebase-noreply@google.com ("com.yeobee" OR "com.nexters.bandalart") -subject:"ready to test"'
LEASE_SECONDS = 1800


def valid_message_id(message_id):
    if not isinstance(message_id, str) or not re.fullmatch('[a-f0-9]{1,64}', message_id):
        raise ValueError('invalid-gmail-message-id')


def prepare_state(state_file):
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_file.touch(mode=0o600, exist_ok=True)
    state_file.chmod(0o600)
    with sqlite3.connect(state_file, timeout=30) as state:
        state.execute('CREATE TABLE IF NOT EXISTS processed (id TEXT PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        state.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, lease_owner TEXT, lease_until INTEGER NOT NULL)')
        state.execute('CREATE TABLE IF NOT EXISTS issue_intents (marker TEXT PRIMARY KEY, started_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    return state_file


def enqueue(message_id, state_file):
    valid_message_id(message_id)
    state_file = prepare_state(state_file)
    with sqlite3.connect(state_file, timeout=30) as state:
        state.execute('BEGIN IMMEDIATE')
        if state.execute('SELECT 1 FROM processed WHERE id=?', (message_id,)).fetchone():
            return {'status': 'already-processed'}
        row = state.execute('INSERT OR IGNORE INTO pending(id, lease_owner, lease_until) VALUES (?, NULL, 0)',
                            (message_id,))
        return {'status': 'queued' if row.rowcount else 'already-queued'}


def normalize(message):
    payload = message['payload']
    headers = payload.get('headers', [])
    sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
    if parseaddr(sender)[1].lower() != 'firebase-noreply@google.com':
        raise ValueError('sender-not-firebase')
    auth = next((h['value'] for h in headers if h['name'].lower() == 'authentication-results'
                 and h['value'].lstrip().startswith('mx.google.com;')), '')
    if not (re.search(r'\bdkim=pass\b[^;]*\bheader\.i=@google\.com(?:\s|;|$)', auth)
            and re.search(r'\bdmarc=pass\b[^;]*\bheader\.from=google\.com(?:\s|;|$)', auth)):
        raise ValueError('gmail-sender-authentication-failed')

    def text_parts(part):
        if part.get('mime_type', part.get('mimeType')) in ('text/plain', 'text/html'):
            body = part.get('body', {})
            if body.get('content'):
                yield body['content']
            elif body.get('data') or body.get('base64_url_content'):
                data = body.get('data') or body['base64_url_content']
                yield base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode()
        for child in part.get('parts') or []:
            yield from text_parts(child)

    found = {}
    for text in text_parts(payload):
        for value in re.findall(r'https://[^\s<>"\']+', html.unescape(text)):
            url = urlsplit(value)
            if url.netloc != 'console.firebase.google.com':
                continue
            match = re.fullmatch(r'/project/([^/]+)/crashlytics/app/android:([^/]+)/issues/([a-f0-9]{32})', unquote(url.path))
            if not match:
                continue
            project, package, issue_id = match.groups()
            name = PACKAGES.get(package)
            if not name or project != bot.TARGETS[name][0]:
                continue
            target = bot.TARGETS[name]
            alert = {'project_id': project, 'app_id': target[1], 'issue_id': issue_id}
            bot.validate(alert)
            found[(project, target[1], issue_id)] = alert
    return list(found.values())


def receive(gh, message, state_file, dry_run=False):
    message_id = message.get('id', '')
    valid_message_id(message_id)
    alerts = normalize(message)
    if dry_run:
        return {'status': 'dry-run', 'alerts': len(alerts), 'results': [bot.sync(gh, alert, True) for alert in alerts]}
    state_file = prepare_state(state_file)
    owner = uuid.uuid4().hex
    with sqlite3.connect(state_file, timeout=30) as state:
        state.execute('BEGIN IMMEDIATE')
        if state.execute('SELECT 1 FROM processed WHERE id=?', (message_id,)).fetchone():
            return {'status': 'already-processed', 'results': []}
        row = state.execute('SELECT lease_until FROM pending WHERE id=?', (message_id,)).fetchone()
        now = int(time.time())
        if row and row[0] > now:
            raise RuntimeError('gmail-message-in-progress')
        state.execute('INSERT INTO pending(id, lease_owner, lease_until) VALUES (?, ?, ?) '
                      'ON CONFLICT(id) DO UPDATE SET lease_owner=excluded.lease_owner, lease_until=excluded.lease_until',
                      (message_id, owner, now + LEASE_SECONDS))
    try:
        # Remote marker dedup makes a retry safe when an issue write succeeds before local completion.
        results = []
        markers = []
        for alert in alerts:
            result, marker = sync_claimed(gh, alert, state_file)
            results.append(result)
            markers.append(marker)
        with sqlite3.connect(state_file, timeout=30) as state:
            state.execute('BEGIN IMMEDIATE')
            claimed = state.execute('SELECT 1 FROM pending WHERE id=? AND lease_owner=?',
                                    (message_id, owner)).fetchone()
            if not claimed:
                raise RuntimeError('gmail-message-lease-lost')
            state.execute('INSERT INTO processed(id) VALUES (?)', (message_id,))
            state.execute('DELETE FROM pending WHERE id=? AND lease_owner=?', (message_id, owner))
            state.executemany('DELETE FROM issue_intents WHERE marker=?', [(marker,) for marker in markers])
    except Exception:
        # A process crash leaves pending behind; the finite lease allows a later retry.
        with sqlite3.connect(state_file, timeout=30) as state:
            state.execute('UPDATE pending SET lease_until=0 WHERE id=? AND lease_owner=?', (message_id, owner))
        raise
    return {'status': 'processed' if alerts else 'ignored-non-target', 'alerts': len(alerts), 'results': results}


def sync_claimed(gh, alert, state_file):
    marker = bot.validate(alert)[2]
    # The file lock serializes issue lookup and external writes across processes on this Mac.
    lock_file = Path(state_file).with_suffix('.write.lock')
    lock_file.touch(mode=0o600, exist_ok=True)
    lock_file.chmod(0o600)
    with open(lock_file, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(state_file, timeout=30) as state:
            uncertain = state.execute('SELECT 1 FROM issue_intents WHERE marker=?', (marker,)).fetchone()

        def before_create():
            # Persist intent before POST. If the response is lost, a retry may only reconcile.
            with sqlite3.connect(state_file, timeout=30) as state:
                state.execute('INSERT OR IGNORE INTO issue_intents(marker) VALUES (?)', (marker,))

        result = bot.sync(gh, alert, create_if_missing=not uncertain, before_create=before_create)
    return result, marker


def pending_ids(state_file):
    state_file = Path(state_file)
    if not state_file.exists():
        return []
    with sqlite3.connect(state_file) as state:
        if not state.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending'").fetchone():
            return []
        return [row[0] for row in state.execute('SELECT id FROM pending ORDER BY id')]


def poll_query(config_path, state_file=None):
    config = json.loads(Path(config_path).read_text())
    started = int(time.time())
    cutoff = max(config['start_epoch'], config.get('last_scan_epoch', config['start_epoch']) - 86400)
    return {'link_id': config['gmail_link_id'], 'profile_id': config['gmail_profile_id'],
            'query': GMAIL_QUERY + f' after:{cutoff}', 'scan_started_epoch': started,
            'pending_ids': pending_ids(state_file) if state_file else []}


def checkpoint(config_path, started, state_file=None):
    config_path = Path(config_path)
    if state_file and pending_ids(state_file):
        raise RuntimeError('pending Gmail messages must be retried before checkpoint')
    config = json.loads(config_path.read_text())
    previous = config.get('last_scan_epoch', config['start_epoch'])
    if not previous <= started <= int(time.time()):
        raise ValueError('invalid-scan-checkpoint')
    config['last_scan_epoch'] = started
    temporary = config_path.with_suffix('.tmp')
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    with temporary.open('w') as output:
        output.write(json.dumps(config) + '\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(config_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--gmail-file', type=Path)
    source.add_argument('--enqueue-id')
    source.add_argument('--poll-query', action='store_true')
    source.add_argument('--checkpoint', type=int)
    parser.add_argument('--runtime-config', type=Path, default=Path(__file__).parent / 'var' / 'receiver.json')
    parser.add_argument('--state', type=Path, default=Path(__file__).parent / 'var' / 'gmail.sqlite')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.dry_run and not args.gmail_file:
        parser.error('--dry-run applies to Gmail messages only')
    # ponytail: same single-Mac lock as the issue/PR CLI; use a shared queue before adding another host.
    with open(Path(tempfile.gettempdir()) / 'personal-crashlytics-bot.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.enqueue_id is not None:
            result = enqueue(args.enqueue_id, args.state)
        elif args.poll_query:
            result = poll_query(args.runtime_config, args.state)
        elif args.checkpoint is not None:
            checkpoint(args.runtime_config, args.checkpoint, args.state)
            result = {'status': 'scan-checkpointed'}
        else:
            result = receive(bot.GitHub(), json.loads(args.gmail_file.read_text()), args.state, args.dry_run)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
