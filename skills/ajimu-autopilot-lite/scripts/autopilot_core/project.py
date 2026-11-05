"""Per-project layout, configuration and the idempotent setup step.

The lite defaults route each task from its plan entry instead of a preflight session,
skip the per-task review, and run the slow checks (`"when": "final"`) once at the end
of the plan instead of after every task. Setup only writes orchestration files and the ignore rule for its own folder. It never
commits, never changes branches and never starts development. Existing plans and settings
are kept and only completed.
"""
import copy
import json
import re
import shlex
from pathlib import Path

from . import gitops, roadmap, routing
from . import prd as prd_module
from .store import read_json, write_bytes_atomic, write_json_atomic, write_text_atomic

PROGRESS_HEADER = '# Ralph Progress Log\n\n## Codebase Patterns\n\n---\n'
DEFAULT_CONFIG = {
    'schema': 1,
    'claude': {'command': None, 'setting_sources': 'project,local'},
    'limits': {
        'max_iterations': 30,
        'max_fix_attempts': 2,
        'final_fix_attempts': 2,
        'preflight_max_rounds': 3,
        'session_timeout_minutes': 60,
        'usage_retry_minutes': 5,
        'check_timeout_minutes': 20,
        'global_timeout_minutes': 720,
        'keep_session_logs': 40,
    },
    # `plan` routes from the story's taskType; `full` runs a preflight session per task.
    'preflight': {'mode': 'plan', 'model': 'sonnet', 'effort': 'medium'},
    'review': {'enabled': False},
    # `final_every_tasks`: 0 runs the final checks only when no task is left to run.
    'verify': {'final_every_tasks': 0, 'fix_model': 'sonnet', 'fix_effort': 'high'},
    'routing': {'overrides': {}},
    'git': {'remote': 'origin', 'branch': 'main', 'protected_branches': []},
    'checks': [],
    'docs': {'roadmap': None, 'changelog': None},
    'permissions': {'bash_allow': [], 'extra_allow': [], 'extra_deny': []},
}
# Written by earlier releases. Nothing reads them, and setup removes them so none looks like a working limit.
RETIRED_LIMITS = ('max_budget_usd_per_session', 'max_budget_usd_total')
CHECK_STAGES = ('task', 'final')
PREFLIGHT_MODES = ('plan', 'full')
# Checks named like these are slow or broad, so setup defers them to the final verification.
FINAL_CHECK_NAMES = re.compile(r'test|e2e|integration|spec', re.IGNORECASE)
ROADMAP_NAMES = ('ROADMAP.md', 'roadmap.md', 'docs/ROADMAP.md', 'docs/roadmap.md', 'TODO.md', 'docs/TODO.md')
CHANGELOG_NAMES = ('CHANGELOG.md', 'docs/CHANGELOG.md', 'CHANGES.md', 'HISTORY.md')
UNSAFE_ALLOW = re.compile(r'^\s*(?:Bash\s*(?:\(\s*\*?\s*\))?|Bash\(\s*(?:sudo|rm|git|curl|wget|ssh|scp|chmod|chown)\b.*)\s*$')
UNSAFE_MODES = ('bypassPermissions', '--dangerously-skip-permissions')


