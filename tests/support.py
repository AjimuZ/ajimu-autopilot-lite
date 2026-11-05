"""Shared helpers: scripted sessions plus a temporary repository with a local bare remote."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'ajimu-autopilot-lite'
SCRIPTS = SKILL / 'scripts'
sys.path.insert(0, str(SCRIPTS))

FAKE_CLAUDE = Path(__file__).resolve().parent / 'fixtures' / 'fake_claude.py'
# Inherited by every run the tests start, in this process or a child, so no real window opens.
os.environ['AJIMU_AUTOPILOT_NO_WINDOW'] = '1'


def ready_preflight(task_id, task_type='feature_standard', model='sonnet', effort='medium', **overrides):
    """Return a schema-valid preflight answer; versions are filled in by the fake CLI."""
    answer = {
        'task_id': task_id, 'analyzed_task_version': '__VERSION__', 'outcome': 'ready', 'issues': [], 'corrections': [],
        'corrected_task': None, 'split_tasks': None, 'functional_change': False, 'blocked_reason': None,
        'complexity': 'low', 'estimated_change_size': 'small', 'risks': [], 'dependencies_verified': [],
        'required_tests': ['unit test'], 'acceptance_evidence': [], 'task_type': task_type, 'recommended_model': model,
        'recommended_effort': effort, 'sonnet_sufficient': True, 'routing_reason': 'ordinary change',
    }
    answer.update(overrides)
    return answer


def done_output(subject='Add greeting output', **overrides):
    answer = {
        'conventions_ack': '__ACK__', 'status': 'done', 'blocked_reason': None, 'summary': 'Implemented the task.',
        'commit_subject': subject, 'commit_body': '', 'user_visible': False, 'docs_updated': [], 'tests_added': [],
        'files_changed': ['app.txt'], 'learnings': ['The app is one text file.'], 'reusable_patterns': [],
    }
    answer.update(overrides)
    return answer


def review_pass(item_criteria=('Works',), **overrides):
    answer = {
        'conventions_ack': '__ACK__', 'verdict': 'pass',
        'criteria': [{'criterion': c, 'met': True, 'evidence': 'app.txt:1'} for c in item_criteria],
        'documentation_ok': True, 'security_ok': True, 'regression_risks': [], 'findings': [],
    }
    answer.update(overrides)
    return answer


class Sandbox(unittest.TestCase):
    """Builds an isolated project: nothing here can reach a real repository or a real model."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='autopilot-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))
        self.project = self.tmp / 'project'
        self.remote = self.tmp / 'remote.git'
        self.fake_log = self.tmp / 'launches.jsonl'
        self.scenario_path = self.tmp / 'scenario.json'
        self.scenario_path.write_text('{}', encoding='utf-8')
        self.environ_patch = {'FAKE_CLAUDE_LOG': str(self.fake_log), 'FAKE_CLAUDE_SCENARIO': str(self.scenario_path)}
        saved = {name: os.environ.get(name) for name in self.environ_patch}
        os.environ.update(self.environ_patch)

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.addCleanup(restore)
        self.lines = []

    def git(self, *args, cwd=None, check=True):
        process = subprocess.run(['git', *args], cwd=str(cwd or self.project), capture_output=True, text=True)
        if check and process.returncode != 0:
            raise AssertionError(f'git {args}: {process.stderr}')
        return process.stdout.strip()

    def make_repository(self, stories=None, roadmap=None, branch='autopilot/roadmap', lite=False):
        """Create the repo, its bare remote and a configured plan with a real passing check.

        By default the project runs like the full skill (a preflight and a review per task,
        every check after every task), which the inherited tests describe. `lite=True` keeps
        the lite defaults and adds a slow check, `final.py`, that runs only at the end.
        """
        subprocess.run(['git', 'init', '--quiet', '--bare', str(self.remote)], check=True)
        self.project.mkdir()
        self.git('init', '--quiet', '--initial-branch=main')
        self.git('config', 'user.name', 'Test Owner')
        self.git('config', 'user.email', 'owner@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('remote', 'add', 'origin', str(self.remote))
        (self.project / 'app.txt').write_text('base\n', encoding='utf-8')
        (self.project / 'check.py').write_text(
            "import pathlib,sys\nsys.exit(1 if 'BROKEN' in pathlib.Path('app.txt').read_text() else 0)\n", encoding='utf-8')
        if roadmap:
            (self.project / 'ROADMAP.md').write_text(roadmap, encoding='utf-8')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Create the sample project')
        self.git('push', '--quiet', 'origin', 'main')

        from autopilot_core import prd, project as project_module
        from autopilot_core.store import write_json_atomic
        project_module.setup(self.project, 'ROADMAP.md' if roadmap else None, branch)
        layout = project_module.Project(self.project)
        config = layout.config()
        config['checks'] = [{'name': 'test', 'command': f'"{sys.executable}" check.py', 'required': True}]
        if lite:
            config['checks'] = [{'name': 'build', 'command': f'"{sys.executable}" check.py', 'required': True, 'when': 'task'},
                                {'name': 'test', 'command': f'"{sys.executable}" final.py', 'required': True, 'when': 'final'}]
            (self.project / 'final.py').write_text(
                "import pathlib,sys\nsys.exit(1 if 'SLOWFAIL' in pathlib.Path('app.txt').read_text() else 0)\n",
                encoding='utf-8')
            self.git('add', 'final.py')
            self.git('commit', '--quiet', '-m', 'Add the slow check')
            self.git('push', '--quiet', 'origin', 'main')
        else:
            config['preflight']['mode'] = 'full'
            config['review']['enabled'] = True
        config['claude']['command'] = [sys.executable, str(FAKE_CLAUDE)]
        write_json_atomic(layout.config_path, config)
        if stories is not None:
            plan = prd.load(layout.prd_path)
            plan['userStories'] = stories
            prd.save(layout.prd_path, plan)
        return layout

    def story(self, task_id='T-1', **overrides):
        item = {'id': task_id, 'title': f'Task {task_id}', 'description': 'Change the app.', 'acceptanceCriteria': ['Works'],
                'priority': 1, 'passes': False, 'notes': '', 'dependsOn': []}
        item.update(overrides)
        return item

    def scenario(self, **phases):
        self.scenario_path.write_text(json.dumps(phases), encoding='utf-8')

    def launches(self, phase=None):
        if not self.fake_log.exists():
            return []
        entries = [json.loads(line) for line in self.fake_log.read_text(encoding='utf-8').splitlines()]
        return [entry for entry in entries if phase is None or entry['phase'] == phase]

    def runner(self, **options):
        from autopilot_core.runner import Runner
        return Runner(self.project, output=self.lines.append, **options)

    def remote_log(self, branch='autopilot/roadmap'):
        return self.git('log', '--format=%s', branch, cwd=self.remote, check=False).splitlines()

    def edit(self, name='app.txt', content='changed\n'):
        return {str(self.project / name): content}
