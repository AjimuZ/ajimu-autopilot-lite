"""Git operations the runner may perform. No code path here can force, rebase or reset work.

Only the runner commits and pushes. Sessions are denied those commands, so the
identity, the message check and the hooks apply to every commit in one place.
"""
import re
import subprocess
from pathlib import Path

METADATA_DIR = '.ajimu-autopilot'
IGNORE_RULE = f'{METADATA_DIR}/'
PLAN_DIR = 'scripts/ralph'
OWNED_PREFIXES = (f'{PLAN_DIR}/', IGNORE_RULE)
# The rule as a person may have written it by hand; `.ajimu-autopilot/runtime/` is not it.
IGNORE_LINE = re.compile(rf'/?{re.escape(METADATA_DIR)}(/\*{{0,2}})?\s*\Z')
SAFE_BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]*\Z')


class GitError(RuntimeError):
    """A git command failed; the message holds the command and its output."""


class Git:
    def __init__(self, root, environ=None):
        self.root = str(root)
        self.environ = environ

    def run(self, *args, check=True, timeout=300):
        """Run git and return stdout without its trailing newline; raise GitError on failure."""
        try:
            process = subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True, encoding='utf-8',
                                     errors='replace', timeout=timeout, env=self.environ)
        except subprocess.TimeoutExpired as error:
            raise GitError(f'git {args[0]} timed out after {timeout}s') from error
        if check and process.returncode != 0:
            raise GitError(f'git {" ".join(args)} failed: {(process.stderr or process.stdout).strip()}')
        return process.stdout.rstrip('\n') if process.returncode == 0 else ''

    def ok(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, capture_output=True, env=self.environ).returncode == 0

    def is_repository(self):
        return self.ok('rev-parse', '--is-inside-work-tree')

    def has_commits(self):
        return self.ok('rev-parse', '--verify', 'HEAD')

    def is_root(self):
        """Tell whether this folder is the repository's top level, which status paths are relative to."""
        return self.run('rev-parse', '--show-prefix', check=False) == ''

    def head(self):
        return self.run('rev-parse', 'HEAD')

    def branch(self):
        return self.run('symbolic-ref', '--quiet', '--short', 'HEAD', check=False)

    def branch_exists(self, name):
        return self.ok('show-ref', '--verify', '--quiet', f'refs/heads/{name}')

    def identity(self):
        return self.run('config', 'user.name', check=False), self.run('config', 'user.email', check=False)

    def status_entries(self):
        """Return [(status, path)] for all changes including untracked files, honouring .gitignore."""
        raw = self.run('status', '--porcelain=v1', '-z', '--untracked-files=all')
        fields, entries, index = raw.split('\0'), [], 0
        while index < len(fields):
            entry = fields[index]
            index += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            if 'R' in status or 'C' in status:
                index += 1
            entries.append((status, path))
        return entries

    @staticmethod
    def is_owned(path):
        return path.replace('\\', '/').startswith(OWNED_PREFIXES)

    def foreign_changes(self):
        """Return changed paths outside the orchestrator's own files and its own ignore rule."""
        return [path for _, path in self.status_entries()
                if not self.is_owned(path) and not (path == '.gitignore' and self.ignore_rule_pending())]

    def owned_changes(self):
        return [path for _, path in self.status_entries() if self.is_owned(path)]

    def metadata_ignored(self):
        """Tell whether the rules cover the metadata folder, even where its files are still tracked."""
        return self.ok('check-ignore', '--quiet', '--no-index', f'{METADATA_DIR}/config.json')

    def tracked_metadata(self):
        """Return metadata files Git still tracks, as earlier releases committed them."""
        return self.run('ls-files', '--', METADATA_DIR).splitlines()

    def ignore_rule_pending(self):
        """Tell whether `.gitignore` differs from HEAD by nothing but the metadata rule.

        Any other uncommitted edit makes this False, so the owner's own work in that file
        is never committed by the runner and still counts as a foreign change.
        """
        try:
            current = (Path(self.root) / '.gitignore').read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return False
        committed = self.run('show', 'HEAD:.gitignore', check=False).splitlines()
        added = [line for line in current if IGNORE_LINE.match(line) and line not in committed]
        return bool(added) and [line for line in current if line not in added] == committed

    def ensure_branch(self, name, remote, protected):
        """Move to the working branch, creating it when absent.

        Refuses a switch while project files have uncommitted changes, because they could
        mix into another line. Orchestration files may travel, and Git itself refuses a
        switch that would overwrite them. Raises GitError for a protected branch.
        """
        if not SAFE_BRANCH.match(name):
            raise GitError(f'unsafe branch name: {name!r}')
        if name in protected:
            raise GitError(f'{name} is listed in git.protected_branches; choose another branch or remove it from that list')
        if self.branch() == name:
            return False
        if self.foreign_changes():
            raise GitError(f'cannot switch to {name}: the working tree has uncommitted changes')
        if self.branch_exists(name):
            self.run('switch', name)
            return False
        remote_sha = self.remote_head(remote, name)
        if remote_sha:
            self.fetch_tracking(remote, name)
            self.run('switch', '-c', name, '--track', f'{remote}/{name}')
            return False
        self.run('switch', '-c', name)
        return True

    def remote_url(self, remote):
        return self.run('remote', 'get-url', remote, check=False)

    def remote_head(self, remote, branch):
        """Return the remote branch's commit, or None when absent. Raises GitError when unreachable."""
        output = self.run('ls-remote', '--heads', remote, f'refs/heads/{branch}', timeout=120)
        return output.split()[0] if output else None

    def fetch_tracking(self, remote, branch):
        """Update the remote-tracking ref and nothing else.

        The `+` lets that ref follow rewritten remote history, as Git's default refspec does.
        The local branch stays where it is, and the caller reports the divergence.
        """
        self.run('fetch', remote, f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}', timeout=300)

    def is_ancestor(self, older, newer):
        return self.ok('merge-base', '--is-ancestor', older, newer)

    def push(self, remote, branch):
        """Fast-forward push only, then confirm the remote holds this HEAD.

        Returns the verified commit and raises GitError otherwise. A rejected push
        leaves the local commit in place for the next attempt.
        """
        head = self.head()
        self.run('push', '--set-upstream', remote, f'{branch}:{branch}', timeout=600)
        remote_sha = self.remote_head(remote, branch)
        if remote_sha != head:
            raise GitError(f'push verification failed: remote has {remote_sha}, local has {head}')
        return head

    def is_published(self, remote, branch, commit):
        """Tell whether `commit` is reachable from the remote branch as this clone knows it."""
        remote_sha = self.remote_head(remote, branch)
        if not remote_sha:
            return False
        if remote_sha == commit:
            return True
        if not self.ok('cat-file', '-e', f'{remote_sha}^{{commit}}'):
            self.run('fetch', remote, f'refs/heads/{branch}', timeout=300)
        return self.is_ancestor(commit, remote_sha)

    def commit_for_task(self, task_id, base=None):
        """Return the commit carrying the exact `Task: <id>` line after `base`, or None.

        The exact-line comparison matters: a substring search would let `Task: US-1`
        match `Task: US-10`, and a restart would then skip real work.
        """
        revision = f'{base}..HEAD' if base else 'HEAD'
        log = self.run('log', revision, '--format=%H%x00%B%x01', '--max-count=200', check=False)
        for record in log.split('\x01'):
            if '\0' not in record:
                continue
            sha, body = record.strip().split('\0', 1)
            if any(line.strip() == f'Task: {task_id}' for line in body.splitlines()):
                return sha
        return None

    def stage_all(self):
        """Stage everything .gitignore allows. Ignored files are never force-added."""
        self.run('add', '--all')

    def stage_owned(self):
        """Stage the plan files. The metadata folder is ignored, and Git refuses to add it by name."""
        if (Path(self.root) / PLAN_DIR).exists():
            self.run('add', '--all', '--', PLAN_DIR)

    def stage_metadata_removal(self):
        """Stage the pending ignore rule and stop tracking metadata; the files stay on disk."""
        if self.ignore_rule_pending():
            self.run('add', '--', '.gitignore')
        if self.tracked_metadata():
            self.run('rm', '-r', '--cached', '--quiet', '--', METADATA_DIR)

    def staged_files(self):
        return [path for path in self.run('diff', '--cached', '--name-only', '-z').split('\0') if path]

    def unstage_all(self):
        """Empty the index; the working tree is not touched, so no change can be lost."""
        if self.has_commits():
            self.run('reset', '--quiet')

    def commit(self, message_file):
        """Commit the index with hooks active. Raises GitError when a hook or Git refuses."""
        self.run('commit', '--quiet', '--file', str(message_file), timeout=600)
        return self.head()

    def preserve_foreign_changes(self, label):
        """Move non-orchestrator changes to a stash and return its commit, or None if nothing moved.

        The extra ref keeps the snapshot reachable even if the stash list is later
        cleared. Files in the orchestrator's folders stay where they are.
        """
        if not self.foreign_changes():
            return None
        self.unstage_all()
        # Git refuses an ignored path by name, even as an exclusion, so an untracked metadata folder is left out.
        excluded = [PLAN_DIR] + ([METADATA_DIR] if self.tracked_metadata() else [])
        self.run('stash', 'push', '--include-untracked', '--message', f'ajimu-autopilot: {label}', '--', '.',
                 *[f':(exclude){path}' for path in excluded])
        sha = self.run('rev-parse', 'stash@{0}')
        safe_label = re.sub(r'[^A-Za-z0-9._-]+', '-', label).strip('-')
        self.run('update-ref', f'refs/ajimu-autopilot/preserved/{safe_label}', sha)
        return sha

    def apply_preserved(self, commit):
        """Bring work set aside by `preserve_foreign_changes` back to the tree; the snapshot stays reachable.

        Raises GitError when project files have uncommitted changes, which the work could
        collide with, or when it does not apply cleanly. After a conflict the markers are in
        the tree for a person to resolve or undo.
        """
        if self.foreign_changes():
            raise GitError('uncommitted changes exist; deal with them before restoring preserved work')
        self.run('stash', 'apply', '--quiet', commit)

    def added_lines(self):
        """Return (path, added line) for tracked changes against HEAD.

        The prefixes are fixed because diff.noprefix and diff.mnemonicPrefix would change
        them, and quoting is off for non-ASCII names, either of which would hide a file from
        the scan. A file name is read only from the header after `diff --git`, because an
        added line can also start with `+++ `.
        """
        diff = self.run('-c', 'core.quotepath=off', 'diff', 'HEAD', '--unified=0', '--no-color', '--no-ext-diff',
                        '--src-prefix=a/', '--dst-prefix=b/', timeout=120)
        path, added, in_header = None, [], False
        for line in diff.split('\n'):
            if line.startswith('diff --git '):
                path, in_header = None, True
            elif in_header:
                if line.startswith('+++ '):
                    # Git still quotes names with special characters, and ends names with spaces in a tab.
                    name = line[4:].rstrip('\t').strip('"')
                    path, in_header = name[2:] if name.startswith('b/') else None, False
            elif line.startswith('+') and path:
                added.append((path, line[1:]))
        return added
