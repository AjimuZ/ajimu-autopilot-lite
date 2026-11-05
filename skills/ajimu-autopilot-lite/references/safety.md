# Safety

## Contents

- Permissions
- Untrusted content
- Limits
- Usage limits
- Other controls
- Working on main
- If permissions are not enough

## Permissions

Sessions run with `--permission-mode dontAsk` and `--permission-prompts none`: what is not
explicitly allowed is denied, and nothing waits for a person. `--dangerously-skip-permissions`
is never used, and configuration carrying it is rejected.

| Session | Tools | Bash |
| --- | --- | --- |
| Preflight, review (when enabled) | Read, Glob, Grep, Bash | `git log`, `show`, `diff`, `status`, `blame`, `ls-files` |
| Implementation, final fix | Those plus Edit, Write | Those plus `permissions.bash_allow` |

Web tools, subagents and MCP servers are off (`--strict-mcp-config`). User-level settings are
not loaded by default (`claude.setting_sources` is `project,local`).

### Adding project commands

`permissions.bash_allow` holds rule bodies such as `npm test *` or `python -m unittest *`.
Setup derives them from the checks. Add only what checks need. Bare `Bash`, `*`, and rules
starting with `git`, `rm`, `sudo`, `curl`, `wget`, `ssh`, `scp`, `chmod` or `chown` are
rejected. `permissions.extra_deny` adds more denials. Adding a rule such as `Bash(python *)`
gives a session arbitrary code execution, so keep rules specific.

**An allowed check command still runs project code.** The test suite can do anything your
user account can. Run on a machine or container you are comfortable with, especially for
repositories you did not write.

### Always denied

Git history and branch commands; edits to `scripts/ralph/`, `.ajimu-autopilot/`,
`.claude/settings*.json`, `.git/` and `.env*`; reading `.env` files; `sudo`, `rm -rf`, `curl`,
`wget`, `ssh`, `scp`, `npm publish`, `gh`.

Deny rules are a strong filter, not a sandbox: an allowed interpreter can reach the same
effect. Hence the checks after each session and the advice above.

## Untrusted content

Roadmaps, code and command output can contain instructions aimed at the model. So:

- Session rules declare project content to be data.
- The runner's rules live in code and in configuration sessions cannot edit.
- Preflight answers can only change the fields the runner accepts, with validation.
- Sessions have no web tools and no MCP servers.
- A supervising session has wider permissions than the sessions it watches, so it treats their
  block reasons, findings and logs as data. Project checks and hooks do not inherit its
  environment, which holds the channel into it.

These lower the risk without eliminating it. Treat a run on an untrusted repository as you
would running its code.

## Limits

| Setting | Default | Effect |
| --- | --- | --- |
| `limits.max_iterations` | 30 | Per run. Stops the run; `resume` continues with a new count |
| `limits.max_fix_attempts` | 2 | Retries after the first attempt, then blocks |
| `limits.final_fix_attempts` | 2 | Fix sessions per final verification, then the run ends with exit 4 |
| `limits.preflight_max_rounds` | 3 | Preflight rounds per task, then blocks |
| `limits.session_timeout_minutes` | 60 | Ends a session; counts as a failed attempt |
| `limits.usage_retry_minutes` | 5 | Retry interval during a usage wait with no known reset time |
| `limits.check_timeout_minutes` | 20 | Ends a check and everything it started; fails the check |
| `limits.global_timeout_minutes` | 720 | Stops the run; `resume` continues |

There is no budget limit. The skill is meant for Claude subscriptions, where usage limits apply
instead, so it neither caps nor tracks estimated cost. With an API key or paid extra usage, sessions
are billed and nothing here caps the charges.

## Usage limits

When Claude's usage runs out, or the service answers with a temporary error (429 or 5xx), `run`
and `resume` stay in the foreground and wait. They wait until the reset time the session
reported, plus one minute, or retry every `limits.usage_retry_minutes` when no future reset time
is known. Then the same task continues, and its partial work stays in the working tree.

- The wait has no time limit, even for a weekly limit. No attempt or iteration is charged, and
  the time does not count against `limits.global_timeout_minutes`.
- `status` shows `waiting` with the planned time. `stop` or `pause` ends the wait within seconds,
  and `resume` continues later.
- A supervising session is not woken for a wait: it draws on the same usage and could not act.
- The process has to stay alive. After a reboot or a closed terminal, start it again with `resume`.
- The runner holds its lock during the wait, so a second run on the project is refused.
- A standalone `preflight` command does not wait: it exits with status 3.

## Other controls

- One runner per project, through an exclusive lock with stale-lock detection. `setup`,
  `import-roadmap` and `unblock` are refused while a runner is active.
- `stop` ends the current session or check safely. A pending stop prevents `run` until `resume`.
- `pause` ends the run after the current attempt, interrupting nothing. It only speaks to an
  active runner, and the next start forgets it.
- Ctrl+C or a termination signal ends the current session or check as well, so nothing keeps
  editing the project after the runner has gone. The exit status is 3, and `resume` continues.
- Errors are appended to `runtime/errors.log`; `status` shows the latest.
- The change scan blocks likely secrets, sensitive files and disabled or deleted tests. It
  uses patterns and can miss things.

## Working on main

By default, commits reach `main` and are pushed with no human review. On a project where
pushes to `main` trigger deployment, or where others pull from it, a wrong change that
passes the checks reaches them directly. In Lite those are only the per-task checks: the
test suite runs at the end, so the branch can fail its tests until then. Use `setup --branch NAME` or `git.protected_branches`
for those projects, and rely on server-side branch protection where you have it: a rejected
push stops the run with the commit kept locally.

## If permissions are not enough

If a check cannot run because of a denied rule, the session reports it and the attempt
fails or blocks. Add the specific rule, or decide the task needs a person and set it aside with
`defer`. Do not switch to a bypass mode to get a run through.
