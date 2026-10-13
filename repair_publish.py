"""Publish a verified one-commit app fix as a Draft PR, never merge it."""

from pathlib import Path
import re
import subprocess
import tempfile

import bot
from repair_project import PROJECT_CHECKS, git


def branch_for(name, issue_number):
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
        raise ValueError('Invalid issue number')
    return f'YB-00/#{issue_number}' if name == 'yeobee' else f'fix/crashlytics-{issue_number}'


def body_for(name, checkout, issue_number, alert, evidence):
    template = (Path(checkout) / '.github' / 'PULL_REQUEST_TEMPLATE.md').read_text()
    for heading in ('## 🔗 관련 이슈', '## 📙 작업 설명', '## 🧪 테스트 내역',
                    '## 📸 스크린샷 또는 시연 영상'):
        if heading not in template:
            raise ValueError('Target PR template is missing a required section')
    if name == 'yeobee':
        if '- Close #\n' not in template:
            raise ValueError('YeoBee PR template issue placeholder changed')
        template = template.replace('- Close #\n', f'- Close #{issue_number}\n', 1)
    else:
        if '- #\n' not in template:
            raise ValueError('BandalArt PR template issue placeholder changed')
        template = template.replace('- #\n', f'- Close #{issue_number}\n', 1)
    root = evidence['root_cause_file']
    test_class = evidence['test_class']
    head = evidence['head_sha']
    marker = '<!-- personal-crashlytics-repair:' + alert['issue_id'] + ' -->'
    summary = (f'Crashlytics 이슈 `{alert["issue_id"]}`의 앱 코드 원인을 `{root}`에서 수정했습니다.\n\n'
               f'확인된 근거: Firebase 샘플 이벤트의 앱 소유 stack 프레임과 `{root}`가 일치합니다. '
               '새 회귀 테스트가 수정 전 실패했고 수정 후 통과했습니다.\n\n')
    description = ('## 📙 작업 설명\n\n'
                   f'- 원인 파일: `{root}`\n- 새 회귀 테스트: `{test_class}`\n')
    template = template.replace('## 📙 작업 설명', description, 1)
    template = template.replace('\n-\n\n## 🧪', '\n\n## 🧪', 1)
    checks = ' '.join(PROJECT_CHECKS[name])
    template = template.replace('## 🧪 테스트 내역 (선택)',
        '## 🧪 테스트 내역 (선택)\n\n'
        f'- `{test_class}`: 수정 전 실패, 수정 후 통과\n'
        f'- `{checks}`: 통과 (`{head[:12]}`)\n'
        '- 기기 재현과 배포 후 Crashlytics 재발 여부: 미검증', 1)
    template = re.sub(r'\|  기능   \|.*?(?=\n## |\Z)',
                      'UI 변경 없음.\n', template, count=1, flags=re.DOTALL)
    return summary + template.rstrip() + '\n\n' + marker + '\n'


def publish(name, alert, issue_number, checkout, evidence, gh=None, reviewer=None):
    _, target, issue_marker, _ = bot.validate(alert)
    if name not in bot.TARGETS or target != bot.TARGETS[name]:
        raise ValueError('App/repository mismatch')
    checkout = Path(checkout).resolve(strict=True)
    repo, base = target[2:]
    branch = branch_for(name, issue_number)
    if name == 'yeobee' and reviewer != 'LeeOhHyung':
        raise ValueError('YeoBee reviewer requires explicit approval')
    if name == 'bandalart' and reviewer is not None:
        raise ValueError('BandalArt reviewer was not requested')
    if git(checkout, 'status', '--porcelain'):
        raise ValueError('Checkout is not clean')
    if git(checkout, 'branch', '--show-current') != branch:
        raise ValueError('Unexpected repair branch')
    if git(checkout, 'remote', 'get-url', 'origin') not in (
            f'https://github.com/{repo}.git', f'git@github.com:{repo}.git'):
        raise ValueError('Unexpected target remote')
    if git(checkout, 'rev-parse', 'HEAD') != evidence['head_sha']:
        raise ValueError('Verified commit no longer matches HEAD')
    if git(checkout, 'merge-base', 'HEAD', f'origin/{base}') != git(checkout, 'rev-parse', f'origin/{base}'):
        raise ValueError('Target base has moved; revalidate the fix')
    if git(checkout, 'rev-list', '--count', f'origin/{base}..HEAD') != '1':
        raise ValueError('Expected exactly one verified repair commit')
    subprocess.run(['gh', 'auth', 'switch', '--hostname', 'github.com', '--user', 'easyhooon'],
                   capture_output=True, check=True, timeout=30)
    gh = gh or bot.GitHub()
    bot.verify(gh, repo)
    item = gh.api(f'repos/{repo}/issues/{issue_number}')
    if item.get('state') != 'open' or issue_marker not in (item.get('body') or ''):
        raise ValueError('Crashlytics issue no longer matches the repair')
    body = body_for(name, checkout, issue_number, alert, evidence)
    marker = '<!-- personal-crashlytics-repair:' + alert['issue_id'] + ' -->'
    label = '🐞 fix' if name == 'yeobee' else 'hotfix'
    if label not in {row.get('name') for row in gh.pages(f'repos/{repo}/labels?')}:
        raise ValueError('Target fix label is unavailable')

    def matching_prs():
        return [pr for pr in gh.pages(f'repos/{repo}/pulls?state=all')
                if pr.get('head', {}).get('ref') == branch]

    prs = matching_prs()
    if prs:
        if len(prs) != 1 or prs[0].get('base', {}).get('ref') != base or marker not in (prs[0].get('body') or ''):
            raise ValueError('Existing PR identity mismatch')
        return {'status': 'existing-draft-pr', 'pr_url': prs[0]['html_url']}
    push = subprocess.run(['git', 'push', 'origin', f'HEAD:refs/heads/{branch}'], cwd=checkout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if push.returncode:
        raise RuntimeError('Verified repair push failed')
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', prefix='crash-repair-pr-',
                                     delete=True) as content:
        content.write(body)
        content.flush()
        command = ['gh', 'pr', 'create', '--repo', repo, '--draft', '--base', base,
                   '--head', branch, '--title', f'fix: Crashlytics {alert["issue_id"][:12]}',
                   '--body-file', content.name, '--assignee', '@me', '--label', label]
        if reviewer:
            command += ['--reviewer', reviewer]
        subprocess.run(command, cwd=checkout, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120)
    prs = matching_prs()
    if (len(prs) != 1 or not prs[0].get('draft') or prs[0].get('base', {}).get('ref') != base
            or marker not in (prs[0].get('body') or '')):
        raise RuntimeError('Draft PR outcome uncertain; manual reconciliation required')
    pr = prs[0]
    return {'status': 'created-draft-pr', 'pr_url': pr['html_url']}
