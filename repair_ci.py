"""Verify an exact repair commit on the target's secret-free GitHub Actions workflow.

The verify workflow lives on the target base branch, runs only on
``crash-verify/**`` pushes, references no secrets and uploads JUnit XML. A
result counts only for the run of that workflow file for the pushed SHA.
"""

import io
import subprocess
import tempfile
import time
import zipfile


WORKFLOW = '.github/workflows/crash-repair-verify.yml'
ARTIFACT = 'test-results'
MAX_REPORT_BYTES = 5_000_000


def verify_branch(name, issue_number, stage, sha):
    if stage not in ('before', 'after') or len(sha) != 40:
        raise ValueError('Invalid verify branch input')
    return f'crash-verify/{name}-{issue_number}-{stage}-{sha[:12]}'


class Actions:
    def __init__(self, gh, repo, checkout, timeout=3600, poll=30, sleep=time.sleep):
        self.gh, self.repo, self.checkout = gh, repo, checkout
        self.timeout, self.poll, self.sleep = timeout, poll, sleep

    def push(self, branch, sha):
        # Plain push: an existing verify branch is never overwritten.
        result = subprocess.run(['git', 'push', 'origin', f'{sha}:refs/heads/{branch}'],
                                cwd=self.checkout, env=self.gh.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode:
            raise RuntimeError('Verify branch push failed')

    def wait(self, branch, sha):
        deadline = time.monotonic() + self.timeout
        while True:
            runs = [run for run in self.gh.api(
                        f'repos/{self.repo}/actions/runs?head_sha={sha}&event=push&per_page=100'
                    ).get('workflow_runs', [])
                    if run.get('path') == WORKFLOW and run.get('head_branch') == branch]
            if len(runs) > 1:
                raise RuntimeError('Ambiguous verify runs for one SHA')
            if runs and runs[0].get('head_sha') == sha and runs[0].get('status') == 'completed':
                return runs[0]
            if time.monotonic() > deadline:
                raise RuntimeError('Verify run did not complete in time')
            self.sleep(self.poll)

    def reports(self, run):
        """Return JUnit XML bytes from the run artifact without writing it to disk."""
        artifacts = [row for row in self.gh.api(
                         f'repos/{self.repo}/actions/runs/{run["id"]}/artifacts').get('artifacts', [])
                     if row.get('name') == ARTIFACT and not row.get('expired')]
        if len(artifacts) != 1:
            return []
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(['gh', 'api', '--hostname', 'github.com',
                                     f'repos/{self.repo}/actions/artifacts/{artifacts[0]["id"]}/zip'],
                                    env=self.gh.env, stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=300)
            if result.returncode:
                raise RuntimeError('Verify artifact download failed')
            output.seek(0)
            archive = zipfile.ZipFile(io.BytesIO(output.read()))
        return [archive.read(item) for item in archive.infolist()
                if item.filename.endswith('.xml') and item.file_size <= MAX_REPORT_BYTES]

    def verify(self, branch, sha):
        self.push(branch, sha)
        run = self.wait(branch, sha)
        return {'conclusion': run.get('conclusion'), 'url': run.get('html_url'),
                'head_sha': run.get('head_sha'), 'reports': self.reports(run)}
