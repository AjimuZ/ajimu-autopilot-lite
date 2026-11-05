"""Pre-publication audit of the working tree and Git history for material that must stay private.

Looks for credentials, private keys, environment and local-configuration files, personal
paths, conversation logs and runtime folders. It uses patterns, so a clean result lowers the
risk but cannot prove absence. Add a dedicated scanner when one is available.

Usage: python tools/audit_publication.py [REPO_ROOT] [--extra-term TEXT ...]
Exit status: 0 = nothing found, 1 = findings to review.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

SECRETS = (
    ('private key', re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----')),
    ('AWS access key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('GitHub token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    ('Anthropic API key', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}\b')),
    ('Slack token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b')),
    ('assigned secret', re.compile(r'''(?i)\b(?:api[_-]?key|secret|passwd|password|auth[_-]?token)\b\s*[:=]\s*['"][^'"\s]{16,}['"]''')),
    ('personal Windows path', re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b)[A-Za-z0-9._-]+', re.IGNORECASE)),
    ('personal Unix path', re.compile(r'/(?:home|Users)/(?!runner\b|user\b|me\b)[a-z0-9._-]+/', re.IGNORECASE)),
)
BAD_PATHS = re.compile(
    r'(?:^|/)(?:\.env(?:\.[^/]*)?|id_rsa|id_ed25519|credentials(?:\.json)?|\.netrc|settings\.local\.json)$'
    r'|\.(?:pem|p12|pfx|key|jsonl|log)$'
    r'|(?:^|/)(?:\.ajimu-autopilot|\.claude|node_modules|__pycache__)/',
    re.IGNORECASE)
# Documentation that names patterns on purpose, plus the scanners' own pattern tables.
OWN_PATTERN_FILES = {'tools/audit_publication.py', 'skills/ajimu-autopilot-lite/scripts/autopilot_core/quality.py',
                     'tests/test_routing.py', 'tests/test_runner.py', 'tests/test_package.py'}


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace')


def audited_paths(root):
    """Return the files publication would include: tracked plus untracked, not ignored."""
    result = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    if result.returncode == 0:
        return [path for path in result.stdout.split('\0') if path]
    return [p.relative_to(root).as_posix() for p in Path(root).rglob('*') if p.is_file() and '.git' not in p.parts]


def scan_text(label, path, text, patterns, findings):
    for line_number, line in enumerate(text.splitlines(), 1):
        for name, pattern in patterns:
            if pattern.search(line):
                # The matched value stays out of the report, which may be pasted elsewhere.
                findings.append(f'{label}: {path}:{line_number}: {name}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('root', nargs='?', default='.')
    parser.add_argument('--extra-term', action='append', default=[], help='also flag this literal text')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    extra = [(f'term "{term}"', re.compile(re.escape(term), re.IGNORECASE)) for term in args.extra_term]
    findings = []
    paths = audited_paths(root)
    for path in paths:
        if BAD_PATHS.search(path):
            findings.append(f'tree: {path}: file should not be published')
        try:
            text = (root / path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        patterns = extra if path in OWN_PATTERN_FILES else SECRETS + tuple(extra)
        scan_text('tree', path, text, patterns, findings)
    history = git(root, 'log', '--all', '-p', '--no-color', '--format=commit %H%n%an <%ae>%n%B')
    if history.returncode == 0 and history.stdout:
        current = None
        for line_number, line in enumerate(history.stdout.splitlines(), 1):
            if line.startswith('+++ b/'):
                current = line[6:]
            if not line.startswith('+') or current in OWN_PATTERN_FILES:
                continue
            for name, pattern in SECRETS + tuple(extra):
                if pattern.search(line):
                    findings.append(f'history: {current}: {name}')
    findings = list(dict.fromkeys(findings))
    for finding in findings:
        print(finding)
    print(f'{len(paths)} files audited; {len(findings)} finding(s)')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
