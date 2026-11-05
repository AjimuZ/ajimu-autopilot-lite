# Persistent memory and recovery

## Where things live

| File | In Git | Holds |
| --- | --- | --- |
| `scripts/ralph/prd.json` | Yes | Tasks and completion |
| `scripts/ralph/progress.txt` | Yes | Codebase patterns and one entry per finished task |
| `.ajimu-autopilot/config.json` | No | Limits, checks, routing overrides, permissions |
| `.ajimu-autopilot/tasks/<ID>.json` | No | Original text, corrections, latest preflight, executions, blockers |
| `CLAUDE.md` | Yes | Stable project conventions, updated by sessions when useful |
| Roadmap, changelog, docs, ADRs | Yes | Requirements and decisions |
| `.ajimu-autopilot/runtime/` | No | Live state, what tasks need from a person, lock, stop and pause flags, session logs, what the runner reported (`run.log`), the window's lock, errors, model probe |

The whole `.ajimu-autopilot/` folder stays local. Setup and the runner add it to the project's
`.gitignore`, and a rule the owner already wrote in another form is respected. The runner
commits that rule on its own at the start of a run. If the owner has other uncommitted edits
to `.gitignore`, the run stops as for any other uncommitted change.

A project from an earlier release has the folder in Git: the next run stops tracking it in one
commit and leaves the files on disk. Other clones lose their copies when they pull that commit.

Session transcripts are never committed. Stream logs stay in `runtime/logs/` and only the
latest `limits.keep_session_logs` (default 40) are kept.

## What a new session receives

Only what it needs: the task, findings from earlier attempts, the `## Codebase Patterns`
section and the two latest progress entries, bounded to 3000 characters. With
`preflight.mode: full`, the preflight conclusions too. It
can read anything else from the repository. `CLAUDE.md` loads automatically through the
project setting source.

Only the runner writes `prd.json`, `progress.txt` and the task records. Sessions return their
learnings as structured output. Sessions are denied edits to these files, and the runner
restores `prd.json`, `progress.txt` and the configuration if a shell command changes them
anyway. An edit made that way to a task record would not be detected.

## Recovery after a crash or reboot

Start the runner again with `resume`. A lock left by a dead process is reclaimed.

| Stage found | What happens |
| --- | --- |
| `preflight` | Restarts; preflights change nothing but metadata. Partial work from an earlier attempt stays in place |
| Final verification `fixing` | Its partial fix stays; the next start verifies and commits it before any task |
| `ready` | The gate is checked again; the preflight is rerun if stale |
| `running`, `verification` | Back to `ready`, partial work kept, and the next session is told to inspect it and continue |
| `committing` | A commit with this task's exact `Task:` line is adopted; otherwise the stored message is used |
| `pushing` | Compared with the remote; pushed only if missing |

If the folder is lost, `prd.json` still decides what is complete, and local commits missing
from the remote are published on the next start. Being behind or diverged from the remote
stops the run for a person to resolve.

A fresh clone has no `.ajimu-autopilot/`. Run `setup` there and set the project's checks and any
other configuration again: they do not travel with the repository. Task records do not either,
so open tasks get a new preflight, and correction history stays on the machine that made it.

An interrupted attempt, including one ended by Ctrl+C, keeps its attempt count. A stop request
or a usage limit gives the attempt back, because neither says anything about the work. A usage
limit does not need `resume`: the runner waits and continues by itself, as described in the
safety reference.
