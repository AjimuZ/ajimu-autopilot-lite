"""Task Preflight: the structured contract and gate in front of every implementation.

The model's answer is treated as untrusted input. It is checked here against the
schema, the analysed task version and the model policy, and only this module's
functions turn it into plan changes. `require_passed` is the single gate the
runner must clear before starting an implementation session.
"""
import json
import re
from pathlib import Path

from . import prd as prd_module
from . import routing

OUTCOMES = ('ready', 'corrected', 'blocked', 'split', 'already_implemented')
ASSETS = Path(__file__).resolve().parents[2] / 'assets'


class PreflightInvalid(ValueError):
    """The model's answer breaks the contract; it must not influence the plan."""


class PreflightGateError(RuntimeError):
    """No valid, passed preflight exists for this task version and commit."""


def load_schema(name):
    return json.loads((ASSETS / 'schemas' / f'{name}.schema.json').read_text(encoding='utf-8'))


def load_prompt(name):
    return (ASSETS / 'prompts' / f'{name}.md').read_text(encoding='utf-8')


def render(template, **values):
    """Fill {{name}} markers in one pass, so substituted task text cannot inject new markers."""
    return re.sub(r'\{\{(\w+)\}\}', lambda match: str(values.get(match.group(1), match.group(0))), template)


def check_schema(value, schema, path='$'):
    """Return the violations of a small JSON Schema subset, or an empty list.

    The CLI validates too, but this boundary must hold for any process output and
    the project stays free of third-party packages.
    """
    errors = []
    expected = schema.get('type')
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        checks = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
                  'string': lambda v: isinstance(v, str), 'boolean': lambda v: isinstance(v, bool),
                  'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
                  'null': lambda v: v is None}
        if not any(checks[name](value) for name in types):
            return [f'{path}: expected {"/".join(types)}, got {type(value).__name__}']
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: {value!r} is not one of {schema["enum"]}')
    if isinstance(value, dict):
        for name in schema.get('required', []):
            if name not in value:
                errors.append(f'{path}: missing "{name}"')
        properties = schema.get('properties', {})
        for name, item in value.items():
            if name in properties:
                errors.extend(check_schema(item, properties[name], f'{path}.{name}'))
            elif schema.get('additionalProperties') is False:
                errors.append(f'{path}: unexpected "{name}"')
    if isinstance(value, list) and 'items' in schema:
        for index, item in enumerate(value):
            errors.extend(check_schema(item, schema['items'], f'{path}[{index}]'))
    return errors


def validate_result(raw, item, head, *, overrides=None, available_families=routing.ALLOWED_FAMILIES):
    """Return the accepted preflight record or raise PreflightInvalid.

    Binds the answer to the task version and commit it analysed, and computes the
    routing decision. A recommendation below Sonnet is a contract violation even if
    it arrives through a session that skipped CLI schema validation. When Opus is
    needed but unavailable and Sonnet is not enough, the record becomes `blocked`.
    """
    if not isinstance(raw, dict):
        raise PreflightInvalid('the session returned no structured result')
    errors = check_schema(raw, load_schema('preflight'))
    if errors:
        raise PreflightInvalid('; '.join(errors[:8]))
    if raw['task_id'] != item['id']:
        raise PreflightInvalid(f'result is for {raw["task_id"]}, expected {item["id"]}')
    version = prd_module.task_version(item)
    if raw['analyzed_task_version'] != version:
        raise PreflightInvalid(f'result analysed version {raw["analyzed_task_version"]}, current is {version}')
    outcome = raw['outcome']
    if outcome == 'corrected' and not raw.get('corrected_task'):
        raise PreflightInvalid('outcome "corrected" requires corrected_task')
    if outcome == 'corrected' and not raw.get('corrections'):
        raise PreflightInvalid('outcome "corrected" requires the reason for each correction')
    if outcome == 'split' and len(raw.get('split_tasks') or []) < 2:
        raise PreflightInvalid('outcome "split" requires at least two split_tasks')
    if outcome == 'blocked' and not (raw.get('blocked_reason') or '').strip():
        raise PreflightInvalid('outcome "blocked" requires blocked_reason')
    if outcome == 'already_implemented':
        evidence = raw.get('acceptance_evidence') or []
        covered = {entry['criterion'] for entry in evidence if entry.get('met') and entry.get('evidence', '').strip()}
        missing = [criterion for criterion in item['acceptanceCriteria'] if criterion not in covered]
        if missing:
            raise PreflightInvalid(f'"already_implemented" lacks evidence for: {missing}')
    record = dict(raw)
    record['head_commit'] = head
    record['routing'] = None
    if outcome in ('ready', 'corrected', 'already_implemented'):
        try:
            decision = routing.select(raw['task_type'], raw['recommended_model'], raw['recommended_effort'],
                                      reason=raw['routing_reason'], overrides=overrides,
                                      available_families=available_families,
                                      sonnet_sufficient=raw['sonnet_sufficient'])
        except routing.ForbiddenModel as error:
            raise PreflightInvalid(str(error)) from error
        except routing.RoutingError as error:
            record['outcome'] = 'blocked'
            record['blocked_reason'] = f'model routing: {error}'
        else:
            record['routing'] = decision.to_dict()
    return record


def require_passed(record, item, head):
    """Raise PreflightGateError unless `record` authorises implementing `item` at `head`."""
    if not record:
        raise PreflightGateError(f'{item["id"]}: no preflight result exists')
    if record.get('outcome') != 'ready':
        raise PreflightGateError(f'{item["id"]}: preflight outcome is {record.get("outcome")}, not ready')
    version = prd_module.task_version(item)
    if record.get('analyzed_task_version') != version:
        raise PreflightGateError(f'{item["id"]}: preflight analysed another task version')
    if record.get('head_commit') != head:
        raise PreflightGateError(f'{item["id"]}: the code changed since the preflight')
    decision = record.get('routing')
    if not decision:
        raise PreflightGateError(f'{item["id"]}: preflight has no routing decision')
    try:
        routing.model_family(decision['model'])
    except routing.ForbiddenModel as error:
        raise PreflightGateError(str(error)) from error
    if decision.get('effort') not in routing.EFFORT_RANK:
        raise PreflightGateError(f'{item["id"]}: preflight effort is invalid')


def apply_outcome(plan, record, item):
    """Apply a corrected or split result. Returns (new_plan, change_entry).

    A correction flagged as a functional change is refused here rather than applied:
    product decisions belong to the owner. Raises prd.PlanError for that case and for
    corrections the plan rules reject, and the caller records the block.
    """
    task_id = item['id']
    if record.get('functional_change'):
        raise prd_module.PlanError(f'{task_id}: the correction changes required behavior and needs a product decision')
    if record['outcome'] == 'corrected':
        corrected = record['corrected_task']
        updated, changed = prd_module.apply_correction(plan, task_id, corrected, corrected.get('replaced_criteria'))
        if not changed:
            raise prd_module.PlanError(f'{task_id}: outcome "corrected" did not change the task')
        return updated, {'type': 'correction', 'fields': changed, 'reasons': record['corrections'],
                         'before': {name: item.get(name) for name in changed}}
    if record['outcome'] == 'split':
        updated, children = prd_module.apply_split(plan, task_id, record['split_tasks'])
        return updated, {'type': 'split', 'children': children, 'reasons': record['corrections']}
    raise ValueError(f'nothing to apply for outcome {record["outcome"]}')


def compact(record):
    """Return the persisted form of a result, dropping fields only useful during the session."""
    return {key: value for key, value in record.items() if key not in ('corrected_task', 'split_tasks')}
