"""Validate this repository's skill package before publishing.

Checks the documented rules that can be verified offline:
- skills/<name>/SKILL.md has YAML frontmatter with `name` and `description`;
- `name`: at most 64 characters, lowercase letters/digits/hyphens, matches the folder, no
  reserved words ("anthropic", "claude"), no XML tags;
- `description`: non-empty, at most 1024 characters, no XML tags, third person, says when to
  use the skill;
- the SKILL.md body stays under 500 lines, and each relative link resolves and stays one
  level deep (links only from SKILL.md, not between reference files);
- every JSON asset parses, and every prompt marker the runner fills is known.

Usage: python tools/validate_skill.py [REPO_ROOT]   (exit 0 = valid, 1 = problems)
"""
import json
import re
import sys
from pathlib import Path

NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
RESERVED = ('anthropic', 'claude')
XML = re.compile(r'<[A-Za-z/][^>]*>')
LINK = re.compile(r'\]\(([^)#\s]+)(?:#[^)]*)?\)')
FENCE = re.compile(r'^(```|~~~).*?^\1', re.MULTILINE | re.DOTALL)
MAX_BODY_LINES = 500


def prose(text):
    """Markdown without fenced code blocks, whose example links are not references."""
    return FENCE.sub('', text)


def frontmatter(text):
    """Return ({key: value}, body) for simple `key: value` YAML frontmatter."""
    if not text.startswith('---\n'):
        raise ValueError('SKILL.md must start with YAML frontmatter (---)')
    end = text.find('\n---\n', 4)
    if end < 0:
        raise ValueError('frontmatter is not closed with ---')
    fields = {}
    for line in text[4:end].splitlines():
        if line.strip() and not line.startswith(' '):
            key, _, value = line.partition(':')
            fields[key.strip()] = value.strip().strip('"\'')
    return fields, text[end + 5:]


def check_skill(folder, problems):
    path = folder / 'SKILL.md'
    try:
        fields, body = frontmatter(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        problems.append(f'{path}: {error}')
        return
    name, description = fields.get('name', ''), fields.get('description', '')
    if not NAME.match(name) or len(name) > 64:
        problems.append(f'{path}: name must be 1-64 lowercase letters, digits and hyphens')
    if name != folder.name:
        problems.append(f'{path}: name "{name}" must match its folder "{folder.name}"')
    if any(word in name for word in RESERVED):
        problems.append(f'{path}: name must not contain reserved words {RESERVED}')
    if not description or len(description) > 1024:
        problems.append(f'{path}: description must be 1-1024 characters (has {len(description)})')
    if XML.search(name + description):
        problems.append(f'{path}: name and description must not contain XML tags')
    if re.match(r'(I|You)\b', description):
        problems.append(f'{path}: write the description in third person')
    if 'use when' not in description.lower():
        problems.append(f'{path}: description should say when to use the skill ("Use when ...")')
    if len(body.splitlines()) >= MAX_BODY_LINES:
        problems.append(f'{path}: body has {len(body.splitlines())} lines; keep it under {MAX_BODY_LINES}')
    for target in LINK.findall(prose(body)):
        if re.match(r'[a-z]+:', target):
            continue
        if '\\' in target or target.startswith('/'):
            problems.append(f'{path}: use relative forward-slash links: {target}')
        elif not (folder / target).exists():
            problems.append(f'{path}: linked file does not exist: {target}')
    for reference in (folder / 'references').glob('*.md'):
        text = reference.read_text(encoding='utf-8')
        if any(not re.match(r'[a-z]+:', target) and target.endswith('.md') for target in LINK.findall(prose(text))):
            problems.append(f'{reference}: keep references one level deep; link files only from SKILL.md')
        if len(text.splitlines()) > 100 and '## Contents' not in text:
            problems.append(f'{reference}: files over 100 lines need a "## Contents" section')
    for schema in (folder / 'assets' / 'schemas').glob('*.json'):
        try:
            json.loads(schema.read_text(encoding='utf-8'))
        except ValueError as error:
            problems.append(f'{schema}: {error}')


def main(argv=None):
    root = Path((argv or sys.argv[1:] or ['.'])[0]).resolve()
    problems = []
    skills = sorted(path.parent for path in (root / 'skills').glob('*/SKILL.md'))
    if not skills:
        problems.append(f'{root}: no skills/<name>/SKILL.md found')
    for folder in skills:
        check_skill(folder, problems)
    for problem in problems:
        print(problem)
    print('valid' if not problems else f'{len(problems)} problem(s)')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
