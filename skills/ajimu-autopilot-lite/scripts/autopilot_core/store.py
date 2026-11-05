"""Crash-safe file persistence and the lock that keeps one runner per project."""
import ctypes
import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class LockHeld(Exception):
    """Another live runner owns the project; carries that runner's lock metadata."""

    def __init__(self, owner):
        super().__init__(f'another runner is active: {owner}')
        self.owner = owner


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write_text_atomic(path, text):
    """Replace `path` so a crash leaves either the old or the new content, never a mix."""
    write_bytes_atomic(path, text.encode('utf-8'))


def write_bytes_atomic(path, content):
    """Replace `path` with exactly `content`, with the same crash guarantee."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows refuses the rename while an antivirus or indexer holds the target briefly.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def write_json_atomic(path, data):
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def read_json(path, default=None):
    """Return the parsed file, or `default` when it is absent. Corrupt JSON raises ValueError.

    A byte order mark is accepted, because Windows tools add one to files edited by hand.
    """
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return default
    return json.loads(text)


def pid_alive(pid):
    """Tell whether a process exists without signalling it."""
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        # os.kill(pid, 0) would send CTRL_C_EVENT on Windows, so query the handle instead.
        process_query_limited_information, still_active = 0x1000, 259
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunnerLock:
    """Exclusive project lock. A lock left by a dead process on this host is reclaimed.

    A lock written on another host is never reclaimed automatically, because its
    process cannot be inspected from here.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.acquired = False

    def owner(self):
        try:
            return read_json(self.path)
        except ValueError:
            return {'pid': -1, 'host': socket.gethostname(), 'corrupt': True}

    @staticmethod
    def owner_is_live(owner):
        """Tell whether the process in `owner` lives. Another host's lock counts, as it cannot be inspected."""
        return owner.get('host') != socket.gethostname() or pid_alive(int(owner.get('pid', -1)))

    def is_active(self):
        """Tell whether a live process owns the lock."""
        owner = self.owner()
        return owner is not None and self.owner_is_live(owner)

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self.owner()
                if owner is None:
                    continue
                if self.owner_is_live(owner):
                    raise LockHeld(owner)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump({'pid': os.getpid(), 'host': socket.gethostname(), 'started_at': utc_now()}, handle)
            self.acquired = True
            return
        raise LockHeld(self.owner())

    def release(self):
        if self.acquired:
            self.acquired = False
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
