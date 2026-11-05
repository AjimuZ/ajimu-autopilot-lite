#!/usr/bin/env python3
"""Command line of Ajimu Autopilot Lite: configure, validate, run, resume, stop and monitor a project.

Run it from any terminal, or from a Claude Code session that stays to supervise the run
(`run --supervised`, in the background). Exit status: 0 finished, 1 error, 2 usage or
configuration, 3 paused and resumable, 4 only blocked tasks remain, 5 a limit was reached.
"""
import argparse
import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autopilot_core import __version__, claude_cli, doctor, follow, gitops, preflight, project as project_module, roadmap, routing  # noqa: E402
from autopilot_core import prd as prd_module  # noqa: E402
from autopilot_core import runner as runner_module  # noqa: E402
from autopilot_core.store import LockHeld, RunnerLock, read_json  # noqa: E402


def configured_project(root):
    """Return the project at `root`, or raise FileNotFoundError before anything is created there."""
    project = project_module.Project(root)
    if not project.is_configured():
        raise FileNotFoundError(f'{project.root} is not configured; run setup there, or pass --project')
    return project


def command_setup(args):
    with RunnerLock(project_module.Project(args.project).lock_path):
        report = project_module.setup(args.project, args.roadmap, args.branch, args.remote)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    for label in ('created', 'updated'):
        for name in report[label]:
            print(f'{label}: {name}')
    if report['imported']:
        print(f'imported {len(report["imported"])} new tasks from the roadmap')
    if not report['created'] and not report['updated'] and not report['imported']:
        print('already configured; nothing changed')
    for warning in report['warnings']:
        print(f'warning: {warning}')
    print('Development has not started. Review the plan and run "validate". Then start it with /ajimu-autopilot-lite run '
          'inside Claude Code, or with "run" from a terminal.')
    return 0


def command_import(args):
    project = configured_project(args.project)
    with RunnerLock(project.lock_path):
        plan = prd_module.load_valid(project.prd_path)
        stories, summary = roadmap.parse_file(project.root, project_module.project_relative(project.root, args.roadmap))
        added = roadmap.merge(plan, stories)
        prd_module.save(project.prd_path, plan)
    print(json.dumps({**summary, 'added': added}, indent=2, ensure_ascii=False))
    return 0


def command_validate(args):
    report = doctor.run(args.project, live=args.live)
    print(doctor.as_json(report) if args.json else doctor.render(report))
    return 0 if report.passed else 1


def _run(args, resume):
    project = project_module.Project(args.project)
    if not resume and project.stop_path.exists():
        print('a stop request is pending; use "resume" to clear it and continue', file=sys.stderr)
        return runner_module.EXIT_USAGE
    if resume:
        # Checked first, because clearing the request of a runner still at work would cancel its stop.
        lock = RunnerLock(project.lock_path)
        if lock.is_active():
            raise LockHeld(lock.owner())
        runner_module.clear_stop(args.project)
    worker = runner_module.Runner(args.project, max_iterations=args.max_iterations, only_task=args.task,
                                  supervised=args.supervised)
    return worker.run()


def command_run(args):
    return _run(args, resume=False)


def command_resume(args):
    return _run(args, resume=True)


def command_stop(args):
    # A stop written anywhere else would be reported as done while the real runner carries on.
    runner_module.request_stop(configured_project(args.project).root)
    print('stop requested; the runner ends its current session and saves its state')
    return 0


def command_pause(args):
    project = configured_project(args.project)
    # A request no runner would read could only surprise a later one.
    if not RunnerLock(project.lock_path).is_active():
        print('no runner is active; there is nothing to pause', file=sys.stderr)
        return runner_module.EXIT_USAGE
    runner_module.request_pause(project.root)
    print('pause requested; the runner ends after its current attempt, interrupting nothing')
    return 0


def command_unblock(args):
    try:
        restored = runner_module.unblock(configured_project(args.project).root, args.task, args.restore_work)
    except KeyError as error:
        print(error.args[0], file=sys.stderr)
        return runner_module.EXIT_USAGE
    if restored:
        print(f'{args.task} is ready again, with its partial work back in the tree; run "resume" to continue')
    else:
        print(f'{args.task} is pending again{"; no partial work had been set aside" if args.restore_work else ""}; '
              'run "resume" to continue')
    return 0


