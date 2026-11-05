# Verification in Lite

Lite spends its verification where it is cheapest: fast checks after each task, the slow
suite once at the end. This is what makes a task quick and light on tokens, and it is also
the trade-off to state to the owner.

## Contents

- Per task
- The final verification
- What is given up
- Settings
- Returning to full rigor

## Per task

| Step | Full skill | Lite (default) |
| --- | --- | --- |
| Model decision | A preflight session per task | `taskType` in the plan, decided at setup |
| Implementation | One session | One session, told to read only what it needs |
| Checks | Every check | Only `"when": "task"` checks (build, typecheck, lint) |
| Change scan | Yes | Yes: secrets, sensitive files, disabled or deleted tests |
| Roadmap tick, changelog rule | Yes | Yes |
| Review session | Yes | No (`review.enabled: false`) |
| Commit and push | After all of the above | After all of the above |

A task with no per-task check is committed on the change scan alone, and `validate` warns
about it.

## The final verification

It runs every configured check, `task` and `final` alike, when no task is left to run (all
complete, or the rest blocked or waiting), and also every `verify.final_every_tasks`
completed tasks when that is above 0. It is skipped when no check has `"when": "final"`,
for a run limited to one task with `--task`, and when no project file changed since it last
passed.

When a required check fails:

1. A fix session starts (`verify.fix_model` and `verify.fix_effort`, default Sonnet at
   high) with the failures and the tasks delivered since the suite last passed.
2. The runner runs every check again and scans the change. A disabled or deleted test is
   refused, as for a task.
3. When everything passes, the fix is committed on its own, with no `Task:` line, and pushed.
4. Up to `limits.final_fix_attempts` (default 2) fix sessions. If the suite still fails, or
   the fix session reports that a person must decide, the fix work is set aside in a stash
   (`ajimu-autopilot: FINAL-fix`), and the run ends with exit status 4. `resume` verifies
   again, with fresh attempts.

The state lives in `.ajimu-autopilot/runtime/state.json` under `final`, and `status` shows
it. An interrupted fix session (`fixing`) owns its partial work: the next start verifies it
before any task begins.

## What is given up

- Commits reach the branch, and the remote, verified only by the fast checks. Between two
  final verifications the branch can hold a failing test suite. Do not use Lite where a push
  to the branch deploys, or where others build on it continuously.
- No session checks the acceptance criteria independently. The implementation session's
  honesty, the fast checks and the final suite are what remains.
- No session corrects, splits or recognizes as already done a task at run time. The plan
  has to be atomic and verifiable from setup.
- A regression is found later, so its fix may touch code written several tasks earlier.

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `checks[].when` | `task` when absent | `task` runs after each task; `final` only in the final verification |
| `verify.final_every_tasks` | 0 | Also verify every N completed tasks; 0 means only at the end |
| `verify.fix_model`, `verify.fix_effort` | `sonnet`, `high` | The fix session's model and effort |
| `limits.final_fix_attempts` | 2 | Fix sessions per final verification; 0 reports failures without fixing |
| `review.enabled` | false | true adds the review session after each task |
| `preflight.mode` | `plan` | `full` adds the preflight session before each task |

## Returning to full rigor

For a delicate phase, set `preflight.mode` to `full`, `review.enabled` to true and every
check's `when` to `task`: the runner then behaves like the full skill. The full
`ajimu-autopilot` skill also reads these files: it ignores `when` and `taskType`, runs
every check after each task, and keeps `review.enabled` as stored here, so set it to true
before switching. Both use the same lock, so only one runner works on a project at a time.
