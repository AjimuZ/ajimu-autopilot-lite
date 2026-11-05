"""Locate Ajimu Conventions and apply its official commit-message validator.

The skill is a required dependency rather than a vendored copy, so every session
works from the rules the owner actually has installed.
"""
import hashlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SKILL = 'ajimu-conventions'
VALIDATOR = 'scripts/check_commit_message.py'
INSTALL_HINT = ('claude plugin marketplace add AjimuZ/ajimu-conventions && '
                'claude plugin install ajimu-conventions@ajimu-conventions')


@dataclass
class Conventions:
    root: Path
    text: str
    digest: str

    @property
    def validator(self):
        return self.root / VALIDATOR


def candidates(project_root=None, home=None):
    """Return the folders where the skill may live, most specific first."""
    home = Path.home() if home is None else Path(home)
    found = []
    if project_root:
        found.append(Path(project_root) / '.claude' / 'skills' / SKILL)
    found.append(home / '.claude' / 'skills' / SKILL)
    plugin_cache = home / '.claude' / 'plugins' / 'cache'
    if plugin_cache.is_dir():
        # Newest install first: the cache can hold several versions, and their folder names need not sort.
        copies = sorted(plugin_cache.glob(f'**/skills/{SKILL}/SKILL.md'), key=lambda path: path.stat().st_mtime, reverse=True)
        found.extend(path.parent for path in copies)
    return found


def locate(project_root=None, home=None):
    """Return the installed skill, or None when no copy has both rules and validator."""
    for folder in candidates(project_root, home):
        skill_file, validator = folder / 'SKILL.md', folder / VALIDATOR
        if skill_file.is_file() and validator.is_file():
            text = skill_file.read_text(encoding='utf-8')
            return Conventions(folder, text, hashlib.sha256(text.encode('utf-8')).hexdigest()[:12])
    return None


def check_message(conventions, message_file):
    """Run the official validator; return (accepted, output). Its exit status decides."""
    process = subprocess.run([sys.executable, str(conventions.validator), str(message_file)], capture_output=True,
                             text=True, encoding='utf-8', errors='replace')
    return process.returncode == 0, (process.stdout + process.stderr).strip()


def refused_task_ids(found, task_ids):
    """Return the IDs the validator refuses on a `Task:` line; no commit of those tasks could ever pass."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'message'

        def accepted(ids):
            path.write_text('Keep the plan current\n\n' + ''.join(f'Task: {task_id}\n' for task_id in ids), encoding='utf-8')
            return check_message(found, path)[0]
        return [] if accepted(task_ids) else [task_id for task_id in task_ids if not accepted([task_id])]
