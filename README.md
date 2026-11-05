# Ajimu Autopilot Lite

A lighter version of [Ajimu Autopilot](https://github.com/AjimuZ/ajimu-autopilot) for small,
low-risk applications: it develops a roadmap autonomously with far fewer sessions, checks and
tokens per task, and runs the full test suite once at the end instead of after every task.

`/ajimu-autopilot-lite` analyses a project and prepares a plan in which **every task already
has its model and effort** (Sonnet or Opus, never below Sonnet). A runner then works through the
plan one task at a time, in fresh Claude Code sessions, committing and pushing each task.

## Lite compared with the full skill

| Per task | Ajimu Autopilot | Lite (default) |
| --- | --- | --- |
| Model and effort choice | Preflight session per task | Chosen per task at setup (`taskType` in the plan), same table and Sonnet floor |
| Claude sessions | Preflight (1-3 rounds) + implementation + review | Implementation only |
| Checks after each task | All, test suite included | Fast ones only (`"when": "task"`: build, typecheck, lint) |
| Test suite | After every task | Once at the end (`"when": "final"`), with a fix session if it fails |
| Task correction, split, "already done" | At run time, by the preflight | At setup, by the analysis |
| Change scan, conventions, commit validator, verified push, recovery | Yes | Yes |

What Lite gives up: commits are pushed after the fast checks only, so the branch can fail its
tests until the final verification; no independent session checks the acceptance criteria. Use
the full skill, or switch Lite to full rigor, for delicate work.
See [references/verification.md](skills/ajimu-autopilot-lite/references/verification.md).

## Requirements

| Requirement | Notes |
| --- | --- |
| Claude Code | Needs `--effort`, `--permission-prompts` and `--json-schema` |
| Python 3.9 or later | No packages needed |
| Git | With an identity configured and a remote you can push to. The project must be the repository's root |
| [Ajimu Conventions](https://github.com/AjimuZ/ajimu-conventions) | Required; applied in every session |
| Claude account | Access to Sonnet, and to Opus for tasks routed to it |

## Installation

```bash
git clone https://github.com/AjimuZ/ajimu-autopilot-lite
cd ajimu-autopilot-lite
python tools/install.py                # ~/.claude/skills/ajimu-autopilot-lite
```

Run it again after an update: it changes only the files that differ. Install the conventions:

```bash
claude plugin marketplace add AjimuZ/ajimu-conventions
claude plugin install ajimu-conventions@ajimu-conventions
```

## Usage

Inside Claude Code, in the project:

```text
/ajimu-autopilot-lite docs/ROADMAP.md     # configure: plan, taskType per task, check stages
/ajimu-autopilot-lite run                 # start, with the session listening
```

Or from any terminal, with `<skill>` the installed skill folder:

```bash
python "<skill>/scripts/autopilot.py" validate     # routing summary, check stages, Git, remote
python "<skill>/scripts/autopilot.py" run
python "<skill>/scripts/autopilot.py" status       # includes the final verification
python "<skill>/scripts/autopilot.py" follow
python "<skill>/scripts/autopilot.py" stop
python "<skill>/scripts/autopilot.py" resume
```

The other commands (`setup`, `import-roadmap`, `pause`, `unblock`, `defer`, `preflight`,
`route`) work as in the full skill.

## Configuration

`.ajimu-autopilot/config.json`, beyond the full skill's settings:

```json
{
  "checks": [
    {"name": "build", "command": "npm run build", "required": true, "when": "task"},
    {"name": "test", "command": "npm test", "required": true, "when": "final"}
  ],
  "preflight": {"mode": "plan"},
  "review": {"enabled": false},
  "verify": {"final_every_tasks": 0, "fix_model": "sonnet", "fix_effort": "high"},
  "limits": {"final_fix_attempts": 2}
}
```

Each story in `scripts/ralph/prd.json` carries its routing:

```json
{"id": "P02.1", "taskType": "integration_sensitive", "sonnetSufficient": true}
```

`taskType` picks the row of the routing table; `model` and `effort` can raise it, never lower
it. A story without `taskType` routes as `feature_standard`, and `validate` warns about it.

Set `verify.final_every_tasks` to N to also run the full suite every N tasks. Set
`preflight.mode` to `full`, `review.enabled` to true and every check's `when` to `task` to get
the full skill's behavior back.

## Compatibility with the full skill

Both use `scripts/ralph/` and `.ajimu-autopilot/` and the same lock, so a project can move
between them, and only one runner works on it at a time. The full skill ignores `when` and
`taskType` and keeps the stored `review.enabled`.

## Development

```bash
python -m unittest discover -s tests      # about 290 tests; the runner tests use a fake CLI
python tools/validate_skill.py            # package rules
python tools/audit_publication.py         # no private material before publishing
```

The tests inherited from the full skill run with `preflight.mode: full` and the review enabled,
so that behavior stays covered; `tests/test_lite.py` covers the lite paths.

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
