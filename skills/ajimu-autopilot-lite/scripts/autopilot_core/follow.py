"""Show one project's run as it happens, from a process that cannot affect it, and open its window.

Everything here is per project, so runs in several projects at once each get their own view.
A view reads `run.log` and the session logs, which are only ever appended to, and opens them just
for the length of a read: on Windows a file held open could not be replaced or deleted by the
runner. It reads no state file, takes no project lock, reads no input and sends nothing back, so
it can start, fail or be closed at any moment without the run noticing.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .store import RunnerLock

NO_WINDOW_VARIABLE = 'AJIMU_AUTOPILOT_NO_WINDOW'
SCRIPT = Path(__file__).resolve().parents[1] / 'autopilot.py'
POLL_SECONDS = 1
HISTORY_BYTES = 4096
TEXT_LIMIT = 200
TOOL_DETAILS = ('file_path', 'command', 'pattern', 'path', 'description')
SESSION_LOG_NAME = re.compile(r'\d{8}-\d{6}-(.+)')
# Sessions write what they read from the repository, and a terminal obeys escape sequences.
UNSAFE_TEXT = re.compile(r'[\x00-\x1f\x7f-\x9f]+')
LINUX_TERMINALS = (('x-terminal-emulator', '-e'), ('gnome-terminal', '--'), ('konsole', '-e'),
                   ('xfce4-terminal', '-x'), ('xterm', '-e'))


def clean(text, limit=TEXT_LIMIT):
    """Return `text` on one line, safe to print to a terminal, at most `limit` characters long."""
    text = UNSAFE_TEXT.sub(' ', text).strip()
    return text if len(text) <= limit else text[:limit - 3] + '...'


def describe_event(event):
    """Return the lines worth showing for one stream-json event; foreign shapes give none."""
    if not isinstance(event, dict):
        return []
    kind = event.get('type')
    if kind == 'system' and event.get('subtype') == 'init':
        return [f'session started on {event.get("model")}']
    if kind == 'result':
        return [f'session ended: {event.get("subtype")}']
    if kind != 'assistant' or not isinstance(event.get('message'), dict):
        return []
    lines = []
    for block in event['message'].get('content') or []:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text' and str(block.get('text') or '').strip():
            lines.append(str(block['text']))
        elif block.get('type') == 'tool_use':
            details = block.get('input') if isinstance(block.get('input'), dict) else {}
            detail = next((details[name] for name in TOOL_DETAILS if isinstance(details.get(name), str)), '')
            lines.append(f'{block.get("name") or "tool"} {detail}'.strip())
    return lines


class LogTail:
    """The complete lines added to a file since the last read. A missing file has none yet."""

    def __init__(self, path, offset=0):
        self.path = Path(path)
        self.offset = offset
        self.partial = b''

    def size(self):
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def lines(self):
        try:
            with open(self.path, 'rb') as handle:
                if handle.seek(0, os.SEEK_END) < self.offset:
                    self.offset, self.partial = 0, b''
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return []
        self.offset += len(data)
        *complete, self.partial = (self.partial + data).split(b'\n')
        return [line.decode('utf-8', errors='replace') for line in complete]


class Follower:
    """Prints what is new in one project on each `tick`. Never raises for what it finds on disk."""

    def __init__(self, project, write=print):
        self.project = project
        self.write = write
        self.run_log = LogTail(project.run_log_path)
        self.session = None
        self.session_label = None
        self.lock_stamp = None
        self.lock_owner = None
        self.active = None

    def start(self):
        """Print the header and the end of the log. A session already under way is joined from its present."""
        self.write(f'Autopilot run of {clean(self.project.root.name)}')
        self.write(clean(str(self.project.root), 1000))
        self.write('Read-only view. Closing it does not affect the run. Ctrl+C closes it.\n')
        self.run_log.offset = max(0, self.run_log.size() - HISTORY_BYTES)
        started_inside_a_line = self.run_log.offset > 0
        history = self.run_log.lines()
        for line in history[1:] if started_inside_a_line else history:
            self.write(clean(line, 1000))
        newest = self.newest_session()
        if newest:
            self.open_session(newest, LogTail(newest).size())

    def newest_session(self):
        """Return the session log created last. Names cannot tell: `probe-*` logs would always sort last."""
        logs = []
        try:
            for path in self.project.logs_dir.glob('*.jsonl'):
                logs.append((path.stat().st_mtime, path.name, path))
        except OSError:
            return None
        return max(logs)[2] if logs else None

    def open_session(self, path, offset=0):
        match = SESSION_LOG_NAME.fullmatch(path.stem)
        # The phase is the last part of the name, and a task ID can hold hyphens of its own.
        task, _, phase = (match.group(1) if match else path.stem).rpartition('-')
        self.session = LogTail(path, offset)
        self.session_label = clean(f'{task} {phase}')

    def show_session_lines(self):
        for line in self.session.lines() if self.session else ():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for text in describe_event(event):
                self.write(f'{datetime.now().strftime("%H:%M:%S")}   {self.session_label}: {clean(text)}')

    def runner_is_active(self):
        """Tell whether a live process owns the project lock.

        The lock is read once per runner, when it appears, because on Windows reading it just as
        the runner releases it would make that release fail. Later checks ask the process itself.
        """
        try:
            stamp = self.project.lock_path.stat().st_mtime_ns
        except OSError:
            return False
        if stamp != self.lock_stamp:
            self.lock_stamp, self.lock_owner = stamp, RunnerLock(self.project.lock_path).owner()
        return bool(self.lock_owner) and RunnerLock.owner_is_live(self.lock_owner)

    def tick(self):
        for line in self.run_log.lines():
            self.write(clean(line, 1000))
        # Found before draining, so the log it replaces has been fully written by then.
        newest = self.newest_session()
        self.show_session_lines()
        if newest and (self.session is None or newest != self.session.path):
            self.open_session(newest)
            self.show_session_lines()
        active = self.runner_is_active()
        if active != self.active:
            self.write('runner active' if active else 'no runner is active; this view shows the next one when it starts')
            self.active = active


def follow(project, write=print, sleep=time.sleep):
    """Print the project's run until interrupted. Works before, during and after a run."""
    follower = Follower(project, write)
    follower.start()
    while True:
        follower.tick()
        sleep(POLL_SECONDS)


