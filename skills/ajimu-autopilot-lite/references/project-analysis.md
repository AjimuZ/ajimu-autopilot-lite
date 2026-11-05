# Project analysis

Do this before refining the plan. Read; do not change application code. In Lite, keep it
short: the goal is a plan that is atomic, ordered and routed, not an audit.

## Identify

- Stack, versions, build tools and package manager, from manifests rather than guesses.
- Architecture: entry points, modules and their boundaries, data storage, external services.
- Conventions: `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING`, linters, formatting, test layout.
- Documentation: index, requirements, architecture notes, ADRs, changelog.
- Planned work: roadmap, TODO files, specifications, and `TODO`/`FIXME` markers in the code.

## Compare requirements with reality

The original documentation is the primary source of requirements, but it must be checked
against what the software really does. For each planned or "done" item:

| Finding | Action |
| --- | --- |
| Marked done and the behavior works | Keep `passes: true`; say how you checked |
| Marked done but broken or partial | Set `passes: false`; note the gap in `notes` |
| Not marked but really implemented | Leave `passes: false` so the preflight verifies it, and note it |
| Not implemented | A normal task |
| Documented behavior conflicts with the code | Record the conflict and ask; do not guess which is right |

Existing code is not evidence of completion. Lite does not run features or test suites
during the analysis: read the code the item names, and record what you only read as
unverified in `notes`. The final verification runs the suite on the delivered work.

## Routing

For each task, judge what it touches, how hard it is to verify and what a mistake would
cost, and set `taskType` from the model routing table. This replaces the per-task preflight.

## Dependencies

Find real prerequisites: schema before API, API before UI, shared module before its users.
Add them to `dependsOn`. A cycle fails validation, so fix the plan instead of removing
dependencies blindly.

## Report

List what you found, what is uncertain and each decision that needs the owner. An open
product question becomes a blocker in the plan, not an invented answer.
