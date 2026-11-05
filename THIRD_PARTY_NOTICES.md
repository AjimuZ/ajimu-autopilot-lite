# Third-party notices

Ajimu Autopilot Lite is licensed under the MIT License (see [LICENSE](LICENSE)). It is derived
from Ajimu Autopilot 1.6.0 by the same author, under the same license. It has no package
dependencies and vendors no third-party code. This file records each external component and
how it relates to this project. Mention here does not imply endorsement by its authors.

## Summary

| Component | Source | License | Reference | Relationship |
| --- | --- | --- | --- | --- |
| Ralph | [snarktank/ralph](https://github.com/snarktank/ralph) | MIT | `6c53cb0b831ebe8739c6a003e22af14902d8b0b5` | File formats and pattern adopted; no code copied; not executed |
| Ajimu Conventions | [AjimuZ/ajimu-conventions](https://github.com/AjimuZ/ajimu-conventions) | MIT | `2cdfaef4a72d02afc67af652580ad7ffb4872d09` | Required at run time; installed separately; not redistributed |
| Claude Code | [Anthropic](https://code.claude.com/docs) | Anthropic's terms | Tested with 2.1.314 | Required at run time; installed separately; not redistributed |
| Python, Git | Their projects | Their own licenses | Python 3.9 or later | Required at run time; not redistributed |

## Ralph

Autopilot adopts Ralph's pattern and these elements, written anew here:

- the `prd.json` field names (`project`, `branchName`, `description`, `userStories`, `id`,
  `title`, `description`, `acceptanceCriteria`, `priority`, `passes`, `notes`);
- the `scripts/ralph/` location;
- the `progress.txt` layout and its headings (`# Ralph Progress Log`, `## Codebase Patterns`,
  `**Learnings for future iterations:**`).

These are small and largely functional, but they do come from Ralph, so its notice is kept
here:

```
MIT License

Copyright (c) 2026 snarktank

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Ralph itself credits [Geoffrey Huntley's Ralph pattern](https://ghuntley.com/ralph/).

## Ajimu Conventions

Same author and license as this project (`Copyright (c) 2026 Azim Ben Othman`, MIT). The rules
and `check_commit_message.py` are read from the installed skill at run time.
`tools/validate_skill.py` and the frontmatter rules in it are adapted from that repository's
validator.
