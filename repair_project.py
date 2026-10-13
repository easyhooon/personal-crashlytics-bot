"""Apply bounded Codex patches and observe a real Gradle regression cycle."""

from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import bot
from repair_policy import assess, safe_path


PROJECT_CHECKS = {
    'yeobee': ['./gradlew', 'spotlessCheck', 'detekt', ':app:assembleDebug'],
    'bandalart': ['./gradlew', 'spotlessCheck', 'detekt', 'allTests',
                  ':androidApp:testDebugUnitTest', ':androidApp:assembleDebug'],
}


def run(command, checkout, timeout=1800):
    with tempfile.TemporaryFile(mode='w+') as output:
        try:
            completed = subprocess.run(command, cwd=checkout, stdout=output, stderr=subprocess.STDOUT,
                                       timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return 124
    return completed.returncode


def git(checkout, *arguments):
    result = subprocess.run(['git', *arguments], cwd=checkout, capture_output=True, text=True,
                            timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f'git {arguments[0]} failed')
    return result.stdout.strip()


def check_patch(patch, expected_path, checkout, new_file):
    if not isinstance(patch, str) or len(patch) > 100_000 or not patch.startswith('diff --git '):
        raise ValueError('Invalid bounded unified patch')
    if not safe_path(expected_path) or not expected_path.endswith('Test.kt' if new_file else '.kt'):
        raise ValueError('Invalid Kotlin target path')
    if any(token in patch for token in ('GIT binary patch', 'new mode ', 'old mode ', 'rename from ',
                                        'rename to ', 'submodule ')):
        raise ValueError('Patch contains unsupported file operation')
    headers = re.findall(r'^diff --git a/([^\n]+) b/([^\n]+)$', patch, re.MULTILINE)
    if headers != [(expected_path, expected_path)]:
        raise ValueError('Patch touches files outside the expected path')
    root = Path(checkout).resolve(strict=True)
    path = root / expected_path
    if not path.parent.resolve(strict=True).is_relative_to(root):
        raise ValueError('Patch path escapes checkout')
    if path.exists() == new_file or path.is_symlink():
        raise ValueError('Expected source/test file state mismatch')
    if new_file and 'new file mode 100644' not in patch:
        raise ValueError('Regression patch must add a new test file')
    if not new_file and 'new file mode ' in patch:
        raise ValueError('Fix patch must edit existing source')
    if re.search(r'(?i)(private.?key|refresh.?token|client.?secret|authorization: bearer)', patch):
        raise ValueError('Patch may contain credentials')
    check = subprocess.run(['git', 'apply', '--check', '--index', '-'], input=patch, text=True,
                           cwd=root, capture_output=True, timeout=30)
    if check.returncode:
        raise ValueError('Patch does not apply cleanly')


def test_contract(name, test_file):
    if not safe_path(test_file) or not test_file.endswith('Test.kt') or '/src/' not in test_file:
        raise ValueError('Invalid regression test path')
    module, source = test_file.split('/src/', 1)
    if not module or not all(re.fullmatch(r'[a-z][a-z0-9-]*', part) for part in module.split('/')):
        raise ValueError('Invalid Gradle module path')
    if name == 'yeobee' and source.startswith('test/kotlin/'):
        task = 'testDebugUnitTest'
        class_path = source[len('test/kotlin/'):]
    elif name == 'bandalart' and source.startswith('commonTest/kotlin/'):
        task = 'testAndroidHostTest'
        class_path = source[len('commonTest/kotlin/'):]
    elif name == 'bandalart' and module == 'androidApp' and source.startswith('test/kotlin/'):
        task = 'testDebugUnitTest'
        class_path = source[len('test/kotlin/'):]
    else:
        raise ValueError('Unsupported project test source set')
    test_class = class_path.removesuffix('.kt').replace('/', '.')
    gradle_task = ':' + ':'.join(module.split('/')) + ':' + task
    return ['./gradlew', gradle_task, '--tests', test_class], module, task, test_class


def report_has_failure(checkout, module, task, test_class):
    reports = (Path(checkout) / module / 'build' / 'test-results' / task).glob('**/*.xml')
    for path in reports:
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            continue
        for case in tree.iter('testcase'):
            if case.get('classname') == test_class and (case.find('failure') is not None
                                                       or case.find('error') is not None):
                return True
    return False


def matching_regression_failure(checkout, module, task, test_class, exception, source_file):
    reports = (Path(checkout) / module / 'build' / 'test-results' / task).glob('**/*.xml')
    for path in reports:
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            continue
        for case in tree.iter('testcase'):
            if case.get('classname') != test_class:
                continue
            for failure in list(case.findall('failure')) + list(case.findall('error')):
                detail = ' '.join((failure.get('type', ''), failure.get('message', ''),
                                   failure.text or ''))
                if exception in detail and source_file in detail:
                    return True
    return False


def observed_exception(event_text):
    found = re.search(r'\b([A-Za-z_$][\w.$]*(?:Exception|Error)):', event_text)
    return found.group(1).rsplit('.', 1)[-1] if found else None


def app_stack_lines(event_text, source_file):
    filename = re.escape(Path(source_file).name)
    return [int(line) for line in re.findall(r'\bat [\w.$]+\s+\(' + filename + r':([0-9]+)\)',
                                             event_text)]


def app_frame_paths(name, event_text, checkout):
    """Map exact app-package stack symbols and Kotlin filenames to unique repo files."""
    package = re.escape(bot.ANDROID_PACKAGES[name])
    frames = re.findall(r'\bat (' + package + r'\.[A-Za-z0-9_.$]+)\s+\(([A-Za-z0-9_]+\.kt):[0-9]+\)',
                        event_text)
    result = set()
    for symbol, filename in frames:
        matches = []
        for path in Path(checkout).rglob(filename):
            if any(part in ('build', '.gradle', '.git') for part in path.parts) or path.is_symlink():
                continue
            relative = path.relative_to(checkout).as_posix()
            if '/src/' not in relative or '/test/' in relative or not safe_path(relative):
                continue
            text = path.read_text(errors='replace')[:4096]
            match = re.search(r'^package\s+([\w.]+)', text, re.MULTILINE)
            if match and symbol.startswith(match.group(1) + '.'):
                matches.append(relative)
        if len(matches) == 1:
            result.add(matches[0])
    return sorted(result)


def verify_and_commit(name, alert, issue_number, checkout, issue, event_text, proposal):
    """Return a commit only after a new test fails before and passes after."""
    bot.validate(alert)
    checkout = Path(checkout).resolve(strict=True)
    if git(checkout, 'status', '--porcelain'):
        raise ValueError('Repair checkout must be clean')
    root = proposal.get('root_cause_file')
    test_file = proposal.get('test_file')
    frames = app_frame_paths(name, event_text, checkout)
    if root not in frames:
        raise ValueError('No matching app-owned frame for proposed source file')
    exception = observed_exception(event_text)
    lines = app_stack_lines(event_text, root)
    if not exception or not lines:
        raise ValueError('No exact exception and source line in sample event')
    if exception not in (proposal.get('causal_explanation') or '') or Path(root).name not in (
            proposal.get('causal_explanation') or ''):
        raise ValueError('Proposal does not connect source and observed exception')
    command, module, task, test_class = test_contract(name, test_file)
    check_patch(proposal.get('regression_patch'), test_file, checkout, True)
    check_patch(proposal.get('fix_patch'), root, checkout, False)
    hunk_lines = [int(value) for value in re.findall(r'^@@ -([0-9]+)',
                                                     proposal['fix_patch'], re.MULTILINE)]
    if not hunk_lines or not any(abs(line - hunk) <= 20 for line in lines for hunk in hunk_lines):
        raise ValueError('Fix patch is not near the observed app stack line')
    subprocess.run(['git', 'apply', '--index', '-'], input=proposal['regression_patch'],
                   text=True, cwd=checkout, check=True, capture_output=True, timeout=30)
    before = run(command, checkout)
    if before == 0 or not matching_regression_failure(checkout, module, task, test_class,
                                                      exception, Path(root).name):
        raise RuntimeError('Regression did not reproduce the observed exception and source')
    subprocess.run(['git', 'apply', '--index', '-'], input=proposal['fix_patch'],
                   text=True, cwd=checkout, check=True, capture_output=True, timeout=30)
    changed = git(checkout, 'diff', '--cached', '--name-only').splitlines()
    if set(changed) != {root, test_file}:
        raise ValueError('Patch changed unexpected files')
    after = run(command, checkout)
    if after != 0 or report_has_failure(checkout, module, task, test_class):
        raise RuntimeError('Regression did not pass after source fix')
    git(checkout, 'commit', '-m', 'fix: Crashlytics 크래시 수정' if name == 'yeobee'
        else 'fix: repair Crashlytics app crash')
    head = git(checkout, 'rev-parse', 'HEAD')
    project_exit = run(PROJECT_CHECKS[name], checkout)
    if git(checkout, 'status', '--porcelain'):
        raise RuntimeError('Project check changed checkout files')
    verified = {'regression_before_exit': before, 'regression_after_exit': after,
                'project_check_exit': project_exit, 'tested_head': head}
    crash = {'event_id': issue.get('sampleEvent'), 'exception_type': issue.get('title'),
             'app_frames': frames,
             'shell_injected': 'CrashedByAdbException' in (issue.get('title') or '')
             and 'CrashedByAdbException' in event_text}
    candidate = {'root_cause_file': root, 'changed_files': changed,
                 'app_fault': True, 'causal_explanation': proposal.get('causal_explanation'),
                 'head_sha': head}
    decision = assess(crash, candidate, verified)
    if decision.action != 'draft_pr':
        raise RuntimeError('Repair evidence gate did not authorize a PR: ' + ', '.join(decision.reasons))
    return {'head_sha': head, 'test_class': test_class, 'root_cause_file': root,
            'verification': verified, 'changed_files': changed}
