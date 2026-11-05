"""The read-only view of a run: what it shows, what it leaves alone, and one window per project."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SCRIPTS, Sandbox, done_output, ready_preflight, review_pass

from autopilot_core import follow, project as project_module
from autopilot_core.store import RunnerLock


def make_layout(root):
    layout = project_module.Project(root)
    layout.logs_dir.mkdir(parents=True)
    return layout


def append(path, text):
    with open(path, 'a', encoding='utf-8', newline='') as handle:
        handle.write(text)


class FollowerTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.layout = make_layout(self.scratch / 'one')
        self.shown = []
        self.follower = follow.Follower(self.layout, self.shown.append)

    def after(self, action):
        """Return what the next tick shows once `action` has run."""
        self.shown.clear()
        action()
        self.follower.tick()
        return list(self.shown)

    def test_new_lines_appear_once_and_a_line_being_written_waits(self):
        self.follower.start()
        self.assertIn('iteration 1', self.after(lambda: append(self.layout.run_log_path, 'iteration 1\nitera')))
        self.assertEqual([], self.after(lambda: None))
        self.assertIn('iteration 2', self.after(lambda: append(self.layout.run_log_path, 'tion 2\n')))
        self.assertNotIn('iteration 1', self.shown)

    def test_starting_shows_the_end_of_the_log_without_a_cut_line(self):
        append(self.layout.run_log_path, ''.join(f'line {n}\n' for n in range(2000)))
        self.follower.start()
        self.assertIn('line 1999', self.shown)
        self.assertNotIn('line 0', self.shown)
        history = [line for line in self.shown if 'line' in line and 'Read-only' not in line]
        self.assertTrue(all(line.startswith('line ') for line in history), history[:2])

    def test_a_log_that_shrank_is_read_from_its_start(self):
        append(self.layout.run_log_path, 'a long first line\n')
        self.follower.start()
        self.assertIn('new', self.after(lambda: self.layout.run_log_path.write_text('new\n', encoding='utf-8')))

    def test_a_new_session_is_shown_from_its_start_and_the_last_one_to_its_end(self):
        self.follower.start()
        first = self.layout.logs_dir / '20261031-100000-T-1-preflight.jsonl'
        append(first, json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5-5'}) + '\n')
        shown = self.after(lambda: None)
        self.assertTrue(any('T-1 preflight: session started on claude-sonnet-5-5' in line for line in shown), shown)

        def next_session():
            append(first, json.dumps({'type': 'result', 'subtype': 'success', 'total_cost_usd': 0.25}) + '\n')
            second = self.layout.logs_dir / '20261031-100500-T-1-implementation.jsonl'
            append(second, json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'text', 'text': 'I will edit it.'}, {'type': 'thinking', 'thinking': 'private'},
                {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': 'app.txt', 'old_string': 'secret'}}]}}) + '\n')
            os.utime(second, (first.stat().st_mtime + 5, first.stat().st_mtime + 5))
        joined = '\n'.join(self.after(next_session))
        self.assertIn('T-1 preflight: session ended: success', joined)
        self.assertNotIn('USD', joined)
        self.assertIn('T-1 implementation: I will edit it.', joined)
        self.assertIn('T-1 implementation: Edit app.txt', joined)
        self.assertNotIn('private', joined)
        self.assertNotIn('secret', joined)

    def test_a_session_already_under_way_is_joined_from_its_present(self):
        log = self.layout.logs_dir / '20261031-100000-T-1-implementation.jsonl'
        append(log, json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'old'}]}}) + '\n')
        self.follower.start()
        event = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'new'}]}}
        joined = '\n'.join(self.after(lambda: append(log, json.dumps(event) + '\n')))
        self.assertIn(': new', joined)
        self.assertNotIn(': old', joined)

    def test_terminal_escapes_written_by_a_session_never_reach_the_terminal(self):
        self.follower.start()
        log = self.layout.logs_dir / '20261031-100000-T-1-review.jsonl'
        event = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'a\x1b]0;evil\x07b\x9bc\nd'}]}}
        shown = self.after(lambda: (append(log, json.dumps(event) + '\n'),
                                    append(self.layout.run_log_path, 'blocked: \x1b[2Jreason\n')))
        for line in shown:
            self.assertEqual(follow.UNSAFE_TEXT.sub('', line), line)
        self.assertTrue(any('blocked:' in line and 'reason' in line for line in shown), shown)

    def test_foreign_or_broken_events_show_nothing_and_do_not_fail(self):
        self.follower.start()
        log = self.layout.logs_dir / '20261031-100000-T-1-review.jsonl'
        broken = 'not json\n[1]\n{"type": "assistant"}\n{"type": "assistant", "message": {"content": ["x", {"type": "tool_use"}]}}\n'
        self.assertEqual(['tool'], [line.split(': ', 1)[1] for line in self.after(lambda: append(log, broken))
                                    if 'review' in line])

    def test_it_tells_when_a_runner_appears_and_goes_even_without_a_last_word(self):
        self.follower.start()
        self.assertEqual(['no runner is active; this view shows the next one when it starts'], self.after(lambda: None))
        lock = RunnerLock(self.layout.lock_path)
        self.assertEqual(['runner active'], self.after(lock.acquire))
        self.assertEqual([], self.after(lambda: None))
        dead = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)

        def crash():
            lock.acquired = False
            owner = json.loads(self.layout.lock_path.read_text(encoding='utf-8'))
            os.utime(self.layout.lock_path, (1, 1))
            self.layout.lock_path.write_text(json.dumps({**owner, 'pid': int(dead.stdout)}), encoding='utf-8')
        self.assertEqual(['no runner is active; this view shows the next one when it starts'], self.after(crash))

    def test_it_changes_nothing_in_the_project(self):
        append(self.layout.run_log_path, 'line\n')
        before = sorted(str(path) for path in self.layout.root.rglob('*'))
        self.follower.start()
        self.follower.tick()
        self.assertEqual(before, sorted(str(path) for path in self.layout.root.rglob('*')))

    def test_a_folder_that_disappears_only_leaves_the_view_with_nothing_to_show(self):
        follower = follow.Follower(project_module.Project(self.scratch / 'never-created'), self.shown.append)
        follower.start()
        follower.tick()


class WindowTests(unittest.TestCase):
    """Several projects at once: each gets one window of its own, and nothing is shared."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.one = make_layout(Path(scratch.name) / 'one')
        self.two = make_layout(Path(scratch.name) / "two with 'odd' name")
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(follow.NO_WINDOW_VARIABLE)
        launch = mock.patch.object(follow, 'launch', return_value=None)
        self.launch = launch.start()
        self.addCleanup(launch.stop)

    def test_each_project_gets_its_own_window_at_the_same_time(self):
        self.assertIsNone(follow.open_window(self.one))
        self.assertIsNone(follow.open_window(self.two))
        self.assertEqual(2, self.launch.call_count)
        first, second = (call.args[0] for call in self.launch.call_args_list)
        self.assertIn(str(self.one.root), first)
        self.assertNotIn(str(self.two.root), first)
        self.assertIn('Autopilot: one', first)
        self.assertIn(shlex.quote(str(self.two.root)), second)

    def test_a_project_with_its_window_open_does_not_get_another_and_others_are_not_affected(self):
        with RunnerLock(self.one.viewer_lock_path):
            self.assertIsNone(follow.open_window(self.one))
            self.launch.assert_not_called()
            self.assertIsNone(follow.open_window(self.two))
            self.assertEqual(1, self.launch.call_count)

    def test_a_window_closed_without_cleaning_up_does_not_prevent_the_next(self):
        dead = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        lock = RunnerLock(self.one.viewer_lock_path)
        lock.acquire()
        owner = json.loads(self.one.viewer_lock_path.read_text(encoding='utf-8'))
        self.one.viewer_lock_path.write_text(json.dumps({**owner, 'pid': int(dead.stdout)}), encoding='utf-8')
        follow.open_window(self.one)
        self.assertEqual(1, self.launch.call_count)

    def test_a_window_that_cannot_open_says_how_to_follow_the_run(self):
        self.launch.return_value = 'no terminal emulator was found'
        message = follow.open_window(self.one)
        self.assertIn('no terminal emulator was found', message)
        self.assertIn(f'follow --project "{self.one.root}"', message)
        self.launch.side_effect = OSError('refused')
        self.assertIn('refused', follow.open_window(self.one))

    def test_the_window_does_not_keep_the_project_folder_busy(self):
        follow.open_window(self.one)
        self.assertEqual(str(Path.home()), self.launch.call_args.args[1])

    def test_it_can_be_turned_off(self):
        os.environ[follow.NO_WINDOW_VARIABLE] = '1'
        self.assertIsNone(follow.open_window(self.one))
        self.launch.assert_not_called()


class LaunchTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows only')
    def test_windows_uses_git_bash_in_a_new_console_with_nothing_inherited(self):
        with mock.patch.object(follow, 'git_bash', return_value=Path('C:/Git/bin/bash.exe')), \
                mock.patch.object(subprocess, 'Popen') as popen:
            self.assertIsNone(follow.launch('command', 'C:/home'))
        self.assertEqual([str(Path('C:/Git/bin/bash.exe')), '-c', 'command'], popen.call_args.args[0])
        self.assertEqual(subprocess.CREATE_NEW_CONSOLE, popen.call_args.kwargs['creationflags'])
        for name in ('stdin', 'stdout', 'stderr'):
            self.assertNotIn(name, popen.call_args.kwargs)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows only')
    def test_windows_without_git_bash_reports_it(self):
        with mock.patch.object(follow, 'git_bash', return_value=None):
            self.assertIn('Git Bash', follow.launch('command', 'C:/home'))

    @unittest.skipUnless(sys.platform == 'linux', 'Linux only')
    def test_linux_needs_a_desktop_and_a_terminal_and_detaches_from_the_run_output(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertIn('graphical', follow.launch('command', '/home'))
        with mock.patch.dict(os.environ, {'DISPLAY': ':0'}), mock.patch.object(follow.shutil, 'which', return_value=None):
            self.assertIn('terminal', follow.launch('command', '/home'))
        with mock.patch.dict(os.environ, {'DISPLAY': ':0'}), mock.patch.object(subprocess, 'Popen') as popen, \
                mock.patch.object(follow.shutil, 'which', lambda name: '/usr/bin/xterm' if name == 'xterm' else None):
            self.assertIsNone(follow.launch('command', '/home'))
        self.assertEqual(['/usr/bin/xterm', '-e', 'bash', '-c', 'command'], popen.call_args.args[0])
        for name in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(subprocess.DEVNULL, popen.call_args.kwargs[name])

    def test_the_title_cannot_carry_escapes_from_a_folder_name(self):
        layout = project_module.Project('x')
        layout.root = Path('folder\x1b]0;evil\x07')
        self.assertNotIn('\x1b', follow.window_command(layout).split(' follow ')[0].split(sys.executable)[0])


class RunLogTests(Sandbox):
    def work(self):
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass()}])

    def read_log(self):
        return self.layout.run_log_path.read_text(encoding='utf-8')

    def test_a_run_reports_to_its_output_and_to_the_log_alike(self):
        self.layout = self.make_repository([self.story()])
        self.work()
        self.runner().run()
        log = self.read_log()
        self.assertIn('--- run started ---', log)
        for line in self.lines:
            self.assertIn(line, log)
        self.assertIn('T-1: running test', self.lines)
        self.assertEqual('', self.git('status', '--porcelain'), 'the log stays out of the project history')

    def test_a_log_that_cannot_be_written_does_not_end_the_run(self):
        self.layout = self.make_repository([self.story()])
        self.layout.run_log_path.mkdir(parents=True)
        self.work()
        self.assertEqual(0, self.runner().run(), self.lines)

    def only_supervised_runs_open_a_window(self, supervised, calls):
        self.layout = self.make_repository([self.story()])
        self.work()
        with mock.patch.object(follow, 'open_window', return_value=None) as opened:
            self.runner(supervised=supervised).run()
        self.assertEqual(calls, opened.call_count)

    def test_a_supervised_run_opens_its_window(self):
        self.only_supervised_runs_open_a_window(True, 1)

    def test_a_run_in_a_terminal_does_not(self):
        self.only_supervised_runs_open_a_window(False, 0)

    def test_a_runner_refused_by_the_lock_opens_no_window(self):
        self.layout = self.make_repository([self.story()])
        with RunnerLock(self.layout.lock_path), mock.patch.object(follow, 'open_window') as opened:
            with self.assertRaises(Exception):
                self.runner(supervised=True).run()
        opened.assert_not_called()

    def test_a_window_that_cannot_open_is_reported_and_the_run_goes_on(self):
        self.layout = self.make_repository([self.story()])
        self.work()
        with mock.patch.object(follow, 'open_window', return_value='no window was opened (reason)'):
            self.assertEqual(0, self.runner(supervised=True).run(), self.lines)
        self.assertIn('no window was opened (reason)', self.lines)

    def test_the_command_line_view_is_one_window_per_project_and_ends_quietly(self):
        self.layout = self.make_repository([self.story()])
        with RunnerLock(self.layout.viewer_lock_path):
            result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'follow', '--window',
                                     '--project', str(self.project)], capture_output=True, text=True, timeout=60)
        self.assertEqual((0, ''), (result.returncode, result.stdout))

    def test_the_view_refuses_a_folder_that_is_not_configured(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'follow', '--project', str(self.tmp)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(2, result.returncode)
        self.assertIn('not configured', result.stderr)
        self.assertFalse((self.tmp / '.ajimu-autopilot').exists())


if __name__ == '__main__':
    unittest.main()
