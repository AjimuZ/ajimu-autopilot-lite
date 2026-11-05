"""Validation of a configured project, plus optional live probes that use a little Claude usage."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import claude_cli, conventions, gitops, preflight, project as project_module, routing, runner as runner_module
from . import prd as prd_module
from .store import read_json, utc_now, write_json_atomic

PROBE_SCHEMA = {'type': 'object', 'properties': {'ack': {'type': 'string'}}, 'required': ['ack']}
MANAGED_SETTINGS = ('C:/Program Files/ClaudeCode/managed-settings.json',
                    '/Library/Application Support/ClaudeCode/managed-settings.json',
                    '/etc/claude-code/managed-settings.json')


class Report:
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail='', blocking=True):
        self.items.append({'name': name, 'ok': bool(ok), 'blocking': blocking, 'detail': detail})

    @property
    def passed(self):
        return all(item['ok'] or not item['blocking'] for item in self.items)


def _settings_files(project):
    home = Path.home()
    return [home / '.claude' / 'settings.json', project.root / '.claude' / 'settings.json',
            project.root / '.claude' / 'settings.local.json'] + [Path(path) for path in MANAGED_SETTINGS]


def check_settings(project, report):
    """Report settings that could undercut the policy; project settings load in every session."""
    for path in _settings_files(project):
        try:
            data = read_json(path)
        except (ValueError, OSError) as error:
            report.add(f'settings {path.name}', False, f'{path}: {error}', blocking=False)
            continue
        if not data or not isinstance(data, dict):
            continue
        for key in ('fallbackModel', 'model'):
            names = data.get(key)
            names = names if isinstance(names, list) else [names] if names else []
            if routing.forbidden_in_usage([n for n in names if n != 'default']):
                # --model and --fallback-model outrank both keys, so this is informational.
                report.add(f'settings {key}', True, f'{path} names {names}; the runner overrides it per session', False)
        env = data.get('env') or {}
        overrides = sorted(set(env) & set(claude_cli.ROUTING_VARIABLES))
        if overrides:
            report.add('settings env', False, f'{path} sets {overrides}, which can change the session model', False)
        if 'managed' in path.name and data.get('availableModels') is not None:
            allowed = data['availableModels']
            usable = [name for name in allowed if not routing.forbidden_in_usage([name])]
            report.add('managed availableModels', bool(usable), f'{path}: {allowed}')
        if data.get('allowManagedPermissionRulesOnly'):
            report.add('managed permission rules', False,
                       f'{path} ignores the runner\'s allow rules, so sessions cannot edit files or run checks')


def run(root, live=False, environ=None):
    """Return a Report for one project. `live` launches real sessions, which use Claude usage."""
    environ = os.environ if environ is None else environ
    project = project_module.Project(root)
    report = Report()
    if not project.is_configured():
        report.add('configuration', False, f'{project.config_path} or {project.prd_path} is missing; run setup')
        return report
    config = project.config()
    config_problems = project_module.validate_config(config)
    report.add('configuration', not config_problems, '; '.join(config_problems) or str(project.config_path))
    plan = prd_module.load(project.prd_path)
    plan_problems = prd_module.validate(plan)
    stories = [] if plan_problems else plan['userStories']
    pending = [item for item in stories if not item['passes']]
    report.add('roadmap plan', not plan_problems and bool(stories),
               '; '.join(plan_problems) or f'{len(stories)} tasks, {len(pending)} open')
    branch_name = plan.get('branchName') if isinstance(plan, dict) else None
    report.add('branch name matches plan', branch_name == config['git']['branch'],
               f'{branch_name} / {config["git"]["branch"]}')
    usable = [] if project_module.check_problems(config['checks']) else config['checks']
    report.add('project checks configured', bool(usable), ', '.join(check['command'] for check in usable)
               or 'add commands to "checks"')
    if usable:
        per_task = [check['name'] for check in usable if project_module.check_stage(check) == 'task']
        final = [check['name'] for check in usable if project_module.check_stage(check) == 'final']
        report.add('check stages', True, f'after each task: {", ".join(per_task) or "none"}; '
                   f'at the end: {", ".join(final) or "none (every check runs after each task)"}', blocking=False)
        if not per_task:
            report.add('per-task checks', False, 'no check runs after each task, so a task is committed on the '
                       'change scan alone; mark a fast check (build, typecheck, lint) "when": "task"', blocking=False)
    source = config['docs'].get('roadmap')
    report.add('roadmap document', bool(source) and (project.root / source).is_file(), source or 'none', blocking=False)

    for label, probe in (('Haiku is rejected', 'haiku'), ('full Haiku ID is rejected', 'claude-haiku-5-5')):
        try:
            routing.model_family(probe)
            report.add(label, False, 'the policy accepted it')
        except routing.ForbiddenModel:
            report.add(label, True, 'rejected')
    try:
        overrides = config['routing'].get('overrides')
        decisions = [routing.select(task_type, overrides=overrides) for task_type in routing.policy_table(overrides)]
        report.add('model routing', all(d.model in routing.ALLOWED_FAMILIES for d in decisions),
                   f'{len(decisions)} task types route to Sonnet or Opus')
    except (routing.RoutingError, routing.ForbiddenModel) as error:
        report.add('model routing', False, str(error))
    if config['preflight'].get('mode', 'plan') == 'plan' and not plan_problems:
        problems = runner_module.routing_problems(plan, config['routing'].get('overrides'))
        report.add('task routing from the plan', not problems, '; '.join(problems) or routing_summary(pending))
        untyped = [item['id'] for item in pending if not item.get('taskType')]
        if untyped:
            report.add('tasks without taskType', False, f'{", ".join(untyped[:15])} route as '
                       f'{routing.DEFAULT_TASK_TYPE}; set taskType on each', blocking=False)

    found = conventions.locate(project.root)
    report.add('ajimu-conventions skill', bool(found),
               f'{found.root} (version {found.digest})' if found else f'not installed: {conventions.INSTALL_HINT}')
    if found:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'message'
            path.write_text('Keep drafts during polling\n', encoding='utf-8')
            good, _ = conventions.check_message(found, path)
            path.write_text('Keep drafts\n\nCo-Authored-By: Someone <someone@example.invalid>\n', encoding='utf-8')
            bad, _ = conventions.check_message(found, path)
        report.add('commit message validator', good and not bad, 'accepts neutral and rejects co-author messages')
        refused = conventions.refused_task_ids(found, [item['id'] for item in pending])
        if refused:
            report.add('task IDs in commit messages', False, f'{", ".join(refused)}: the validator refuses each of these on the '
                       '`Task:` line of its commit, so it could never finish. Rename it, keeping its source')

    command = claude_cli.find_claude(config['claude'].get('command'), environ)
    version = ''
    if command:
        try:
            process = subprocess.run(command + ['--version'], capture_output=True, text=True, timeout=60)
            version = process.stdout.strip()
            report.add('Claude Code', process.returncode == 0, f'{command[-1]} {version}')
        except (OSError, subprocess.TimeoutExpired) as error:
            report.add('Claude Code', False, str(error))
        if str(command[0]).lower().endswith(('.cmd', '.bat')):
            report.add('Claude Code launcher', False, f'{command[0]} goes through cmd.exe, which mangles JSON arguments; '
                                                      'install the native build', blocking=False)
    else:
        report.add('Claude Code', False, 'not found; set claude.command or AJIMU_AUTOPILOT_CLAUDE')

    routed = sorted(set(environ) & set(claude_cli.ROUTING_VARIABLES))
    if routed:
        report.add('environment overrides', True, f'{routed} are set here; the runner removes them for its sessions', False)
    check_settings(project, report)

    git = gitops.Git(project.root)
    git_config = config['git']
    is_repo = git.is_repository()
    report.add('Git repository', is_repo and git.has_commits(), str(project.root))
    if is_repo:
        report.add('project at the repository root', git.is_root(),
                   'yes' if git.is_root() else 'this folder is inside a larger repository; configure the project at its root')
        name, email = git.identity()
        report.add('Git identity', bool(name and email), f'{name} <{email}>' if name else 'not configured')
        report.add('working branch', git_config['branch'] not in git_config['protected_branches'], git_config['branch'])
        try:
            report.add('remote access', bool(git.remote_url(git_config['remote'])), _remote_detail(git, git_config))
            # A repository whose main line has another name would silently gain a second one.
            if not git.branch_exists(git_config['branch']) and not git.remote_head(git_config['remote'], git_config['branch']):
                report.add('working branch exists', False, f'{git_config["branch"]} does not exist; the first run creates it '
                           'from the current commit. Use "setup --branch NAME" if the project uses another name', False)
        except gitops.GitError as error:
            report.add('remote access', False, str(error))
        foreign = git.foreign_changes()
        report.add('clean working tree', not foreign, ', '.join(foreign[:10]) or 'clean')
        tracked = git.tracked_metadata()
        report.add('metadata outside Git', git.metadata_ignored() and not tracked,
                   f'the next run stops tracking {len(tracked)} files; they stay on disk, and other clones need setup again'
                   if tracked else f'{gitops.IGNORE_RULE} is ignored' if git.metadata_ignored()
                   else f'the next run adds {gitops.IGNORE_RULE} to .gitignore', blocking=False)
    if live and (config_problems or plan_problems):
        report.add('live probes', False, 'skipped: nothing is spent until the configuration and the plan are valid',
                   blocking=False)
    elif live and command and found:
        live_probes(project, config, command, report, environ)
    return report


def routing_summary(stories):
    """Return how many open tasks route to each model and effort, such as 'sonnet/medium: 4'."""
    counts = {}
    for item in stories:
        decision = routing.plan_decision(item)
        key = f'{decision.model}/{decision.effort}'
        counts[key] = counts.get(key, 0) + 1
    return ', '.join(f'{key}: {count}' for key, count in sorted(counts.items())) or 'no open task'


def _remote_detail(git, git_config):
    head = git.remote_head(git_config['remote'], git_config['branch'])
    return f'{git_config["remote"]} reachable; {git_config["branch"]} ' + (f'at {head[:10]}' if head else 'does not exist yet')


def live_probes(project, config, command, report, environ):
    """Measure which models really run and cache the result for routing.

    `--model` can be silently replaced when a model is unavailable, so a family counts
    as available only when the session reports that family in its own usage. A usage limit
    leaves a family counted as available: this result is cached for later runs, and the
    limit says nothing about what the account can use. Also runs one real preflight
    without saving it.
    """
    families, resolved, limited = [], {}, False
    base = dict(environ)
    for family in routing.ALLOWED_FAMILIES:
        with tempfile.TemporaryDirectory(prefix='ajimu-probe-') as scratch:
            request = claude_cli.SessionRequest(
                phase='probe', model=family, effort='low', prompt='Return ack = "ok". Do nothing else.', cwd=scratch,
                log_path=str(project.logs_dir / f'probe-{family}.jsonl'), schema=PROBE_SCHEMA, tools=(), allow=(),
                timeout_seconds=180, setting_sources='project,local')
            result = claude_cli.run_session(command, request, None, base)
        check = claude_cli.model_report(result, family)
        if result.transient and not check['forbidden']:
            limited = True
            families.append(family)
            report.add(f'live {family}', False, f'usage or service limit (status {result.api_error_status}); '
                       'availability was not measured', blocking=False)
            continue
        healthy = result.has_result and not result.is_error and not check['forbidden'] and not check['fallback']
        report.add(f'live {family}', healthy, f'requested {family}; ran {check["actual"]}; '
                   f'{result.subtype}; effort low was sent but cannot be read back', blocking=family == 'sonnet')
        if healthy:
            families.append(family)
            if result.init_model:
                resolved[family] = result.init_model
    capabilities = {'families': families, 'resolved': resolved, 'sonnet_id': resolved.get('sonnet'), 'probed_at': utc_now()}
    write_json_atomic(project.capabilities_path, capabilities)
    for family, model_id in resolved.items():
        levels = routing.supported_efforts(model_id)
        report.add(f'effort levels of {model_id}', True, 'unrecognised ID; levels unverified' if levels is None
                   else ', '.join(level for level in routing.EFFORTS if level in levels) or 'takes no effort', False)
    plan = prd_module.load(project.prd_path)
    pending = [item for item in plan['userStories'] if not item['passes']]
    if config['preflight'].get('mode', 'plan') == 'plan':
        report.add('live preflight', True, 'skipped: tasks route from the plan, so runs start no preflight', blocking=False)
        return
    if not pending or 'sonnet' not in families or limited:
        report.add('live preflight', False, 'skipped: no open task, Sonnet is unavailable, or usage ran out', blocking=False)
        return
    report.add('live preflight', *live_preflight(project, config, command, pending[0], base))


def live_preflight(project, config, command, item, base_env):
    """Run one preflight without persisting it; return (ok, detail)."""
    worker = runner_module.Runner(project.root, base_env=base_env, output=lambda _: None)
    worker.load_state()
    worker.command = command
    plan = prd_module.load(project.prd_path)
    head = worker.git.head()
    request = claude_cli.SessionRequest(
        phase='preflight', model=config['preflight']['model'], effort=config['preflight']['effort'],
        prompt=worker.preflight_prompt(plan, item, ''), cwd=str(project.root),
        log_path=str(project.logs_dir / 'probe-preflight.jsonl'), system_append=preflight.load_prompt('system_rules'),
        schema=preflight.load_schema('preflight'), tools=runner_module.READ_TOOLS, allow=runner_module.READ_ALLOW,
        deny=runner_module.GIT_DENY + runner_module.PATH_DENY + runner_module.SHELL_DENY,
        timeout_seconds=config['limits']['session_timeout_minutes'] * 60,
        setting_sources=config['claude']['setting_sources'])
    result = claude_cli.run_session(command, request, None, base_env)
    try:
        record = preflight.validate_result(result.structured, item, head)
    except preflight.PreflightInvalid as error:
        return False, f'{item["id"]}: {error}'
    decision = record.get('routing') or {}
    return True, (f'{item["id"]}: {record["outcome"]}; would route to {decision.get("model")}/{decision.get("effort")}; '
                  f'ran {result.actual_models}; result not saved')


def render(report):
    lines = []
    for item in report.items:
        mark = 'ok  ' if item['ok'] else ('FAIL' if item['blocking'] else 'warn')
        lines.append(f'[{mark}] {item["name"]}: {item["detail"]}' if item['detail'] else f'[{mark}] {item["name"]}')
    lines.append('valid' if report.passed else 'not ready: fix the FAIL lines')
    return '\n'.join(lines)


def as_json(report):
    return json.dumps({'passed': report.passed, 'items': report.items}, indent=2, ensure_ascii=False)
