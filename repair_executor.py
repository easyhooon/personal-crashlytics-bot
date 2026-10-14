"""Event-triggerable, fail-closed Crashlytics issue-to-Draft-PR command."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

import bot
from codex_repair import propose
from crashlytics_mcp import FirebaseMcp
from repair_ci import WORKFLOW
from repair_project import git, verify_and_commit
from repair_publish import branch_for, github_client, publish, remotes
from repair_triage import classify, post_analysis


def prepare_worktree(name, checkout, work_root, issue_number):
    repo, base = bot.TARGETS[name][2:]
    checkout = Path(checkout).resolve(strict=True)
    work_root = Path(work_root).resolve(strict=True)
    if git(checkout, 'status', '--porcelain'):
        raise ValueError('Source checkout contains local changes')
    if git(checkout, 'remote', 'get-url', 'origin') not in remotes(repo):
        raise ValueError('Source checkout remote is not the allowed repository')
    fetch = subprocess.run(['git', 'fetch', 'origin', base], cwd=checkout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if fetch.returncode:
        raise RuntimeError('Target base fetch failed')
    # Without the secret-free verify workflow on the base, no repair can be verified.
    if subprocess.run(['git', 'cat-file', '-e', f'origin/{base}:{WORKFLOW}'], cwd=checkout,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode:
        raise ValueError('Target base lacks the secret-free verify workflow')
    branch = branch_for(name, issue_number)
    destination = work_root / (name + '-' + str(issue_number))
    if destination.exists():
        raise ValueError('Repair worktree already exists; manual reconciliation required')
    subprocess.run(['git', 'worktree', 'add', '-b', branch, str(destination), f'origin/{base}'],
                   cwd=checkout, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=120)
    return destination


def execute(alert, issue_number, checkout, work_root, *, mcp_factory=FirebaseMcp,
            proposer=propose, verifier=verify_and_commit, publisher=publish, gh=None,
            reviewer=None):
    name, target, _, _ = bot.validate(alert)
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
        raise ValueError('Invalid issue number')
    if gh is None:
        gh = github_client()
    bot.verify(gh, target[2])
    with mcp_factory() as mcp:
        issue, event_text = mcp.fetch(alert)
    kind = classify(issue, event_text)
    if kind == 'shell-test-no-code-fix':
        note = post_analysis(gh, alert, issue_number, kind)
        return {'status': 'issue-only-shell-test', 'analysis': note['status']}
    work_root = Path(work_root).resolve(strict=True)
    worktree = work_root / (name + '-' + str(issue_number))
    evidence_path = work_root / ('.' + name + '-' + str(issue_number) + '.evidence.json')
    if worktree.exists():
        if not evidence_path.exists() or evidence_path.stat().st_mode & 0o077:
            raise RuntimeError('Existing repair worktree needs manual reconciliation')
        record = json.loads(evidence_path.read_text())
        if (record.get('issue_id') != alert['issue_id'] or record.get('repo') != target[2]
                or record.get('sample_event') != issue.get('sampleEvent')):
            raise ValueError('Persisted repair evidence identity mismatch')
        result = publisher(name, alert, issue_number, worktree, record['evidence'], gh=gh,
                           reviewer=reviewer)
        return {'status': result['status'], 'pr_url': result['pr_url'], 'worktree': str(worktree)}
    candidate = proposer(alert, checkout)
    if candidate['decision'] != 'propose_fix':
        note = post_analysis(gh, alert, issue_number, 'needs-investigation')
        return {'status': 'issue-only-insufficient-evidence', 'analysis': note['status']}
    if name == 'yeobee' and reviewer != 'LeeOhHyung':
        raise PermissionError('YeoBee reviewer approval is required before repair publication')
    worktree = prepare_worktree(name, checkout, work_root, issue_number)
    try:
        evidence = verifier(name, alert, issue_number, worktree, issue, event_text, candidate)
    except Exception:
        post_analysis(gh, alert, issue_number, 'verification-failed')
        raise
    record = {'issue_id': alert['issue_id'], 'repo': target[2],
              'sample_event': issue.get('sampleEvent'), 'evidence': evidence}
    with tempfile.NamedTemporaryFile(mode='w', dir=work_root, prefix='.repair-evidence-',
                                     delete=False) as output:
        temporary = Path(output.name)
        temporary.chmod(0o600)
        json.dump(record, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(evidence_path)
    result = publisher(name, alert, issue_number, worktree, evidence, gh=gh, reviewer=reviewer)
    return {'status': result['status'], 'pr_url': result['pr_url'], 'worktree': str(worktree)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alert-file', type=Path, required=True)
    parser.add_argument('--issue-number', type=int, required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--reviewer', help='YeoBee counterpart, only after explicit approval')
    args = parser.parse_args()
    alert = json.loads(args.alert_file.read_text())
    result = execute(alert, args.issue_number, args.checkout, args.work_root,
                     reviewer=args.reviewer)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