def command_defer(args):
    worker = runner_module.Runner(configured_project(args.project).root)
    try:
        with RunnerLock(worker.project.lock_path):
            worker.load_state()
            worker.defer(args.task, args.reason)
    except KeyError:
        print(f'unknown task: {args.task}', file=sys.stderr)
        return runner_module.EXIT_USAGE
    except runner_module.Halt as halt:
        print(halt.message, file=sys.stderr)
        return halt.code
    print(f'{args.task} is set aside until you act; "unblock --task {args.task}" brings it back')
    return 0


def command_route(args):
    try:
        decision = routing.select(args.task_type, args.model, args.effort, sonnet_sufficient=args.sonnet_sufficient,
                                  available_families=tuple(args.available.split(',')))
    except (routing.ForbiddenModel, routing.RoutingError) as error:
        print(f'refused: {error}', file=sys.stderr)
        return 1
    print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
    return 0


def command_preflight(args):
    """Run the preflight of one task, on its own and without implementing anything."""
    worker = runner_module.Runner(args.project)
    plan = worker.plan()
    problems = project_module.validate_config(worker.config) + prd_module.validate(plan)
    if problems:
        print('configuration problems:\n- ' + '\n- '.join(problems), file=sys.stderr)
        return runner_module.EXIT_USAGE
    if args.task not in {item['id'] for item in plan['userStories']}:
        print(f'unknown task: {args.task}', file=sys.stderr)
        return runner_module.EXIT_USAGE
    try:
        with RunnerLock(worker.project.lock_path):
            worker.load_state()
            status = worker.entry(args.task)['status']
            if status in ('completed', 'committing', 'pushing', 'blocked'):
                print(f'{args.task} is {status}; a preflight applies only to an open task. '
                      f'{"Use unblock first." if status == "blocked" else ""}'.strip(), file=sys.stderr)
                return runner_module.EXIT_USAGE
            worker.prepare_for_single_phase()
            worker.run_preflight(args.task)
    except runner_module.Halt as halt:
        print(halt.message, file=sys.stderr)
        return halt.code
    print(json.dumps(worker.load_record(args.task).get('preflight'), indent=2, ensure_ascii=False))
    # A split or a verified "already implemented" also ends without a `ready` task, and neither is a block.
    return runner_module.EXIT_BLOCKED if (worker.state['tasks'].get(args.task) or {}).get('status') == 'blocked' else 0


def build_status(root):
    """Collect everything the monitor shows from files only; works while a run is active."""
    project = project_module.Project(root)
    plan = prd_module.load_valid(project.prd_path)
    state = read_json(project.state_path) or {'tasks': {}, 'run': {'status': 'never started'}, 'iteration': 0}
    config = project.config()
    tasks, completed, remaining, blocked = {}, [], [], []
    for item in plan['userStories']:
        entry = state['tasks'].get(item['id']) or {}
        status = runner_module.effective_status(item, entry.get('status', 'pending'))
        tasks[item['id']] = status
        if status == 'completed':
            completed.append(item['id'])
        elif status == 'blocked':
            blocked.append({'id': item['id'], 'reason': entry.get('blocked_reason'),
                            'preserved_commit': entry.get('preserved_commit'),
                            'needs_owner': (entry.get('needs_owner') or {}).get('request')})
        else:
            remaining.append({'id': item['id'], 'status': status})
    current = state.get('current_task')
    selected = latest_checks = None
    for task_id in [current] + [item['id'] for item in reversed(plan['userStories'])]:
        record = read_json(project.record_path(task_id)) if task_id else None
        if not record:
            continue
        routed = record.get('routing') or (record.get('preflight') or {}).get('routing')
        if selected is None and routed:
            selected = {'task': task_id, 'model': routed['model'], 'effort': routed['effort'],
                        'reason': routed.get('reason')}
        executions = [e for e in record.get('executions', []) if e.get('checks')]
        if latest_checks is None and executions:
            latest_checks = {'task': task_id, 'attempt': executions[-1].get('attempt'), 'results': executions[-1]['checks'],
                             'actual_models': executions[-1].get('actual_models')}
        if selected and latest_checks:
            break
    errors = []
    if project.errors_path.is_file():
        for line in project.errors_path.read_text(encoding='utf-8').splitlines()[-5:]:
            try:
                errors.append(json.loads(line))
            except ValueError:
                continue
    git = gitops.Git(project.root)
    return {
        # Asked of the lock's owner, because a crash leaves both the lock and a `running` state behind.
        'run': state.get('run'), 'runner_active': RunnerLock(project.lock_path).is_active(),
        'stop_requested': project.stop_path.exists(),
        'iteration': state.get('iteration', 0), 'max_iterations': config['limits']['max_iterations'],
        'current_task': current, 'completed': completed, 'remaining': remaining, 'blocked': blocked,
        'waiting_on_blocked': prd_module.waiting_on_blocked(plan, tasks), 'selection': selected, 'latest_checks': latest_checks, 'last_commit': state.get('last_commit') or
        (git.head() if git.has_commits() else None), 'last_push': state.get('last_push'),
        'recent_errors': errors, 'final': state.get('final'),
    }


