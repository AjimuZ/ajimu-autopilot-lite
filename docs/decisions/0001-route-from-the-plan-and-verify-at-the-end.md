# 0001. Route tasks from the plan and run the slow checks at the end

Date: 2026-11-03. Status: accepted.

## Context

Ajimu Autopilot spends, per task, one to three preflight sessions, an implementation session, a
review session and every configured check, the full test suite included. For small, low-risk
applications this makes each task slow and expensive in tokens, while most of that rigor guards
against risks those projects do not have. The owner still wants each task to get its own model
and effort.

## Decision

Lite is a separate skill built on the same runner:

- Each story carries its routing (`taskType`, optional `model`, `effort`, `sonnetSufficient`),
  chosen once by the setup analysis. `routing.plan_decision` applies it through the same table,
  overrides and Sonnet floor as a preflight recommendation.
- Checks have a stage. Fast ones run after each task; slow ones only in a final verification,
  which runs every check and gives failures to a bounded fix session committed on its own.
- The review session is off by default; the preflight is optional (`preflight.mode: full`).
- Project files, folder and lock stay those of the full skill.

## Alternatives

- **A cheaper preflight per task** (one round, low effort): keeps a session per task, which is
  most of the cost to remove, and still decides with less context than the setup analysis has.
- **Options in the full skill:** would mix two trade-offs in one skill whose promise is
  per-task verification.
- **Tests only on request:** leaves regressions undetected; a final verification bounds the risk.

## Consequences

- One session per task, and only fast checks between tasks.
- Commits are pushed before the suite has run: the branch can fail its tests until the final
  verification. Lite is not for branches that deploy on push.
- A wrong `taskType` is not corrected at run time; `validate` shows the routing for review.
- A regression is found later and its fix may touch work of several tasks.
