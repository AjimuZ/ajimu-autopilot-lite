# Git workflow

Only the runner commits and pushes. Sessions are denied `git commit`, `push`, `reset`,
`rebase`, `merge`, `checkout`, `switch`, `stash`, `clean`, `restore`, `branch`, `tag`,
`config`, `remote` and `update-ref`. If `HEAD` or the branch moves during a session anyway,
the run halts with nothing discarded.

## Branch

Work happens on `git.branch`, which defaults to `main`: each task's commit is pushed there
directly, with no pull request and no human review in between. An absent branch is created
safely from the current commit, and `validate` warns about it first.

To keep autonomous work apart from the main line, set another branch with
`setup --branch autopilot/roadmap`. Merging it is then the owner's decision. A project
already configured keeps its branch when setup runs again. Branches listed in
`git.protected_branches` (empty by default) are refused.

Pushing publishes the whole local history of the branch, so any of the owner's unpushed
commits on it go out too, at the start of the run.

## Per task

1. Require a clean start: uncommitted project changes with no interrupted task stop the run.
2. Session implements. The runner checks that the branch and `HEAD` did not move.
3. Run checks, scan the change, tick the roadmap, run the review.
4. Validate the message: subject, optional body, and an exact `Task: <ID>` line. An ID the
   validator refuses blocks the task before any session runs, and `validate` names it.
5. Set `passes`, write progress, `git add --all` (honoring `.gitignore`), refuse sensitive
   paths, and commit with hooks active.
6. `git push --set-upstream origin <branch>`, then confirm with `git ls-remote` that the
   remote has this exact commit.
7. Mark `completed`, with `pushed_commit` and `pushed_at`.

A commit refused by a hook returns the task to `ready` with the hook's output, and the
changes stay in the working tree. `--no-verify` does not exist in this code.

A fix from the final verification is its own commit, with the fix session's validated
message (or a neutral one if the validator refuses it) and no `Task:` line, pushed the same
way. A refused commit or a hook failure counts as a failed fix attempt.

Plan-only changes (a block, a split, plan edits) go in separate commits touching only
`scripts/ralph/`. They have no `Task:` line, so a recovery can never mistake them for delivered
work. The `.ajimu-autopilot/` folder is never committed.

## Committed versus published

`committing` and `pushing` are separate states. While a task is in `pushing`, its commit is
safe locally and no new task starts. `status` shows the last confirmed push.

| Push problem | Behavior |
| --- | --- |
| Remote unreachable or rejected | Commit kept, status `pushing`, exit 3. `resume` retries |
| Local is ahead on restart | Pushed before new work; a refusal is exit 3 again |
| Local is behind or diverged | Run stops with a message. Never rebased or forced |

## Never done

Force push, merges between branches, rebase, reset of work, deployment, changes to Git
configuration, or force-adding ignored files.

## Blocked tasks

Partial work is moved to a stash labelled `ajimu-autopilot: <ID>-attempt`, or
`ajimu-autopilot: FINAL-fix` for a final verification that could not be fixed. A ref at
`refs/ajimu-autopilot/preserved/<ID>-attempt` keeps it reachable even if the stash list is
cleared. Recover it with:

```bash
git stash list
git stash apply <stash>
```

After resolving the cause, `unblock --task <ID>` returns the task to the queue with fresh
attempts. `unblock --task <ID> --restore-work` also brings the partial work back to the tree,
and the next session continues from it under the usual checks, review and commit. It refuses a
tree with other uncommitted changes. If the work should be dropped instead, edit the plan.

A block adds a `BLOCKED:` line at the top of the task's `notes` and keeps the rest; `unblock`
removes only that line. Notes are how a person passes instructions to the next session.

`defer --task <ID> --reason <text>` records what a person has to do. A task not blocked yet is
blocked then, with the same handling of its partial work.
