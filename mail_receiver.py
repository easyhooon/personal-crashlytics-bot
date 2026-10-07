"""Normalize trusted Gmail Crashlytics messages and sync personal GitHub issues."""
import argparse
import base64
import fcntl
import html
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from email.utils import parseaddr
from urllib.parse import unquote, urlsplit

import bot

PACKAGES = {package: name for name, package in bot.ANDROID_PACKAGES.items()}
GMAIL_QUERY = 'from:firebase-noreply@google.com ("com.yeobee" OR "com.nexters.bandalart") -subject:"ready to test"'


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
    if not isinstance(message_id, str) or not re.fullmatch('[a-f0-9]{1,64}', message_id):
        raise ValueError('invalid-gmail-message-id')
    alerts = normalize(message)
    if dry_run:
        return {'status': 'dry-run', 'alerts': len(alerts), 'results': [bot.sync(gh, alert, True) for alert in alerts]}
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_file.touch(mode=0o600, exist_ok=True)
    state_file.chmod(0o600)
    with sqlite3.connect(state_file) as state:
        state.execute('CREATE TABLE IF NOT EXISTS processed (id TEXT PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        if state.execute('SELECT 1 FROM processed WHERE id=?', (message_id,)).fetchone():
            return {'status': 'already-processed', 'results': []}
        # Remote marker dedup makes a retry safe when one issue write succeeds before another fails.
        results = [bot.sync(gh, alert) for alert in alerts]
        state.execute('INSERT INTO processed(id) VALUES (?)', (message_id,))
    return {'status': 'processed' if alerts else 'ignored-non-target', 'alerts': len(alerts), 'results': results}


def poll_query(config_path):
    config = json.loads(Path(config_path).read_text())
    started = int(time.time())
    cutoff = max(config['start_epoch'], config.get('last_scan_epoch', config['start_epoch']) - 86400)
    return {'link_id': config['gmail_link_id'], 'profile_id': config['gmail_profile_id'],
            'query': GMAIL_QUERY + f' after:{cutoff}', 'scan_started_epoch': started}


def checkpoint(config_path, started):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text())
    previous = config.get('last_scan_epoch', config['start_epoch'])
    if not previous <= started <= int(time.time()):
        raise ValueError('invalid-scan-checkpoint')
    config['last_scan_epoch'] = started
    temporary = config_path.with_suffix('.tmp')
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.write_text(json.dumps(config) + '\n')
    temporary.replace(config_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--gmail-file', type=Path)
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
        if args.poll_query:
            result = poll_query(args.runtime_config)
        elif args.checkpoint is not None:
            checkpoint(args.runtime_config, args.checkpoint)
            result = {'status': 'scan-checkpointed'}
        else:
            result = receive(bot.GitHub(), json.loads(args.gmail_file.read_text()), args.state, args.dry_run)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
