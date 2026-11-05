"""Checks the runner executes itself, so a task's success never rests on a session's claim."""
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import processes

SECRET_PATTERNS = (
    ('private key', re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----')),
    ('AWS access key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('GitHub token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    ('Anthropic API key', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}\b')),
    ('Slack token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b')),
    ('generic assigned secret', re.compile(
        r'''(?i)\b(?:api[_-]?key|secret|passwd|password|auth[_-]?token)\b\s*[:=]\s*['"][^'"\s]{16,}['"]''')),
)
SENSITIVE_PATH = re.compile(
    r'(?:^|/)(?:\.env(?:\.(?!example$|sample$|template$)[^/]+)?|id_rsa|id_ed25519|credentials(?:\.json)?|\.netrc|\.npmrc|\.pypirc)$'
    r'|\.(?:pem|p12|pfx|key)$'
    r'|(?:^|/)\.claude/settings(?:\.local)?\.json$'
    r'|(?:^|/)\.git/',
    re.IGNORECASE)
TEST_PATH = re.compile(r'(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]+$|[._](?:test|spec)\.[^/]+$|_test\.[^/]+$',
                       re.IGNORECASE)
DISABLED_TEST = re.compile(
    r'\b(?:it|test|describe)\.(?:skip|only)\b|\b(?:xit|xdescribe|xtest)\s*\('
    r'|@pytest\.mark\.(?:skip|xfail)|@unittest\.skip|\bunittest\.skip\b|\bpytest\.skip\s*\('
    r'|@Disabled\b|@Ignore\b|#\[ignore\]|\bt\.Skip(?:Now)?\s*\(')
OUTPUT_TAIL = 4000


def run_checks(root, checks, timeout_seconds, should_stop=None):
    """Run the configured commands in order; return {'ok', 'results', 'stopped'}.

    Commands come from the project configuration, which sessions may not edit, and
    run through the shell so npm-style shims and `&&` behave as they do by hand.
    Never raises for a failing check. A failing required check stops the sequence,
    since later checks would only report the same breakage. A stop request ends the
    running check and leaves it out of the results.
    """
    results, ok = [], True
    for check in checks:
        if should_stop and should_stop():
            return {'ok': False, 'results': results, 'stopped': True}
        required = check.get('required', True)
        code, tail, outcome = _run_command(check['command'], root, timeout_seconds, should_stop)
        if outcome == 'stopped':
            return {'ok': False, 'results': results, 'stopped': True}
        if outcome == 'timed_out':
            # Last, because findings keep only the end of this text.
            code, tail = None, f'{tail}\ntimed out after {timeout_seconds}s'
        passed = code == 0
        results.append({'name': check['name'], 'command': check['command'], 'required': required,
                        'passed': passed, 'exit_code': code, 'output_tail': '' if passed else tail})
        if not passed and required:
            ok = False
            break
    return {'ok': ok and bool(results), 'results': results, 'stopped': False}


def _run_command(command, root, timeout_seconds, should_stop):
    """Run one shell command; return (exit code, end of its output, outcome of wait_bounded).

    The output goes to a file because a pipe stays open while any descendant of the shell
    lives, so waiting on one would outlast the timeout. Input is empty because no person
    can answer a prompt.
    """
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=str(root), shell=True, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, **processes.GROUP_OPTIONS)
        outcome = processes.wait_bounded(process, timeout_seconds, should_stop)
        output.seek(0, os.SEEK_END)
        # Four bytes is the longest UTF-8 character, so this keeps OUTPUT_TAIL characters at least.
        output.seek(max(output.tell() - OUTPUT_TAIL * 4, 0))
        tail = output.read().decode('utf-8', errors='replace').replace('\r\n', '\n')[-OUTPUT_TAIL:]
    return process.returncode, tail, outcome


def scan_change(root, git):
    """Return findings for secrets, sensitive files and disabled tests in the pending change."""
    findings = []
    entries = [(status, path) for status, path in git.status_entries() if not git.is_owned(path)]
    for status, path in entries:
        posix = path.replace('\\', '/')
        if 'D' in status:
            if TEST_PATH.search(posix):
                findings.append(f'{posix}: an existing test file was deleted')
            continue
        if SENSITIVE_PATH.search(posix):
            findings.append(f'{posix}: sensitive file; the runner will not commit it')
    added = list(git.added_lines())
    for status, path in entries:
        if status == '??':
            try:
                target = Path(root) / path
                if target.stat().st_size <= 1_000_000:
                    added.extend((path, line) for line in target.read_text(encoding='utf-8').splitlines())
            except (OSError, UnicodeDecodeError):
                continue
    for path, line in added:
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                # The value itself stays out of the finding, which ends up in logs and prompts.
                findings.append(f'{path}: possible {name} in the change')
        if TEST_PATH.search(path.replace('\\', '/')) and DISABLED_TEST.search(line):
            findings.append(f'{path}: a test appears to be disabled: {line.strip()[:120]}')
    return list(dict.fromkeys(findings))


def scan_text_for_secrets(text):
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]
