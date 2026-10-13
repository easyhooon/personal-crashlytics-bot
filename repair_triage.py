"""Post bounded, idempotent evidence notes on an existing personal crash issue."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import bot
from crashlytics_mcp import FirebaseMcp


def classify(issue, event_text):
    """Recognize only the verified shell test; all other causes remain open."""
    title = issue.get('title', '')
    subtitle = issue.get('subtitle', '')
    if ('CrashedByAdbException' in title or 'CrashedByAdbException' in subtitle) \
            and 'CrashedByAdbException' in event_text:
        return 'shell-test-no-code-fix'
    return 'needs-investigation'


def analysis_body(alert, kind):
    bot.validate(alert)
    digest = hashlib.sha256((alert['app_id'] + ':' + alert['issue_id']).encode()).hexdigest()
    marker = f'<!-- personal-crashlytics-analysis:{digest}:{kind} -->'
    if kind == 'shell-test-no-code-fix':
        note = ('### 확인된 사실\n'
                '- Firebase Crashlytics 이슈와 샘플 이벤트를 읽어 동일한 이슈 ID를 확인했습니다.\n'
                '- 예외 유형은 `CrashedByAdbException`이며, 셸에서 주입한 검증용 크래시입니다.\n\n'
                '### 원인 판단\n'
                '- 앱 코드 결함을 가리키는 근거가 없습니다. 수정 PR은 만들지 않습니다.\n\n'
                '### 추가 정보\n'
                '- 실제 앱 사용 중 재현된 별도 이벤트와 앱 코드 stack이 있으면 새 근거로 다시 분석합니다.')
    elif kind == 'needs-investigation':
        note = ('### 확인된 사실\n'
                '- Firebase Crashlytics 이슈와 샘플 이벤트를 읽어 동일한 이슈 ID를 확인했습니다.\n\n'
                '### 가설과 미확인 사항\n'
                '- 앱 코드 원인, 재현 조건, 수정 효과가 아직 검증되지 않았습니다. '
                '제조사·OS·WebView·외부 의존성의 영향도 조사 대상입니다.\n\n'
                '### 다음 단계\n'
                '- 앱 소유 stack, 재현 조건, 실패하는 회귀 테스트와 수정 후 실제 프로젝트 검증이 확보되기 전에는 수정 PR을 만들지 않습니다.')
    elif kind == 'verification-failed':
        note = ('검증을 마치지 못해 수정 PR을 만들지 않았습니다.\n\n'
                '### 확인된 사실\n'
                '- Firebase 이슈와 샘플 이벤트는 조회했습니다.\n'
                '- 수정 전 실패·수정 후 성공하는 회귀 테스트와 실제 프로젝트 검사 중 필요한 조건을 모두 확인하지 못했습니다.\n\n'
                '### 가설과 추가 정보\n'
                '- 앱 코드 원인과 수정 효과는 미확정입니다. 빌드 환경, 재현 조건과 실패한 검사 결과를 검토해야 합니다.')
    else:
        raise ValueError('unknown analysis classification')
    return marker + '\n' + note


def post_analysis(gh, alert, issue_number, kind):
    _, target, issue_marker, _ = bot.validate(alert)
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
        raise ValueError('invalid GitHub issue number')
    repo = target[2]
    bot.verify(gh, repo)
    issue = gh.api(f'repos/{repo}/issues/{issue_number}')
    if (issue.get('number') != issue_number or issue.get('state') != 'open'
            or 'pull_request' in issue or issue_marker not in (issue.get('body') or '')):
        raise ValueError('GitHub issue identity or state mismatch')
    marker = analysis_body(alert, kind).splitlines()[0]
    comments = gh.pages(f'repos/{repo}/issues/{issue_number}/comments?')
    if any(marker in (row.get('body') or '') for row in comments):
        return {'status': 'existing-analysis', 'repo': repo, 'issue_number': issue_number}
    body = analysis_body(alert, kind)
    gh.api(f'repos/{repo}/issues/{issue_number}/comments', 'POST', {'body': body})
    return {'status': 'analysis-posted', 'repo': repo, 'issue_number': issue_number}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alert-file', type=Path, required=True)
    parser.add_argument('--issue-number', type=int, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    alert = json.loads(args.alert_file.read_text())
    bot.validate(alert)
    with FirebaseMcp() as client:
        issue, event_text = client.fetch(alert)
    kind = classify(issue, event_text)
    if args.dry_run:
        result = {'status': 'validated-no-write', 'classification': kind}
    else:
        # Both target repositories require this actor before GitHub writes.
        subprocess.run(['gh', 'auth', 'switch', '--hostname', 'github.com', '--user', 'easyhooon'],
                       capture_output=True, check=True, timeout=30)
        result = post_analysis(bot.GitHub(), alert, args.issue_number, kind)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
