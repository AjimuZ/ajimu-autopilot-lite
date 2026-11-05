# Architecture

## Two separate responsibilities

| Part | Runs in | Does |
| --- | --- | --- |
| Skill (`/ajimu-autopilot`) | An interactive Claude Code session | Analyses the project, refines the plan, validates, and on request starts the runner and fixes what stops it. Never develops. |
| Runner (`scripts/autopilot.py`) | Any terminal, or that session's background | Runs the loop by launching independent `claude -p` processes. |

Lite keeps this split and the same modules as the full skill. What changes is how much each
task costs: one session instead of three or more, and only the fast checks.

The runner is deterministic Python using only the standard library. Sessions propose
work. The runner owns state, checks, commits and pushes, so a task's completion never
rests on what a session claims. A supervising session is not an exception: what it fixes
still goes through the runner's checks, review and commit.

## One iteration

1. Select the next task whose dependencies are complete. An interrupted task goes first.
2. Route it from its plan entry (`taskType`, optional `model`, `effort`). With
   `preflight.mode: full`, a preflight session and its gate take this place instead.
3. Launch an implementation session on the selected model and effort.
4. Verify: the per-task checks, change scan, roadmap tick, and the review session if
   `review.enabled`.
5. Validate the commit message, commit, push, and confirm the remote holds the commit.
6. When no task can run (or every `verify.final_every_tasks` tasks), run the final
   verification: every check, a fix session for failures, and a separate fix commit.

A failed step records findings and the next iteration retries, up to the attempt limit.
Then the task is blocked, its partial work preserved, and independent tasks continue.

A `--supervised` run differs in one way: at the first new block or anomaly it exits at the
next boundary, so that the session that started it can act on a quiet tree. Each item is
raised once, so `resume` goes on without the blocked task. It also opens the project's
read-only window, a separate process that only reads the logs and cannot affect the run.

## Task states

```
pending -> preflight -> ready -> running -> verification -> committing -> pushing -> completed
              |           ^          |            |              |
              v           +----------+------------+--------------+   (retry with findings)
           blocked <--- any stage before committing; `unblock` returns it to pending
```

Illegal transitions raise an error. State is written before each risky step.

## Modules

| Module | Responsibility |
| --- | --- |
| `routing.py` | Model and effort policy, plan routing, the Sonnet floor |
| `preflight.py` | Result contract, validation, the implementation gate |
| `prd.py` | Plan rules, versioning, selection, corrections, splits |
| `roadmap.py` | Markdown roadmap import |
| `claude_cli.py` | Session launch, environment, and reading what really ran |
| `processes.py` | Ending a session or check together with everything it started |
| `gitops.py` | The only Git operations allowed |
| `quality.py` | Check execution, secret and disabled-test scan |
| `conventions.py` | Locating Ajimu Conventions and its validator |
| `project.py` | Project layout, configuration, check stages, setup, progress file |
| `runner.py` | State machine, recovery, the loop, the final verification |
| `doctor.py` | Validation and live probes |
| `follow.py` | The read-only view of a run, and its per-project window |

`routing.py`, `preflight.py` and `prd.py` do no I/O beyond reading their assets, so they
can be tested without launching anything.

## Commands

`setup`, `import-roadmap`, `validate [--live]`, `run`, `resume`, `stop`, `pause`,
`status [--json]`, `follow`, `unblock --task ID [--restore-work]`, `defer --task ID --reason TEXT`,
`preflight --task ID`, `route --task-type TYPE`. All accept `--project PATH` except `route`.
`run` and `resume` accept `--max-iterations N`, `--task ID` and `--supervised`.

Exit status: 0 finished, 1 error, 2 usage or configuration, 3 paused and resumable, 4
only blocked tasks remain or the final verification failed, 5 a limit was reached.
