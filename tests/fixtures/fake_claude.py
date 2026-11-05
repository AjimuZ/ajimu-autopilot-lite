"""Stand-in for the Claude Code CLI: records each launch and plays a scripted session.

The scenario file maps a phase name to a list of steps consumed in order. A step may
set `structured`, `models`, `edits` (paths to contents), `delete`, `run` (argv),
`is_error`, `subtype`, `api_error_status`, `rate_limited`, `resets_at`, `text`, `sleep`,
`linger` (seconds a child outlives this process, holding its output handles), or
`exit`. Phases come from the
`AUTOPILOT-PHASE:` line of the prompt. `__VERSION__` and `__ACK__` in a structured value
are replaced by the task version in the prompt and the ack token in the system file.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def fill(value, version, ack):
    if isinstance(value, str):
        return value.replace('__VERSION__', version).replace('__ACK__', ack)
    if isinstance(value, list):
        return [fill(item, version, ack) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, version, ack) for key, item in value.items()}
    return value


def main():
    argv = sys.argv[1:]
    if argv == ['--version']:
        print('2.1.999 (fake)')
        return 0
    prompt = sys.stdin.read()
    log_path, scenario_path = os.environ['FAKE_CLAUDE_LOG'], os.environ['FAKE_CLAUDE_SCENARIO']
    phase = re.search(r'AUTOPILOT-PHASE: (\S+)', prompt)
    phase = phase.group(1) if phase else 'probe'
    version = re.search(r'AUTOPILOT-TASK-VERSION: (\S+)', prompt)
    version = version.group(1) if version else ''
    system_file = option(argv, '--append-system-prompt-file')
    system = Path(system_file).read_text(encoding='utf-8') if system_file else ''
    ack = re.search(r'acknowledgement token is (\S+)', system)
    ack = ack.group(1) if ack else ''
    with open(log_path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'phase': phase, 'pid': os.getpid(), 'argv': argv, 'cwd': os.getcwd(), 'prompt': prompt,
                                 'has_conventions': 'Ajimu conventions' in system,
                                 'env': {k: v for k, v in os.environ.items()
                                         if k.startswith(('ANTHROPIC_', 'CLAUDE_CODE_', 'CLAUDECODE'))}}) + '\n')
    scenario = json.loads(Path(scenario_path).read_text(encoding='utf-8'))
    steps = scenario.get(phase) or []
    step = steps.pop(0) if steps else {}
    Path(scenario_path).write_text(json.dumps(scenario), encoding='utf-8')
    time.sleep(step.get('sleep', 0))
    for path, content in (step.get('edits') or {}).items():
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    for path in step.get('delete') or []:
        Path(path).unlink()
    if step.get('run'):
        subprocess.run(step['run'], check=False, capture_output=True)
    if step.get('linger'):
        subprocess.Popen([sys.executable, '-c', f'import time; time.sleep({step["linger"]})'], close_fds=False,
                         stdin=subprocess.DEVNULL)
    if 'exit' in step:
        return step['exit']
    requested = option(argv, '--model')
    models = step.get('models') or [f'claude-{requested}-5-5']
    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': models[0], 'skills': [], 'session_id': 'fake'}))
    if step.get('rate_limited'):
        reset = {'resetsAt': step['resets_at']} if 'resets_at' in step else {}
        print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', **reset}}))
    is_error = step.get('is_error', False)
    print(json.dumps({
        'type': 'result', 'subtype': step.get('subtype', 'error_during_execution' if is_error else 'success'),
        'is_error': is_error, 'api_error_status': step.get('api_error_status'), 'result': step.get('text', ''),
        'total_cost_usd': step.get('cost', 0.01), 'permission_denials': [],
        'modelUsage': {name: {'canonicalModel': name} for name in models},
        **({'structured_output': fill(step['structured'], version, ack)} if 'structured' in step else {}),
    }))
    return 0


if __name__ == '__main__':
    sys.exit(main())
