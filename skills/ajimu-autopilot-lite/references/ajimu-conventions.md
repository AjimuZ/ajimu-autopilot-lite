# Ajimu Conventions integration

[Ajimu Conventions](https://github.com/AjimuZ/ajimu-conventions) is a required dependency,
found at `.claude/skills/`, `~/.claude/skills/` or the plugin cache. It is not vendored, so
sessions always use the rules the owner has installed. If it is missing, `run` refuses to
start and `validate` prints the official install commands:

```bash
claude plugin marketplace add AjimuZ/ajimu-conventions
claude plugin install ajimu-conventions@ajimu-conventions
```

## Applied in every session, not just at setup

Sessions start with `--setting-sources project,local`, which also keeps broad user-level
permissions out. A side effect is that user-level skills are not listed in those sessions.
So the runner reads `SKILL.md` and injects its text into each implementation and review
session's system prompt.

**Proof of loading:** the system prompt contains a random token for that session. The
structured output must return it in `conventions_ack`, or the attempt fails. This proves the
session received the text. It cannot prove the session followed it, which is what the checks
below are for.

## Enforced by the runner

| Rule | How |
| --- | --- |
| Official validator on every message | `check_commit_message.py` runs before each commit |
| Neutral message, no trailers | Sessions cannot commit, and `attribution` is disabled in their settings |
| Roadmap | Runner ticks the item only after its own checks pass; the commit includes it |
| Changelog | Required when the session reports a user-visible change |
| Honest progress and metadata | Written by the runner from observed results |
| Hooks and identity | `git commit` runs with hooks active and the configured identity |
| Secrets | Scanned in the change before commit |

## Judged by the review session, when enabled

Whether affected documents match the code, whether comments are English and contract-focused,
and whether the change has unrelated edits. A review failure sends findings to the next attempt.
Lite disables the review by default (`review.enabled: false`): these judgments then rest on
the implementation session alone, while everything in the table above is still enforced.

## Asked of the implementation session

Technical and functional documentation, indexes, ADRs for material decisions, and comments.
These are judgments, so they rest on the session and the review rather than on a mechanical
check. Unaffected documentation stays untouched.

## A limitation of the validator

It rejects tool and vendor names anywhere in a message. A project that really is about one
of those tools needs wording that avoids the name, or the attempt fails and is retried with
the validator's output. No wording can help a task ID holding such a name, because every commit
of the task carries it: that task is blocked before any session runs, and `validate` names it.
