# Changelog

## 0.1.0 - 2026-11-03

First release, derived from Ajimu Autopilot 1.6.0 and the fixes installed after it (an exact
acknowledgement token in the system prompt, and split children that never name siblings).

### Added

- Per-task routing from the plan: `taskType`, and optionally `model`, `effort` and
  `sonnetSufficient`, on each story. The same table, overrides and Sonnet floor apply; a request
  can raise a row but never lower it. `validate` shows how open tasks route and warns about
  stories without `taskType`.
- Check stages: `"when": "task"` checks run after each task, `"when": "final"` checks only in
  the final verification. Setup puts detected test suites in `final` and the rest in `task`.
- Final verification: when no task can run, or every `verify.final_every_tasks` tasks, every
  check runs; failures go to a fix session (`verify.fix_model`, `verify.fix_effort`), up to
  `limits.final_fix_attempts`, and the fix is committed and pushed on its own. A failure that
  persists is set aside in a stash and ends the run with exit status 4. `status` shows its state.
- `final_fix` prompt and `references/verification.md`.

### Changed

- Defaults: `preflight.mode` is `plan` (no preflight session per task) and `review.enabled` is
  false. `preflight.mode: full` and `review.enabled: true` restore the full skill's behavior.
- The implementation prompt asks for focused tests only where the logic needs them, tells the
  session not to run the full suite, and to read only the code the task touches.
- A new session receives the two latest progress entries, bounded to 3000 characters.
- The setup analysis stays short and does not run features or test suites.
