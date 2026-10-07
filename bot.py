"""Personal normalized Crashlytics issue sync and explicit PR connectivity probe."""
import argparse
import base64
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote, unquote

TARGETS = {
    'yeobee': ('yeobeeios', '1:434182132125:android:5f42e801adcc2624631f74', 'YeoBee-official/YeoBee-Android', 'develop'),
    'bandalart': ('bandalart-e0288', '1:197590766195:android:0e0a78d2390e6849179006', 'Nexters/BandalArt-KMP', 'main'),
}
ANDROID_PACKAGES = {'yeobee': 'com.yeobee', 'bandalart': 'com.nexters.bandalart'}
START, END = '<!-- personal-crashlytics:start -->', '<!-- personal-crashlytics:end -->'


class GitHub:
    def __init__(self):
        self.env = os.environ.copy()
        if not self.env.get('GH_TOKEN'):
            result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', 'easyhooon'], capture_output=True, text=True, check=True)
            self.env['GH_TOKEN'] = result.stdout.strip()
        if not self.env['GH_TOKEN']:
            raise ValueError('easyhooon credential unavailable')

    def api(self, path, method='GET', data=None):
        command = ['gh', 'api', '--hostname', 'github.com', path, '--method', method]
        if data is not None:
            command += ['--input', '-']
        result = subprocess.run(command, input=json.dumps(data) if data is not None else None, env=self.env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Never include API payloads or credentials in diagnostics.
            raise RuntimeError(f'GitHub {method} {path} failed (exit {result.returncode})')
        return json.loads(result.stdout)

    def pages(self, path):
        # ponytail: bounded full scan, use a durable index if issue volume exceeds 10,000.
        for page in range(1, 101):
            rows = self.api(f'{path}&per_page=100&page={page}')
            yield from rows
            if len(rows) < 100:
                return
        raise RuntimeError('Issue/PR scan limit reached; no write performed')


def validate(alert):
    keys = {'project_id', 'app_id', 'issue_id', 'release'}
    if set(alert) - keys:
        raise ValueError('Only normalized technical fields are accepted')
    for name, target in TARGETS.items():
        if (alert.get('project_id'), alert.get('app_id')) == target[:2]:
            break
    else:
        raise ValueError('Unknown personal app')
    if not isinstance(alert.get('issue_id'), str) or not re.fullmatch('[a-f0-9]{32}', alert['issue_id']):
        raise ValueError('Invalid Crashlytics issue ID')
    release = alert.get('release', 'unknown')
    if not isinstance(release, str) or not re.fullmatch('[A-Za-z0-9._()+-]{1,80}', release):
        raise ValueError('Invalid release')
    identity = ':'.join((*target[:2], alert['issue_id']))
    marker = '<!-- personal-crashlytics:' + hashlib.sha256(identity.encode()).hexdigest() + ' -->'
    block = f"{START}\n{marker}\nProject: `{target[0]}`\nApp: `{target[1]}`\nCrashlytics issue: `{alert['issue_id']}`\nLatest release: `{release}`\n{END}"
    return name, target, marker, block


def verify(gh, repo):
    if gh.api('user')['login'] != 'easyhooon':
        raise ValueError('Only easyhooon may write personal repositories')
    info = gh.api('repos/' + repo)
    if info['full_name'] != repo or not info.get('permissions', {}).get('push'):
        raise ValueError('Repository write permission missing')


def issue(gh, repo, marker, title, body, existing_url=None, create_if_missing=True, before_create=None):
    link_pattern = re.escape(existing_url) + r'(?=$|[?\s<>\"\'\)\]])' if existing_url else None
    def find_matches():
        return [row for row in gh.pages(f'repos/{repo}/issues?state=all')
                if 'pull_request' not in row and (marker in (row.get('body') or '')
                or (link_pattern and re.search(link_pattern, html.unescape(unquote(row.get('body') or '')))))]

    matches = find_matches()
    if len(matches) > 1:
        raise ValueError('Multiple matching issues; manual resolution required')
    if not matches:
        if not create_if_missing:
            raise RuntimeError('Issue create outcome uncertain; manual reconciliation required')
        if before_create:
            before_create()
        try:
            row = gh.api(f'repos/{repo}/issues', 'POST', {'title': title, 'body': body})
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            matches = find_matches()
            if len(matches) == 1:
                return matches[0], 'reconciled-after-create-error'
            if len(matches) > 1:
                raise ValueError('Multiple matching issues; manual resolution required') from error
            raise RuntimeError('Issue create outcome uncertain; manual reconciliation required') from error
        return row, 'created'
    row = matches[0]
    if row['state'] != 'open':
        return row, 'closed-held'
    old = row.get('body') or ''
    if marker not in old and existing_url:
        row = gh.api(f'repos/{repo}/issues/{row["number"]}', 'PATCH', {'body': old.rstrip() + '\n\n' + body})
        return row, 'adopted-existing'
    if START in old and END in old and START in body:
        previous_release = re.search(r'^Latest release: `([A-Za-z0-9._()+-]{1,80})`$', old, re.MULTILINE)
        if previous_release and 'Latest release: `unknown`' in body:
            body = body.replace('Latest release: `unknown`', previous_release.group())
        begin, end = old.index(START), old.index(END) + len(END)
        updated = old[:begin] + body + old[end:]
        if updated != old:
            row = gh.api(f'repos/{repo}/issues/{row["number"]}', 'PATCH', {'body': updated})
            return row, 'updated'
    return row, 'unchanged'


def sync(gh, alert, dry_run=False, create_if_missing=True, before_create=None):
    name, target, marker, body = validate(alert)
    repo = target[2]
    verify(gh, repo)
    if dry_run:
        return {'repo': repo, 'status': 'validated-no-write'}
    link = f'https://console.firebase.google.com/project/{target[0]}/crashlytics/app/android:{ANDROID_PACKAGES[name]}/issues/{alert["issue_id"]}'
    body = body.replace(END, f'Crashlytics: {link}\n{END}')
    title = f'[YB-00] Crashlytics: {alert["issue_id"][:12]}' if name == 'yeobee' else f'[Crashlytics] {name}: {alert["issue_id"][:12]}'
    row, status = issue(gh, repo, marker, title, body, existing_url=link,
                        create_if_missing=create_if_missing, before_create=before_create)
    return {'repo': repo, 'status': status, 'issue_url': row['html_url']}


def probe(gh, name):
    """Explicit synthetic probe only. It does not diagnose or fix a production crash."""
    target = TARGETS[name]
    repo, base = target[2:]
    verify(gh, repo)
    marker = '<!-- personal-crashlytics-connectivity:v1 -->'
    title = '[YB-00] test: 크래시 대응 PR 연결 검증' if name == 'yeobee' else 'test: 크래시 대응 PR 연결 검증'
    item, status = issue(gh, repo, marker, title,
        f'{marker}\n개인 크래시 대응 bot의 이슈 등록과 Draft PR 게시를 검증합니다.\n합성 연결 테스트이며 실제 크래시를 발생시키거나 수정하지 않습니다.')
    if status == 'closed-held':
        return {'repo': repo, 'status': status, 'issue_url': item['html_url']}
    number = item['number']
    head = f'YB-00/#{number}' if name == 'yeobee' else f'codex/crashlytics-connectivity-{number}'
    prs = [row for row in gh.pages(f'repos/{repo}/pulls?state=all') if row['head']['ref'] == head]
    if len(prs) > 1:
        raise ValueError('Multiple matching PRs')
    if prs:
        row = prs[0]
        if row['base']['ref'] != base or marker not in (row.get('body') or ''):
            raise ValueError('Existing PR identity mismatch')
        return {'repo': repo, 'status': 'existing-pr', 'issue_url': item['html_url'], 'pr_url': row['html_url']}
    base_ref = gh.api(f'repos/{repo}/git/ref/heads/{base}')
    # Check refs before creating, so a partial failure can be resumed without force pushes.
    refs = gh.api(f'repos/{repo}/git/matching-refs/heads/{quote(head, safe="")}')
    existing = [ref for ref in refs if ref['ref'] == 'refs/heads/' + head]
    if not existing:
        gh.api(f'repos/{repo}/git/refs', 'POST', {'ref': 'refs/heads/' + head, 'sha': base_ref['object']['sha']})
    readme = gh.api(f'repos/{repo}/contents/README.md?ref={quote(head, safe="")}')
    content = base64.b64decode(readme['content']).decode()
    note = '\n\n<!-- personal-crashlytics-connectivity:v1 -->\n이 변경은 개인 크래시 대응 bot의 Draft PR 연결 검증용입니다. 검증 후 병합 없이 닫습니다.\n'
    if marker not in content:
        gh.api(f'repos/{repo}/contents/README.md', 'PUT', {
            'branch': head, 'sha': readme['sha'], 'message': 'test: 크래시 대응 PR 연결 검증 안내',
            'content': base64.b64encode((content + note).encode()).decode(),
        })
    comparison = gh.api(f'repos/{repo}/compare/{base}...{quote(head, safe="")}')
    files = comparison.get('files', [])
    if len(files) != 1 or files[0]['filename'] != 'README.md' or comparison['ahead_by'] != 1:
        raise ValueError('Probe must contain exactly one README commit')
    if name == 'yeobee':
        template = gh.api(f'repos/{repo}/contents/.github/PULL_REQUEST_TEMPLATE.md?ref={base}')
        body = base64.b64decode(template['content']).decode()
        body = body.replace('- Close #\n', f'- Close #{number}\n').replace('\n-\n', '\n- 개인 크래시 대응 bot의 Draft PR 연결을 검증합니다. 합성 테스트이며 검증 후 병합 없이 닫습니다.\n', 1)
    else:
        body = f'Close #{number}\n\n개인 크래시 대응 bot의 Draft PR 연결을 검증합니다. 합성 테스트이며 검증 후 병합 없이 닫습니다.\n'
    body += '\n' + marker + '\n'
    row = gh.api(f'repos/{repo}/pulls', 'POST', {'title': title, 'body': body, 'base': base, 'head': head, 'draft': True})
    return {'repo': repo, 'status': 'created-pr', 'issue_url': item['html_url'], 'pr_url': row['html_url']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--alert-file', type=Path)
    source.add_argument('--probe', choices=TARGETS)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.probe and args.dry_run:
        parser.error('--dry-run applies to alerts only')
    # ponytail: single-host lock; a second host needs a shared queue before enabling writes.
    with open(Path(tempfile.gettempdir()) / 'personal-crashlytics-bot.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        gh = GitHub()
        result = probe(gh, args.probe) if args.probe else sync(gh, json.loads(args.alert_file.read_text()), args.dry_run)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
