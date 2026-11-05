# Task Preflight

**Optional in Lite.** It runs only with `preflight.mode: full` in the configuration. By
default (`plan`) tasks route from their plan entry and no preflight session starts; the
`preflight --task ID` command still works on demand, for one task you want analysed.

With `preflight.mode: full`, no task reaches implementation without one. The preflight is a separate, read-only session
with a fixed input, a JSON Schema output and deterministic checks on the result. It runs on
at least Sonnet (`preflight.model` and `preflight.effort`, default Sonnet at medium).

## Input and analysis

The prompt gives the task, its version, dependency status, source, documentation paths,
checks and recent commits. The session then reads the repository and judges the original
requirement, criteria, dependencies, real code state, regressions, architectural risk,
complexity, change size and required tests.

## Result

The schema is `assets/schemas/preflight.schema.json`. Key fields:

| Field | Meaning |
| --- | --- |
| `task_id`, `analyzed_task_version` | Bind the result to one exact task text |
| `outcome` | `ready`, `corrected`, `blocked`, `split` or `already_implemented` |
| `issues`, `corrections` | Problems found; each correction has a reason and evidence |
| `corrected_task`, `split_tasks` | The proposed new content |
| `functional_change`, `blocked_reason` | Whether a product decision is required, and which |
| `complexity`, `estimated_change_size`, `risks` | The assessment |
| `dependencies_verified`, `required_tests` | Verified prerequisites and tests to write |
| `acceptance_evidence` | Per-criterion evidence, required for `already_implemented` |
| `task_type`, `recommended_model`, `recommended_effort`, `routing_reason` | Routing input |
| `sonnet_sufficient` | Whether the task may proceed if Opus is unavailable |

The runner adds `head_commit` and the final `routing` decision.

## What the runner does with it

The answer is untrusted input. `validate_result` rejects a wrong schema, another task, a
stale version, a missing reason or a recommendation below Sonnet. A rejected answer uses
one of `limits.preflight_max_rounds` (default 3); after the last, the task is blocked.

| Outcome | Result |
| --- | --- |
| `ready` | Status `ready`; the gate can now pass |
| `corrected` | Applied, recorded, and evaluated again in a new session |
| `split` | Children replace the task in place, and each gets its own preflight |
| `already_implemented` | Runner's checks plus an independent review; completes only if both pass. A rejected claim goes back to the preflight with the findings |
| `blocked` | Recorded and published; independent tasks continue |

## Correction rules

- A correction may change title, description, criteria, dependencies and priority. It can
  never change the ID or the completion state.
- Removing a criterion requires listing it in `replaced_criteria`.
- `functional_change: true` is refused and blocks the task: product decisions stay with
  the owner.
- A correction that adds a cycle or an unknown dependency is refused.
- The original text and each change with its reason are kept in
  `.ajimu-autopilot/tasks/<ID>.json`, so corrections survive into later sessions and stay
  traceable to the requirement on this machine; that folder is not in Git.

## The gate

`require_passed` runs immediately before each implementation. It refuses when the outcome
is not `ready`, the task changed, the commit changed, or the routing decision is missing or
below the floor. A retry on the same task and commit reuses the valid preflight.

## Run it alone

```bash
python scripts/autopilot.py preflight --task P02.1 --project PATH
```

This saves the result without implementing. A finished or blocked task is refused, and the
exit status is 4 only when the task ended up blocked. The routing and validation logic can
also be tested with no process at all (`tests/test_preflight.py`, `tests/test_routing.py`).