def command_status(args):
    try:
        status = build_status(args.project)
    except (FileNotFoundError, ValueError) as error:
        print(error, file=sys.stderr)
        return runner_module.EXIT_USAGE
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0
    run = status['run'] or {}
    activity = (' (active)' if status['runner_active'] else
                ' (no runner is active; "resume" continues)' if run.get('status') in ('running', 'waiting') else '')
    print(f'run: {run.get("status")}{activity}{" - " + run["message"] if run.get("message") else ""}')
    print(f'iteration: {status["iteration"]}/{status["max_iterations"]}')
    print(f'completed: {len(status["completed"])}  remaining: {len(status["remaining"])}  blocked: {len(status["blocked"])}')
    if status['current_task']:
        print(f'current task: {status["current_task"]}')
    if status['selection']:
        selection = status['selection']
        print(f'selection: {selection["task"]} -> {selection["model"]} / {selection["effort"]} ({selection.get("reason") or "no reason"})')
    for item in status['blocked']:
        print(f'blocked: {item["id"]}: {item["reason"]}')
        if item['needs_owner']:
            print(f'  needs you: {item["needs_owner"]}')
    if status['waiting_on_blocked']:
        print(f'waiting on blocked tasks: {len(status["waiting_on_blocked"])}')
    if status['latest_checks']:
        checks = status['latest_checks']
        summary = ', '.join(f'{c["name"]}={"pass" if c["passed"] else "FAIL"}' for c in checks['results'])
        print(f'latest checks: {checks["task"]}: {summary}; models used: {checks["actual_models"]}')
    final = status['final'] or {}
    if final.get('status'):
        verified = f' at {final["verified_commit"][:10]}' if final.get('status') == 'passed' and final.get('verified_commit') else ''
        print(f'final verification: {final["status"]}{verified}; tasks since: {final.get("since", 0)}')
        if final.get('status') != 'passed' and final.get('findings'):
            print(f'  {final["findings"][0][:200]}')
    elif project_module.has_final_checks(config_checks(args.project)):
        print(f'final verification: not run yet; tasks since: {final.get("since", 0)}')
    print(f'last commit: {status["last_commit"]}')
    push = status['last_push']
    print(f'last push: {push["commit"][:10]} on {push["branch"]} at {push["at"]}' if push else 'last push: none yet')
    for error in status['recent_errors']:
        print(f'error: {error["at"]} {error.get("task") or ""}: {error["message"][:200]}')
    return 0


def config_checks(root):
    try:
        return project_module.Project(root).config()['checks']
    except (FileNotFoundError, ValueError, KeyError):
        return []