class Project:
    """Paths of one configured repository."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.ralph_dir = self.root / 'scripts' / 'ralph'
        self.prd_path = self.ralph_dir / 'prd.json'
        self.progress_path = self.ralph_dir / 'progress.txt'
        self.meta_dir = self.root / gitops.METADATA_DIR
        self.config_path = self.meta_dir / 'config.json'
        self.tasks_dir = self.meta_dir / 'tasks'
        self.runtime_dir = self.meta_dir / 'runtime'
        self.state_path = self.runtime_dir / 'state.json'
        self.lock_path = self.runtime_dir / 'runner.lock'
        self.stop_path = self.runtime_dir / 'STOP'
        self.pause_path = self.runtime_dir / 'PAUSE'
        self.errors_path = self.runtime_dir / 'errors.log'
        self.run_log_path = self.runtime_dir / 'run.log'
        self.viewer_lock_path = self.runtime_dir / 'viewer.lock'
        self.capabilities_path = self.runtime_dir / 'capabilities.json'
        self.logs_dir = self.runtime_dir / 'logs'

    def is_configured(self):
        return self.config_path.is_file() and self.prd_path.is_file()

    def ensure_runtime(self):
        """Create the runtime folder and keep the metadata folder out of Git.

        Returns True when the project's `.gitignore` gained the rule. Git decides whether a
        rule already applies, so one the owner wrote in another form is left alone. The
        file's other bytes and line endings are preserved. Outside a repository Git cannot
        answer, so the file itself is what keeps this idempotent.
        """
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        path = self.root / '.gitignore'
        content = path.read_bytes() if path.is_file() else b''
        rule = gitops.IGNORE_RULE.encode()
        if gitops.Git(self.root).metadata_ignored() or rule in content.splitlines():
            return False
        newline = b'\r\n' if b'\r\n' in content else b'\n'
        separator = newline if content and not content.endswith(b'\n') else b''
        write_bytes_atomic(path, content + separator + rule + newline)
        return True

    def config(self):
        """Return the configuration completed with current defaults. Raises when it is absent."""
        stored = read_json(self.config_path)
        if stored is None:
            raise FileNotFoundError(f'{self.config_path} does not exist; run setup first')
        return merge_defaults(stored, DEFAULT_CONFIG)

    def record_path(self, task_id):
        return self.tasks_dir / f'{task_id}.json'


def merge_defaults(stored, defaults):
    """Return `stored` with missing keys filled in. Values already present always win."""
    merged = copy.deepcopy(stored)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = merge_defaults(merged[key], value)
    return merged


def validate_config(config):
    """Return every configuration problem that would weaken a safety rule."""
    problems = []
    try:
        routing.policy_table(config['routing'].get('overrides'))
    except (routing.ForbiddenModel, routing.RoutingError, KeyError) as error:
        problems.append(f'routing: {error}')
    try:
        if routing.model_family(config['preflight']['model']) != config['preflight']['model']:
            problems.append('preflight.model must be the alias sonnet or opus')
    except routing.ForbiddenModel as error:
        problems.append(f'preflight: {error}')
    if config['preflight']['effort'] not in routing.EFFORTS:
        problems.append(f'preflight.effort is unknown: {config["preflight"]["effort"]}')
    if config['preflight'].get('mode') not in PREFLIGHT_MODES:
        problems.append(f'preflight.mode must be one of {", ".join(PREFLIGHT_MODES)}')
    verify = config['verify']
    try:
        if routing.model_family(verify.get('fix_model')) != verify.get('fix_model'):
            problems.append('verify.fix_model must be the alias sonnet or opus')
    except routing.ForbiddenModel as error:
        problems.append(f'verify: {error}')
    if verify.get('fix_effort') not in routing.EFFORTS:
        problems.append(f'verify.fix_effort is unknown: {verify.get("fix_effort")}')
    every = verify.get('final_every_tasks')
    if not isinstance(every, int) or isinstance(every, bool) or every < 0:
        problems.append('verify.final_every_tasks must be zero or more')
    limits = config['limits']
    for name in ('max_iterations', 'session_timeout_minutes', 'check_timeout_minutes', 'global_timeout_minutes',
                 'usage_retry_minutes', 'preflight_max_rounds'):
        if not isinstance(limits.get(name), int) or limits[name] <= 0:
            problems.append(f'limits.{name} must be a positive integer; an unlimited run is not allowed')
    for name in ('max_fix_attempts', 'final_fix_attempts', 'keep_session_logs'):
        if not isinstance(limits.get(name), int) or limits[name] < 0:
            problems.append(f'limits.{name} must be zero or more')
    problems += check_problems(config['checks'])
    permissions = config['permissions']
    for rule in permissions.get('bash_allow', []):
        if UNSAFE_ALLOW.match(f'Bash({rule})'):
            problems.append(f'permissions.bash_allow: "{rule}" is too broad or reserved for the runner')
    for rule in permissions.get('extra_allow', []):
        if UNSAFE_ALLOW.match(rule) or any(mode in rule for mode in UNSAFE_MODES):
            problems.append(f'permissions.extra_allow: "{rule}" is too broad or reserved for the runner')
    command = config['claude'].get('command')
    if command and any(mode in str(command) for mode in UNSAFE_MODES + ('--fallback-model', '--model')):
        problems.append('claude.command must not carry permission or model flags')
    return problems


def check_problems(checks):
    """Return why `checks` cannot be run, or an empty list. Sessions edit this list by hand."""
    def usable(check):
        return (isinstance(check, dict) and all(isinstance(check.get(name), str) and check[name].strip()
                                                for name in ('name', 'command'))
                and check.get('when', 'task') in CHECK_STAGES)
    if isinstance(checks, list) and all(usable(check) for check in checks):
        return []
    return ['checks must be a list of objects such as {"name": "test", "command": "npm test", "required": true, '
            '"when": "final"}; "when" is "task" (after each task) or "final" (once at the end)']


def check_stage(check):
    """Return when a check runs: 'task' after every task, or 'final' in the final verification.

    A check without `when`, as configurations of the full skill have, runs after every task.
    """
    return check.get('when', 'task')


def checks_for(checks, stage):
    """Return the checks to run at `stage`: per-task ones for 'task', every check for 'final'.

    The final verification runs all of them, so the state it records as verified is
    verified by the whole configured suite.
    """
    return [check for check in checks if stage == 'final' or check_stage(check) == 'task']


def has_final_checks(checks):
    return any(check_stage(check) == 'final' for check in checks)


def _scripts(root):
    try:
        return (read_json(root / 'package.json') or {}).get('scripts') or {}
    except ValueError:
        return {}


def detect_checks(root):
    """Suggest verification commands from project files; return [] when nothing is recognisable.

    These are suggestions, confirmed by the setup session and by `validate`.
    """
    root = Path(root)
    checks = []
    if (root / 'package.json').is_file():
        runner = 'pnpm' if (root / 'pnpm-lock.yaml').exists() else 'yarn' if (root / 'yarn.lock').exists() else 'npm'
        scripts = _scripts(root)
        for name in ('typecheck', 'lint', 'build', 'test'):
            if name in scripts and 'no test specified' not in scripts[name]:
                command = f'{runner} test' if name == 'test' else f'{runner} run {name}'
                checks.append({'name': name, 'command': command, 'required': True})
    if (root / 'pyproject.toml').is_file() or (root / 'setup.py').is_file() or (root / 'pytest.ini').is_file():
        text = (root / 'pyproject.toml').read_text(encoding='utf-8') if (root / 'pyproject.toml').is_file() else ''
        if 'ruff' in text:
            checks.append({'name': 'lint', 'command': 'ruff check .', 'required': True})
        if 'mypy' in text:
            checks.append({'name': 'typecheck', 'command': 'mypy .', 'required': True})
        checks.append({'name': 'test', 'command': 'pytest' if 'pytest' in text or (root / 'pytest.ini').exists()
                       else 'python -m unittest discover', 'required': True})
    elif (root / 'tests').is_dir() and any((root / 'tests').glob('test_*.py')):
        checks.append({'name': 'test', 'command': 'python -m unittest discover -s tests', 'required': True})
    if (root / 'Cargo.toml').is_file():
        checks += [{'name': 'build', 'command': 'cargo build', 'required': True},
                   {'name': 'test', 'command': 'cargo test', 'required': True}]
    if (root / 'go.mod').is_file():
        checks += [{'name': 'vet', 'command': 'go vet ./...', 'required': True},
                   {'name': 'test', 'command': 'go test ./...', 'required': True}]
    if (root / 'gradlew').is_file():
        checks.append({'name': 'test', 'command': './gradlew test', 'required': True})
    elif (root / 'pom.xml').is_file():
        checks.append({'name': 'test', 'command': 'mvn -q test', 'required': True})
    if any(root.glob('*.sln')) or any(root.glob('*.csproj')):
        checks.append({'name': 'test', 'command': 'dotnet test', 'required': True})
    if not checks and (root / 'Makefile').is_file():
        if re.search(r'^test:', (root / 'Makefile').read_text(encoding='utf-8', errors='replace'), re.MULTILINE):
            checks.append({'name': 'test', 'command': 'make test', 'required': True})
    for check in checks:
        check['when'] = 'final' if FINAL_CHECK_NAMES.search(check['name']) else 'task'
    return checks


def bash_rules_for_checks(checks):
    """Derive permission rules letting a session run the checks; compound commands are skipped."""
    rules = []
    for check in checks:
        if any(token in check['command'] for token in ('&&', '||', ';', '|', '>', '<', '`', '$(')):
            continue
        try:
            words = shlex.split(check['command'])
        except ValueError:
            continue
        if words:
            # `python -m unittest` needs three words: `python -m *` would allow any module.
            width = 3 if len(words) > 2 and (words[1] == 'run' or words[1].startswith('-')) else 2
            rules.append(' '.join(words[:width]) + ' *')
    return list(dict.fromkeys(rules))


def project_relative(root, path):
    """Return a roadmap path as project-relative POSIX, so no machine-specific path enters tracked files.

    A relative `path` starts at the project root. Raises ValueError for a path outside the
    project, because the runner edits this file and Git could not see that edit.
    """
    base = Path(root).resolve()
    try:
        return (base / path).resolve().relative_to(base).as_posix()
    except ValueError:
        raise ValueError(f'the roadmap must be inside the project: {path}') from None


def find_first(root, names):
    for name in names:
        if (Path(root) / name).is_file():
            return name
    return None


def setup(root, roadmap_path=None, branch=None, remote=None):
    """Create or complete the orchestration files; return a report of what happened.

    Idempotent: a second run with the same input changes nothing. Existing stories,
    settings and progress entries are never overwritten; only retired limits are removed.
    """
    project = Project(root)
    report = {'created': [], 'updated': [], 'imported': [], 'warnings': []}
    existed = project.config_path.is_file()
    stored = read_json(project.config_path, {}) if existed else {}
    config = merge_defaults(stored, DEFAULT_CONFIG)
    for name in RETIRED_LIMITS:
        config['limits'].pop(name, None)
    if branch:
        config['git']['branch'] = branch
    if remote:
        config['git']['remote'] = remote
    if not config['checks']:
        config['checks'] = detect_checks(project.root)
        if not config['checks']:
            report['warnings'].append('no check command was detected; add "checks" before running')
    if check_problems(config['checks']):
        raise ValueError('; '.join(check_problems(config['checks'])))
    config['permissions']['bash_allow'] = list(dict.fromkeys(
        config['permissions']['bash_allow'] + bash_rules_for_checks(config['checks'])))
    config['docs']['changelog'] = config['docs']['changelog'] or find_first(project.root, CHANGELOG_NAMES)
    if roadmap_path:
        config['docs']['roadmap'] = project_relative(project.root, roadmap_path)
    config['docs']['roadmap'] = config['docs']['roadmap'] or find_first(project.root, ROADMAP_NAMES)
    problems = validate_config(config)
    if problems:
        raise ValueError('; '.join(problems))
    if config != stored:
        write_json_atomic(project.config_path, config)
        report['updated' if existed else 'created'].append(project.config_path.relative_to(project.root).as_posix())

    plan_existed = project.prd_path.is_file()
    plan = (prd_module.load_valid(project.prd_path) if plan_existed
            else prd_module.new_plan(project.root.name, config['git']['branch']))
    before = json.dumps(plan, sort_keys=True)
    if plan['branchName'] != config['git']['branch']:
        plan['branchName'] = config['git']['branch']
    source = config['docs']['roadmap']
    if source and (project.root / source).is_file():
        stories, _ = roadmap.parse_file(project.root, source)
        report['imported'] = roadmap.merge(plan, stories)
    elif source:
        report['warnings'].append(f'roadmap {source} does not exist')
    else:
        report['warnings'].append('no roadmap was found; add stories to scripts/ralph/prd.json')
    if not plan_existed or json.dumps(plan, sort_keys=True) != before:
        prd_module.save(project.prd_path, plan)
        report['updated' if plan_existed else 'created'].append('scripts/ralph/prd.json')
    if not project.progress_path.is_file():
        write_text_atomic(project.progress_path, PROGRESS_HEADER)
        report['created'].append('scripts/ralph/progress.txt')
    if project.ensure_runtime():
        report['updated'].append('.gitignore')
    project.tasks_dir.mkdir(parents=True, exist_ok=True)
    git = gitops.Git(project.root)
    if git.is_repository() and not git.is_root():
        report['warnings'].append('this folder is not the root of its Git repository, and the runner refuses to work there')
    return report


def add_progress(path, heading, lines, patterns=()):
    """Append one Ralph-format entry and merge reusable patterns into the top section.

    Duplicated patterns are dropped so the section stays short enough to give every
    new session in full.
    """
    text = Path(path).read_text(encoding='utf-8') if Path(path).is_file() else PROGRESS_HEADER
    marker = '## Codebase Patterns\n'
    if marker not in text:
        text = marker + '\n---\n' + text
    start = text.index(marker) + len(marker)
    end = text.find('\n---', start)
    end = len(text) if end < 0 else end
    section = text[start:end]
    fresh = [p.strip() for p in patterns if p.strip() and f'- {p.strip()}\n' not in section + '\n']
    if fresh:
        section = section.rstrip('\n') + '\n' + ''.join(f'- {pattern}\n' for pattern in fresh)
        text = text[:start] + section + text[end:]
    entry = f'\n## {heading}\n' + ''.join(f'{line}\n' for line in lines) + '---\n'
    write_text_atomic(path, text.rstrip('\n') + '\n' + entry)


def progress_context(path, recent_entries=2, limit_chars=3000):
    """Return the patterns section and the latest entries, bounded for a fresh session's prompt."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return ''
    marker = '## Codebase Patterns'
    start = text.find(marker)
    end = text.find('\n---', start) if start >= 0 else -1
    patterns = text[start:end] if start >= 0 and end >= 0 else ''
    entries = re.split(r'\n(?=## \d{4}-)', text[end + 4:] if end >= 0 else text)
    recent = [entry.strip() for entry in entries if entry.strip().startswith('## ')][-recent_entries:]
    return (patterns + '\n\n' + '\n\n'.join(recent)).strip()[-limit_chars:]
