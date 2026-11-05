# Ralph integration

[Ralph](https://github.com/snarktank/ralph) is an autonomous loop: fresh instances, one
story per iteration, memory in Git, `prd.json` and `progress.txt`. Reference commit:
`6c53cb0b831ebe8739c6a003e22af14902d8b0b5` (2026-02-02). MIT license.

## What is kept

| Ralph contract | Here |
| --- | --- |
| Clean context per iteration | Every session is a new `claude -p` process, never resumed |
| `scripts/ralph/prd.json` with `branchName` and `userStories` | Same location and fields |
| `passes` marks completion | Same; set by the runner, not by a session |
| `progress.txt` with `## Codebase Patterns` on top and dated entries | Same format |
| One story at a time, in priority order | Same, with dependencies added |
| A maximum number of iterations | Default 30 (`limits.max_iterations`) |

## What is different, and why

The runner does not execute `ralph.sh`, and it cannot through a wrapper:

| `ralph.sh` | Requirement |
| --- | --- |
| Always passes `--dangerously-skip-permissions` | Limited permissions, no blanket bypass |
| Has no `--model` or `--effort` | A selected model and effort per task |
| Has no phase before implementation | A model and effort chosen per task; an optional preflight with a gate |
| Needs Bash and `jq` | Windows, Linux and macOS |
| Trusts the session's completion signal and commit | Runner-verified checks, commit and push |

Patching the script would mean maintaining a fork. So Autopilot reimplements the loop in
Python and stays compatible with the file formats. See
`docs/decisions/0001-own-loop-compatible-with-ralph.md` in the repository.

No Ralph code is copied. The plan and progress formats and their headings are used so the
files remain familiar; the attribution is in `THIRD_PARTY_NOTICES.md`.

## Using the files with upstream Ralph

The extra story fields are ignored upstream. If you run `ralph.sh` on the same plan, you
leave Autopilot's guarantees: no preflight, routing or verified push.