def command_follow(args):
    """Show the run until interrupted. `--window` makes this view its project's only window."""
    project = configured_project(args.project)
    try:
        if args.window:
            with RunnerLock(project.viewer_lock_path):
                follow.follow(project)
        else:
            follow.follow(project)
    except (KeyboardInterrupt, LockHeld):
        return 0


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog='autopilot.py', description=__doc__.splitlines()[0])
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)

    def add(name, handler, description):
        sub = commands.add_parser(name, help=description, description=description)
        sub.add_argument('--project', default='.', help='project root (default: current folder)')
        sub.set_defaults(handler=handler)
        return sub

    sub = add('setup', command_setup, 'Create or complete the orchestration files. Never starts development.')
    sub.add_argument('--roadmap', help='roadmap path relative to the project root')
    sub.add_argument('--branch', help='working branch (default: main)')
    sub.add_argument('--remote', help='remote name (default: origin)')
    sub.add_argument('--json', action='store_true')
    sub = add('import-roadmap', command_import, 'Add unseen items from a Markdown roadmap to prd.json.')
    sub.add_argument('--roadmap', required=True)
    sub = add('validate', command_validate, 'Check configuration, dependencies, routing, Git and the remote.')
    sub.add_argument('--live', action='store_true', help='also probe real models and a real preflight (uses a little Claude usage)')
    sub.add_argument('--json', action='store_true')
    for name, handler, text in (('run', command_run, 'Start the autonomous loop.'),
                                ('resume', command_resume, 'Clear a stop request and continue an interrupted run.')):
        sub = add(name, handler, text)
        sub.add_argument('--max-iterations', type=positive_int, help='override limits.max_iterations for this run')
        sub.add_argument('--task', help='work only on this task')
        sub.add_argument('--supervised', action='store_true',
                         help='end the run at the first block or anomaly, for the session that started it to act on')
    add('stop', command_stop, 'Ask the active runner to stop safely.')
    add('pause', command_pause, 'Ask the active runner to end after its current attempt, interrupting nothing.')
    sub = add('status', command_status, 'Show tasks, iteration, selection, checks, commit, push and errors.')
    sub.add_argument('--json', action='store_true')
    sub = add('follow', command_follow, 'Show the run in real time, read-only, until interrupted.')
    sub.add_argument('--window', action='store_true', help='end at once if this project already has its window open')
    sub = add('unblock', command_unblock, 'Return a blocked task to the queue after you resolved its cause.')
    sub.add_argument('--task', required=True)
    sub.add_argument('--restore-work', action='store_true',
                     help='bring back the partial work its block set aside, for the next session to continue')
    sub = add('defer', command_defer, 'Set a task aside until a person acts; the run goes on without it.')
    sub.add_argument('--task', required=True)
    sub.add_argument('--reason', required=True, help='what a person has to do before the task can continue')
    sub = add('preflight', command_preflight, 'Run and save the preflight of one task, without implementing it.')
    sub.add_argument('--task', required=True)
    sub = add('route', command_route, 'Show the routing decision for a task type; launches nothing.')
    sub.add_argument('--task-type', required=True, choices=sorted(routing.POLICY))
    sub.add_argument('--model', help='a recommended model')
    sub.add_argument('--effort', help='a recommended effort')
    sub.add_argument('--sonnet-sufficient', action='store_true')
    sub.add_argument('--available', default='sonnet,opus', help='comma-separated available families')
    return parser


def interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    # Task titles can hold characters a Windows console cannot encode, which must not end a run.
    # Line buffering keeps the output of a run started in the background readable while it works.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace', line_buffering=True)
    # Project checks and hooks run code this process does not control. Started from a Claude Code
    # session, they must not inherit the channel into that session, which has wider permissions.
    for name in claude_cli.SESSION_VARIABLES:
        os.environ.pop(name, None)
    # A closed terminal or a `kill` must end the running session too, as Ctrl+C does, instead of orphaning it.
    for name in ('SIGTERM', 'SIGHUP', 'SIGBREAK'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except LockHeld as held:
        print(f'{held}\nUse "status" to watch it or "stop" to end it.', file=sys.stderr)
        return runner_module.EXIT_USAGE
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return runner_module.EXIT_PAUSED
    except (FileNotFoundError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return runner_module.EXIT_USAGE
    except (RuntimeError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return runner_module.EXIT_ERROR


if __name__ == '__main__':
    sys.exit(main())
