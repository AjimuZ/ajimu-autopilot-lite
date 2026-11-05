"""Start child processes so that a timeout or a stop can end them with all their descendants."""
import os
import signal
import subprocess
import sys
import time

# A session of its own gives a child a process group to signal as a whole. Windows needs
# no option, because taskkill follows the process tree.
GROUP_OPTIONS = {'start_new_session': True} if sys.platform != 'win32' else {}


def terminate_tree(process):
    """End a process started with GROUP_OPTIONS and its descendants, which could otherwise keep editing the project."""
    if process.poll() is not None:
        return
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def wait_bounded(process, timeout_seconds, should_stop=None):
    """Wait for `process`; return 'exited', 'timed_out' or 'stopped'. The last two end its tree.

    An exception while waiting, such as Ctrl+C, also ends the tree before it propagates.
    A POSIX child has its own session, so the terminal's signal never reaches it, and it
    would go on working after the runner released its lock.
    """
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                process.wait(timeout=1)
                return 'exited'
            except subprocess.TimeoutExpired:
                pass
            outcome = 'timed_out' if time.monotonic() > deadline else 'stopped' if should_stop and should_stop() else None
            if outcome:
                terminate_tree(process)
                return outcome
    except BaseException:
        terminate_tree(process)
        raise
