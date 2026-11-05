"""Task plan rules over Ralph's prd.json: validation, versioning, selection and rewrites.

Upstream Ralph reads `branchName` with jq and leaves the stories to the agent, so
the additive story fields used here (`dependsOn`, `source`, `splitFrom`, and the
routing fields `taskType`, `model`, `effort`, `sonnetSufficient`) do not disturb it. Live execution state stays out of this file.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

from .store import read_json, write_json_atomic

TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
REQUIRED_FIELDS = ('id', 'title', 'description', 'acceptanceCriteria', 'priority', 'passes')
VERSIONED_FIELDS = ('id', 'title', 'description', 'acceptanceCriteria', 'dependsOn', 'priority')
EDITABLE_FIELDS = ('title', 'description', 'acceptanceCriteria', 'dependsOn', 'priority')
# Optional per-story routing, decided at setup; routing.plan_decision gives them meaning.
ROUTING_FIELDS = {'taskType': str, 'model': str, 'effort': str, 'sonnetSufficient': bool}


class PlanError(ValueError):
    """A requested plan change would break the plan; the plan is left untouched."""


def load(path):
    prd = read_json(path)
    if prd is None:
        raise FileNotFoundError(f'{path} does not exist; run setup first')
    return prd


def load_valid(path):
    """Return the plan, or raise PlanError naming every problem that makes it unsafe to use."""
    prd = load(path)
    problems = validate(prd)
    if problems:
        raise PlanError('; '.join(problems))
    return prd


def save(path, prd):
    problems = validate(prd)
    if problems:
        raise PlanError('; '.join(problems))
    write_json_atomic(path, prd)


def new_plan(project, branch, description=''):
    return {'project': project, 'branchName': branch, 'description': description, 'userStories': []}


def story(prd, task_id):
    for item in prd['userStories']:
        if item['id'] == task_id:
            return item
    raise KeyError(task_id)


def task_version(item):
    """Return the hash a preflight result is bound to; any requirement edit changes it."""
    canonical = {name: item.get(name) if name != 'dependsOn' else sorted(item.get(name) or [])
                 for name in VERSIONED_FIELDS}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]


def find_cycle(stories):
    """Return one dependency cycle as a list of IDs, or None."""
    graph = {item['id']: item.get('dependsOn') or [] for item in stories}
    state, path = {}, []

    def visit(node):
        state[node] = 'open'
        path.append(node)
        for target in graph.get(node, []):
            if state.get(target) == 'open':
                return path[path.index(target):] + [target]
            if target in graph and target not in state:
                found = visit(target)
                if found:
                    return found
        path.pop()
        state[node] = 'done'
        return None

    for node in graph:
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def _well_formed_dependencies(item):
    depends = item.get('dependsOn')
    return depends is None or (isinstance(depends, list) and all(isinstance(target, str) for target in depends))


def validate(prd):
    """Return every problem that would make the plan unsafe to schedule, or an empty list.

    Never raises for a malformed plan, which is usually a hand edit: each problem is named.
    """
    problems = []
    if not isinstance(prd, dict):
        return ['prd.json must contain a JSON object']
    for name in ('project', 'branchName'):
        if not isinstance(prd.get(name), str) or not prd.get(name):
            problems.append(f'"{name}" must be a non-empty string')
    stories = prd.get('userStories')
    if not isinstance(stories, list):
        return problems + ['"userStories" must be a list']
    seen = set()
    for index, item in enumerate(stories):
        label = item.get('id', f'#{index}') if isinstance(item, dict) else f'#{index}'
        if not isinstance(item, dict):
            problems.append(f'{label}: must be an object')
            continue
        missing = [name for name in REQUIRED_FIELDS if name not in item]
        if missing:
            problems.append(f'{label}: missing {", ".join(missing)}')
            continue
        if not isinstance(item['id'], str) or not TASK_ID.match(item['id']):
            problems.append(f'{label}: id must match {TASK_ID.pattern}')
            if not isinstance(item['id'], str):
                continue
        if item['id'] in seen:
            problems.append(f'{label}: duplicate id')
        seen.add(item['id'])
        if not _well_formed_dependencies(item):
            problems.append(f'{label}: dependsOn must be a list of task IDs')
        criteria = item['acceptanceCriteria']
        if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
            problems.append(f'{label}: acceptanceCriteria must be a non-empty list of strings')
        if not isinstance(item['priority'], int) or isinstance(item['priority'], bool):
            problems.append(f'{label}: priority must be an integer')
        if not isinstance(item['passes'], bool):
            problems.append(f'{label}: passes must be true or false')
        for name in ('title', 'description'):
            if not isinstance(item[name], str) or not item[name].strip():
                problems.append(f'{label}: {name} must be a non-empty string')
        for name, kind in ROUTING_FIELDS.items():
            if item.get(name) is not None and not isinstance(item[name], kind):
                problems.append(f'{label}: {name} must be {"true or false" if kind is bool else "a string"}')
    valid = [item for item in stories
             if isinstance(item, dict) and isinstance(item.get('id'), str) and _well_formed_dependencies(item)]
    for item in valid:
        for target in item.get('dependsOn') or []:
            if target not in seen:
                problems.append(f'{item["id"]}: unknown dependency {target}')
    cycle = find_cycle(valid)
    if cycle:
        problems.append('dependency cycle: ' + ' -> '.join(cycle))
    return problems


def select_next(prd, statuses):
    """Return the next runnable story, or None.

    A story already in flight goes first so an interrupted task resumes before new
    work starts. Otherwise the lowest priority number wins, ties keeping plan order.
    Either way its dependencies must be satisfied: a standalone preflight can leave a
    task `ready` before they are. `statuses` maps task IDs to runner states.
    """
    in_flight = ('ready', 'running', 'verification', 'preflight')
    done = {item['id'] for item in prd['userStories'] if item['passes']}
    runnable = [item for item in prd['userStories']
                if not item['passes'] and all(dep in done for dep in item.get('dependsOn') or [])]
    for item in runnable:
        if statuses.get(item['id']) in in_flight:
            return item
    candidates = [item for item in runnable if statuses.get(item['id'], 'pending') == 'pending']
    return min(candidates, key=lambda item: item['priority']) if candidates else None


def waiting_on_blocked(prd, statuses):
    """Return {task: [blocking IDs]} for stories that cannot run because of blocked ancestors."""
    blocked = {task for task, status in statuses.items() if status == 'blocked'}
    graph = {item['id']: item.get('dependsOn') or [] for item in prd['userStories']}

    def ancestors(node, seen):
        found = []
        for target in graph.get(node, []):
            if target in seen:
                continue
            seen.add(target)
            if target in blocked:
                found.append(target)
            found.extend(ancestors(target, seen))
        return found

    waiting = {}
    for item in prd['userStories']:
        if not item['passes'] and item['id'] not in blocked:
            reasons = ancestors(item['id'], set())
            if reasons:
                waiting[item['id']] = sorted(set(reasons))
    return waiting


def apply_correction(prd, task_id, corrected, replaced_criteria=None):
    """Return (new_prd, changed_fields) after applying a preflight correction.

    Requires every removed acceptance criterion to appear in `replaced_criteria`,
    which is what keeps a correction from quietly shrinking the requirement.
    Raises PlanError and leaves `prd` untouched when the result would be invalid.
    """
    updated = copy.deepcopy(prd)
    item = story(updated, task_id)
    changed = []
    for name in EDITABLE_FIELDS:
        if name in corrected and corrected[name] is not None and corrected[name] != item.get(name):
            item[name] = copy.deepcopy(corrected[name])
            changed.append(name)
    if 'acceptanceCriteria' in changed:
        removed = [c for c in story(prd, task_id)['acceptanceCriteria'] if c not in item['acceptanceCriteria']]
        justified = set(replaced_criteria or [])
        unjustified = [c for c in removed if c not in justified]
        if unjustified:
            raise PlanError(f'correction drops acceptance criteria without justification: {unjustified}')
    problems = validate(updated)
    if problems:
        raise PlanError('; '.join(problems))
    return updated, changed


def apply_split(prd, task_id, children):
    """Return (new_prd, child_ids) with the story replaced in place by its children.

    Dependents of the original now wait for every child. The original survives in
    the task record, so splitting loses no requirement text.
    """
    if len(children) < 2:
        raise PlanError('a split needs at least two tasks')
    updated = copy.deepcopy(prd)
    stories = updated['userStories']
    index = next(i for i, item in enumerate(stories) if item['id'] == task_id)
    parent = stories[index]
    child_ids = [f'{task_id}.{number}' for number in range(1, len(children) + 1)]
    built = []
    for position, (child_id, child) in enumerate(zip(child_ids, children)):
        previous = [child_ids[position - 1]] if child.get('sequential', True) and position else []
        outside = [dep for dep in child.get('dependsOn') or [] if dep not in child_ids and dep != task_id]
        built.append({
            'id': child_id,
            'title': child['title'],
            'description': child['description'],
            'acceptanceCriteria': child['acceptanceCriteria'],
            'priority': parent['priority'],
            'passes': False,
            'notes': '',
            'dependsOn': list(dict.fromkeys((parent.get('dependsOn') or []) + outside + previous)),
            'splitFrom': task_id,
            **({'source': parent['source']} if parent.get('source') else {}),
        })
    stories[index:index + 1] = built
    for item in stories:
        if task_id in (item.get('dependsOn') or []):
            item['dependsOn'] = [dep for dep in item['dependsOn'] if dep != task_id] + child_ids
    problems = validate(updated)
    if problems:
        raise PlanError('; '.join(problems))
    return updated, child_ids


def tick_roadmap(root, item):
    """Mark the roadmap line behind a story as done; return True when the line now reads done.

    Only an exact line recorded by the importer is edited, so a line rewritten by hand
    stays as it is and returns False. A `- TODO:` bullet has no checkbox, so it becomes a
    ticked one, which imports to the same ID. A line already ticked is left alone.
    """
    source = item.get('source') or {}
    path, line = source.get('path'), source.get('line')
    if not path or not line:
        return False
    target = Path(root) / path
    try:
        # Bytes, because text mode would rewrite CRLF files and turn one tick into a whole-file diff.
        text = target.read_bytes().decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return False
    body = rf'{re.escape(line)}[ \t]*\r?)$'
    for pattern, replacement in ((rf'^([ \t]*[-*+][ \t]+)\[ \]([ \t]+{body}', r'\1[x]\2'),
                                 (rf'^([ \t]*[-*+][ \t]+)(?i:TODO|FIXME)[ \t]*[:\-][ \t]*({body}', r'\1[x] \2')):
        updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
        if count:
            target.write_bytes(updated.encode('utf-8'))
            return True
    return bool(re.search(rf'^[ \t]*[-*+][ \t]+\[[xX]\][ \t]+{re.escape(line)}[ \t]*\r?$', text, flags=re.MULTILINE))


def siblings_open(prd, item):
    """Tell whether other parts of a split task remain, so their shared roadmap line must wait.

    Parts are matched by the line they trace to rather than by `splitFrom`, which names
    only the direct parent and would miss the children of a part that was split again.
    """
    source = item.get('source')
    return bool(source and item.get('splitFrom')) and any(
        other.get('splitFrom') and other.get('source') == source and other['id'] != item['id'] and not other['passes']
        for other in prd['userStories'])