def window_command(project):
    """Return the Bash command line of a project's window, whose title tells projects apart.

    Only a failed view holds the window, so that its error can be read.
    """
    title = clean(f'Autopilot: {project.root.name} - {project.root}', 1000)
    view = shlex.join([sys.executable, str(SCRIPT), 'follow', '--window', '--project', str(project.root)])
    return (f"printf '\\033]0;%s\\007' {shlex.quote(title)}; "
            f"{view} || read -r -p 'The view failed. Press Enter to close this window. '")


def git_bash():
    """Return the bash.exe of Git for Windows. PATH alone could find WSL's, which runs elsewhere."""
    git = shutil.which('git')
    roots = list(Path(git).parents) if git else []
    roots.append(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git')
    for root in roots:
        if (root / 'git-bash.exe').is_file() and (root / 'bin' / 'bash.exe').is_file():
            return root / 'bin' / 'bash.exe'
    return None


def launch(command, cwd):
    """Start a Bash window running `command`; return why it could not be, or None.

    Must not leave the window holding this process's output: whoever waits for a background run
    to end, as a supervising session does, would never see it end. A new console replaces the
    standard handles on Windows, and they would stop reaching it if they were redirected here.
    """
    if sys.platform == 'win32':
        bash = git_bash()
        if not bash:
            return 'Git Bash was not found'
        subprocess.Popen([str(bash), '-c', command], cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return None
    if sys.platform != 'linux':
        return f'windows are not supported on {sys.platform}'
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        return 'this machine has no graphical session'
    for name, flag in LINUX_TERMINALS:
        terminal = shutil.which(name)
        if terminal:
            subprocess.Popen([terminal, flag, 'bash', '-c', command], cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return None
    return 'no terminal emulator was found'


def open_window(project):
    """Open the project's window unless it is open already; return a line to report, or None.

    A supervised run restarts many times, and each project gets one window however many restarts
    or other projects there are. Two starts can race only within a project, which the caller's
    project lock prevents, and a surplus view would close itself. A window never costs a run, so
    any failure becomes the returned line, which says how to follow the run anyway.
    """
    if os.environ.get(NO_WINDOW_VARIABLE) or RunnerLock(project.viewer_lock_path).is_active():
        return None
    try:
        # Not the project folder: on Windows a window left open would keep it from being renamed or deleted.
        reason = launch(window_command(project), str(Path.home()))
    except OSError as error:
        reason = str(error)
    if not reason:
        return None
    return f'no window was opened ({reason}); follow this run with: "{sys.executable}" "{SCRIPT}" follow --project "{project.root}"'
