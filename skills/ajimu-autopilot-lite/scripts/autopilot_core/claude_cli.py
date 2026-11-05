"""Launch one independent `claude -p` process and report what it really did.

Every session is a new process with no resume, a prompt on stdin (no command-line
length limit) and an environment rebuilt so nothing inherited can change the model.
The reported usage, not the request, is what the runner trusts afterwards.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import processes, routing

# Set by a parent Claude Code session; a child inheriting them would act as its nested session.
SESSION_VARIABLES = (
    'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_AGENT_SDK_VERSION', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_MESSAGING_SOCKET',
    'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_EMIT_STARTUP_TIMING', 'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING',
    'CLAUDE_CODE_ENABLE_TASKS', 'CLAUDE_CODE_QUESTION_PREVIEW_FORMAT', 'CLAUDE_CODE_STARTUP_FAILURE_RESULTS',
    'CLAUDE_CODE_EXECPATH', 'CLAUDE_EFFORT', 'AI_AGENT',
)
# Each of these can change the running model or effort. CLAUDE_CODE_EFFORT_LEVEL in
# particular outranks --effort, so leaving it set would make the recorded level false.
ROUTING_VARIABLES = (
    'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_FABLE_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL',
    'CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE',
    'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL', 'CLAUDE_CODE_BG_CLASSIFIER_MODEL', 'CLAUDE_CODE_AUTO_MODE_MODEL',
)
TRANSIENT_STATUSES = (429, 500, 502, 503, 504, 529)
AUTH_STATUSES = (401, 403)
# How Claude Code words a refused request when neither a status nor an event says so.
LIMIT_TEXT = re.compile(r"you've hit your .*limit|usage limit reached", re.IGNORECASE)


@dataclass
class SessionRequest:
    """One headless session. `model` must be an allowed family alias."""
    phase: str
    model: str
    effort: object
    prompt: str
    cwd: str
    log_path: str
    system_append: str = ''
    schema: dict = None
    tools: tuple = ()
    allow: tuple = ()
    deny: tuple = ()
    timeout_seconds: int = 3600
    setting_sources: str = 'project,local'
    sonnet_id: str = routing.DEFAULT_SONNET_ID


@dataclass
class SessionResult:
    """Observed outcome. `actual_models` comes from init and modelUsage, never from the request."""
    exit_code: object = None
    timed_out: bool = False
    stopped: bool = False
    init_model: object = None
    skills: list = field(default_factory=list)
    actual_models: list = field(default_factory=list)
    structured: object = None
    is_error: bool = False
    subtype: object = None
    api_error_status: object = None
    rate_limited: bool = False
    resets_at: object = None
    permission_denials: list = field(default_factory=list)
    result_text: str = ''
    stderr_tail: str = ''
    has_result: bool = False
    session_id: object = None

    @property
    def transient(self):
        """True when a usage or service limit ended the session, which says nothing about the work.

        A session that finished cleanly never counts, even after a rejected event: paid
        extra usage can carry it past the window that refused it.
        """
        if self.has_result and not self.is_error:
            return False
        return (self.rate_limited or self.api_error_status in TRANSIENT_STATUSES
                or bool(LIMIT_TEXT.search(f'{self.result_text}\n{self.stderr_tail}')))

    @property
    def auth_failure(self):
        return self.api_error_status in AUTH_STATUSES


def find_claude(config_command=None, environ=None):
    """Return the Claude Code command as an argv prefix, or None when no install is found.

    Order: project configuration, AJIMU_AUTOPILOT_CLAUDE, PATH, then the binary
    bundled with the VS Code extension, which many installs have without a PATH entry.
    """
    environ = os.environ if environ is None else environ
    if config_command:
        return list(config_command) if isinstance(config_command, list) else [config_command]
    if environ.get('AJIMU_AUTOPILOT_CLAUDE'):
        return [environ['AJIMU_AUTOPILOT_CLAUDE']]
    found = shutil.which('claude', path=environ.get('PATH'))
    if found:
        return [found]
    binary = 'claude.exe' if sys.platform == 'win32' else 'claude'
    candidates = []
    for editor in ('.vscode', '.vscode-insiders', '.cursor'):
        pattern = f'anthropic.claude-code-*/resources/native-binary/{binary}'
        candidates.extend((Path.home() / editor / 'extensions').glob(pattern))

    def version_key(path):
        return [int(number) for number in re.findall(r'\d+', path.parts[-4].split('claude-code-')[-1])[:3]]

    return [str(max(candidates, key=version_key))] if candidates else None


def child_environment(request, base=None):
    """Return the environment of one session.

    ANTHROPIC_DEFAULT_HAIKU_MODEL is the documented control for background queries,
    and it needs a full model ID: the alias `sonnet` is rejected there. The subagent
    variables pin any spawned agent to the selected family.
    """
    env = dict(os.environ if base is None else base)
    for name in SESSION_VARIABLES + ROUTING_VARIABLES:
        env.pop(name, None)
    env.update({
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': request.sonnet_id,
        'CLAUDE_CODE_SUBAGENT_MODEL': request.model,
        'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': '1',
        'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1',
        'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
        'GIT_TERMINAL_PROMPT': '0',
    })
    return env


def build_argv(command, request, system_file=None):
    """Return the full command line. Raises routing.ForbiddenModel before anything can launch.

    --fallback-model repeats the selected model: it overrides any `fallbackModel`
    setting, so the system can never be switched to another model behind the
    runner's back. An unavailable model fails the session instead.
    """
    family = routing.model_family(request.model)
    settings = {'availableModels': list(routing.ALLOWED_FAMILIES), 'attribution': {'commit': '', 'pr': ''},
                'includeGitInstructions': False}
    argv = list(command) + [
        '-p', '--model', family, '--fallback-model', family,
        '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
        '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
        '--setting-sources', request.setting_sources, '--strict-mcp-config',
        '--settings', json.dumps(settings, separators=(',', ':')),
    ]
    if request.effort is not None:
        argv += ['--effort', request.effort]
    if system_file:
        argv += ['--append-system-prompt-file', str(system_file)]
    if request.schema is not None:
        argv += ['--json-schema', json.dumps(request.schema, separators=(',', ':'))]
    argv += ['--tools', ','.join(request.tools)]
    if request.allow:
        argv += ['--allowedTools', *request.allow]
    if request.deny:
        argv += ['--disallowedTools', *request.deny]
    return argv


def note_rejection(result, info):
    """Record a refused request from a `rate_limit_info` object; `resetsAt` is in epoch seconds."""
    if isinstance(info, dict) and info.get('status') == 'rejected':
        result.rate_limited = True
        if isinstance(info.get('resetsAt'), (int, float)):
            result.resets_at = info['resetsAt']


def _ran_a_model(name):
    # '<synthetic>' marks locally generated messages, not an inference.
    return bool(name) and not name.startswith('<')


def parse_stream(path):
    """Build a SessionResult from a stream-json log, tolerating truncated or foreign lines."""
    result = SessionResult()
    models = []
    try:
        # Not splitlines: JSON strings can hold U+2028 or U+0085 unescaped, and cutting there would lose the event.
        lines = Path(path).read_text(encoding='utf-8', errors='replace').split('\n')
    except OSError:
        return result
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get('type') if isinstance(event, dict) else None
        if kind:
            note_rejection(result, event.get('rate_limit_info'))
        if kind == 'system' and event.get('subtype') == 'init':
            result.init_model = event.get('model')
            result.skills = event.get('skills') or []
            result.session_id = event.get('session_id')
            if _ran_a_model(result.init_model):
                models.append(result.init_model)
        elif kind == 'assistant':
            model = (event.get('message') or {}).get('model')
            if _ran_a_model(model):
                models.append(model)
        elif kind == 'result':
            result.has_result = True
            result.is_error = bool(event.get('is_error'))
            result.subtype = event.get('subtype')
            result.api_error_status = event.get('api_error_status')
            result.permission_denials = event.get('permission_denials') or []
            result.result_text = event.get('result') or ''
            result.structured = event.get('structured_output')
            for name, usage in (event.get('modelUsage') or {}).items():
                if _ran_a_model(name):
                    models.append(name)
                if isinstance(usage, dict) and usage.get('canonicalModel'):
                    models.append(usage['canonicalModel'])
    result.actual_models = list(dict.fromkeys(models))
    return result


def run_session(command, request, should_stop=None, base_env=None):
    """Run one session to completion, timeout or stop request; always returns a SessionResult.

    The stream is written to `request.log_path`, a runtime file outside Git. Raises
    routing.ForbiddenModel for a disallowed model and OSError if the binary is missing.
    """
    Path(request.log_path).parent.mkdir(parents=True, exist_ok=True)
    system_file = None
    scratch = tempfile.mkdtemp(prefix='ajimu-autopilot-')
    try:
        if request.system_append:
            system_file = Path(scratch) / 'system.md'
            system_file.write_text(request.system_append, encoding='utf-8')
        prompt_file = Path(scratch) / 'prompt.md'
        prompt_file.write_text(request.prompt, encoding='utf-8')
        stderr_file = Path(scratch) / 'stderr.txt'
        argv = build_argv(command, request, system_file)
        with open(prompt_file, 'rb') as stdin, open(request.log_path, 'wb') as stdout, open(stderr_file, 'wb') as stderr:
            process = subprocess.Popen(argv, cwd=request.cwd, env=child_environment(request, base_env),
                                       stdin=stdin, stdout=stdout, stderr=stderr, **processes.GROUP_OPTIONS)
            outcome = processes.wait_bounded(process, request.timeout_seconds, should_stop)
        result = parse_stream(request.log_path)
        result.exit_code = process.returncode
        result.timed_out = outcome == 'timed_out'
        result.stopped = outcome == 'stopped'
        result.stderr_tail = stderr_file.read_text(encoding='utf-8', errors='replace')[-2000:]
    finally:
        # On Windows a process the session left behind can still hold these files. A leftover
        # folder is harmless, and raising here would throw away the finished session's result.
        shutil.rmtree(scratch, ignore_errors=True)
    return result


def model_report(result, selected_model):
    """Compare the observed models with the selection.

    Returns {'actual', 'forbidden', 'fallback', 'downgraded', 'unverified'}. `forbidden`
    lists names below the floor. `fallback` is set when the selected family never ran,
    and `downgraded` when the family that ran instead ranks lower.
    """
    forbidden = routing.forbidden_in_usage(result.actual_models)
    families = {routing.model_family(name) for name in result.actual_models if name not in forbidden}
    selected = routing.model_family(selected_model)
    fallback = None
    downgraded = False
    if result.actual_models and selected not in families:
        fallback = sorted(families) or result.actual_models
        downgraded = all(routing.MODEL_RANK[family] < routing.MODEL_RANK[selected] for family in families)
    return {'actual': result.actual_models, 'forbidden': forbidden, 'fallback': fallback,
            'downgraded': downgraded, 'unverified': not result.actual_models}
