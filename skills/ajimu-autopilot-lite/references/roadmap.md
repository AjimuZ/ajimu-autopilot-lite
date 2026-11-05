# Roadmap and tasks

## The plan file

`scripts/ralph/prd.json` keeps Ralph's format. Upstream only reads `branchName` from it by
script, so the additive fields below are compatible.

```json
{
  "project": "MyApp",
  "branchName": "main",
  "description": "What this plan delivers",
  "userStories": [
    {
      "id": "P02.1",
      "title": "Import a Git bundle",
      "description": "Import a bundle into an isolated workspace.",
      "acceptanceCriteria": ["An import never writes to the original checkout", "Import tests pass"],
      "priority": 1,
      "passes": false,
      "notes": "",
      "dependsOn": ["P01.3"],
      "taskType": "feature_standard",
      "source": {"path": "docs/roadmap.md", "section": "P02", "line": "P02.1 Import a Git bundle."}
    }
  ]
}
```

| Requirement | Where it lives |
| --- | --- |
| Stable ID, title, description, priority, acceptance criteria, completion | `prd.json` (Ralph fields) |
| Dependencies and traceability | `prd.json` (`dependsOn`, `source`, `splitFrom`) |
| Model and effort of the task | `prd.json` (`taskType`, optional `model`, `effort`, `sonnetSufficient`) |
| Live status, attempts, blocks in progress | `.ajimu-autopilot/runtime/state.json` |
| Original text, corrections, preflight, executions, blockers | `.ajimu-autopilot/tasks/<ID>.json` |

## Import

`setup` and `import-roadmap` read Markdown:

- `- [ ]` and `- [x]` items, and `- TODO:` bullets, become tasks.
- A code such as `P02.1` becomes the ID. Other items get an ID hashed from their text, so
  an unchanged line keeps its ID when imported again. A repeated code or text gets a numbered
  suffix such as `-2`, because each occurrence is its own task.
- Nested checkboxes and indented lines stay in the parent's description.
- `**Depends on:** P01` applies to the section's items; `Exit:` lines become acceptance criteria.
  Both need their colon, so that ordinary prose is not read as a rule.
- `- [x]` items import with `passes: true` and a note that the claim is unverified.

Importing again only adds unseen items. It never overwrites a task, and a line that a task
already traces to through `source`, as a split or renamed task does, is not added again. The
roadmap must be inside the project, and its path is stored relative to the project root.

## Refining

The importer gives every task generic criteria and no `taskType`. In Lite no session
corrects or splits a task at run time, so this pass is where the plan gets its quality:

- **Atomic:** one deliverable that fits one session. Split anything that touches many
  subsystems now; nothing will split it later.
- **Routed:** set `taskType` from the model routing table on every task, and `model`,
  `effort` or `sonnetSufficient` where needed.
- **Few criteria:** two to four observable outcomes. Each one costs implementation time.
- **Verifiable:** each criterion can be checked by a test or an observable behavior.
  Prefer "Empty names return a validation error" to "Validation works".
- **Ordered:** a lower `priority` number runs first among tasks whose dependencies are done.
- **Complete:** every requirement of the source stays somewhere in the plan. If you reword,
  keep the source reference.

Keep each `source.line` exactly as imported: the runner uses it to tick the roadmap
checkbox after a task's checks pass, turning a `- TODO:` bullet into a ticked checkbox. Do
not add tasks for work nobody asked for.

An ID must also pass the commit-message validator, which rejects tool and vendor names on the
`Task:` line. Rename any task `validate` lists, keeping its `source`.

## Validation

`validate` rejects duplicate or unsafe IDs, missing fields, empty criteria, unknown
dependencies, cycles, and routing fields that are malformed or below the floor, and names
each problem. It warns about open tasks without `taskType`.
