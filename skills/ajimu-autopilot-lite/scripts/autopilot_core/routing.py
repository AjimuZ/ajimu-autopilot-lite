"""Model and effort selection; the only place that decides what a session may run on.

A decision comes either from the plan story's own routing fields (`plan_decision`, the
lite default) or from a preflight recommendation (`select`), through the same table.

Invariant: every value leaving this module belongs to ALLOWED_FAMILIES. Sonnet is
the floor of the whole system, so Haiku and unknown models raise instead of
degrading to a default.
"""
import re
from dataclasses import dataclass, field

ALLOWED_FAMILIES = ('sonnet', 'opus')
MODEL_RANK = {'sonnet': 0, 'opus': 1}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
EFFORT_RANK = {name: rank for rank, name in enumerate(EFFORTS)}
# Written to the child environment so background queries, which ignore --model, stay on Sonnet.
DEFAULT_SONNET_ID = 'claude-sonnet-5-5'

POLICY = {
    'docs_simple': ('sonnet', 'low'),
    'ui_simple': ('sonnet', 'medium'),
    'feature_standard': ('sonnet', 'medium'),
    'feature_multi_component': ('sonnet', 'high'),
    'bug_complex': ('sonnet', 'high'),
    'integration_sensitive': ('opus', 'high'),
    'refactor_architectural': ('opus', 'high'),
    'problem_very_complex': ('opus', 'xhigh'),
    'critical_hard_to_verify': ('opus', 'max'),
}
# The routing of a story whose plan entry names no type: the most common kind of change.
DEFAULT_TASK_TYPE = 'feature_standard'

_ALL_EFFORTS = frozenset(EFFORTS)
# Documented per model at code.claude.com/docs/en/model-config; a family and major
# version prefix keeps newer minor releases working without an update here. A version
# can end the ID or be followed by a date or a context suffix such as [1m].
_EFFORT_SUPPORT = (
    (re.compile(r'claude-(?:opus|sonnet)-(?:[5-9]|\d\d)(?:[-\[]|$)'), _ALL_EFFORTS),
    (re.compile(r'claude-(?:opus|sonnet)-4-[6-9](?:[-\[]|$)'), frozenset(('low', 'medium', 'high', 'max'))),
    (re.compile(r'claude-(?:opus|sonnet)-'), frozenset()),
)


class ForbiddenModel(ValueError):
    """The named model is below the Sonnet floor or outside the allowed families."""


class RoutingError(ValueError):
    """The task cannot run under the policy: the caller must block it."""


def model_family(name):
    """Return 'sonnet' or 'opus' for an alias or full model ID; raise ForbiddenModel otherwise."""
    if not isinstance(name, str) or not name.strip():
        raise ForbiddenModel(f'model name is empty or not a string: {name!r}')
    lowered = name.strip().lower()
    if 'haiku' in lowered:
        raise ForbiddenModel(f'{name} is below the Sonnet floor and is never allowed')
    for family in ALLOWED_FAMILIES:
        if re.search(rf'(?:^|[-_./:\[]){family}(?:$|[-_./:@\]\[])', lowered):
            return family
    raise ForbiddenModel(f'{name} is not an allowed model; allowed families: {", ".join(ALLOWED_FAMILIES)}')


def supported_efforts(model_id):
    """Return the effort levels of a resolved model ID, or None when the ID is not recognised.

    An empty set means the model accepts no effort parameter at all.
    """
    if not model_id or model_id.lower() in ALLOWED_FAMILIES:
        return None
    lowered = model_id.lower()
    for pattern, levels in _EFFORT_SUPPORT:
        if pattern.match(lowered):
            return levels
    return None


def compatible_effort(model_id, effort):
    """Return (effort_to_send, note). Lowers explicitly because the runtime would do it silently.

    `effort_to_send` is None when the model takes no effort, and `note` records any
    change so an execution record never claims a level that was not sent.
    """
    if effort not in EFFORT_RANK:
        raise RoutingError(f'unknown effort level: {effort!r}')
    levels = supported_efforts(model_id)
    if levels is None or effort in levels:
        return effort, None
    if not levels:
        return None, f'{model_id} takes no effort parameter; "{effort}" was not sent'
    for rank in range(EFFORT_RANK[effort], -1, -1):
        if EFFORTS[rank] in levels:
            return EFFORTS[rank], f'{model_id} does not support "{effort}"; lowered to "{EFFORTS[rank]}"'
    lowest = min(levels, key=EFFORT_RANK.get)
    return lowest, f'{model_id} does not support "{effort}"; raised to its lowest level "{lowest}"'


@dataclass
class Decision:
    """A routing outcome. `model` is a family alias passed verbatim to --model."""
    model: str
    effort: str
    task_type: str
    default_model: str
    default_effort: str
    reason: str
    adjustments: list = field(default_factory=list)

    def to_dict(self):
        return dict(self.__dict__)


def policy_table(overrides=None):
    """Return POLICY merged with validated overrides from project configuration."""
    table = dict(POLICY)
    for task_type, entry in (overrides or {}).items():
        model, effort = entry['model'], entry['effort']
        if model_family(model) != model or effort not in EFFORT_RANK:
            raise RoutingError(f'invalid routing override for {task_type}: {entry}')
        table[task_type] = (model, effort)
    return table


def select(task_type, recommended_model=None, recommended_effort=None, *, reason='',
           overrides=None, available_families=ALLOWED_FAMILIES, sonnet_sufficient=False):
    """Combine the policy default and the preflight recommendation into one Decision.

    The recommendation may only raise the default: a preflight can ask for more
    capability than the table but cannot silently undercut it. Raises
    ForbiddenModel for a recommendation below the floor and RoutingError when Opus
    is required, unavailable and Sonnet was not judged sufficient.
    """
    table = policy_table(overrides)
    if task_type not in table:
        raise RoutingError(f'unknown task type: {task_type!r}')
    default_model, default_effort = table[task_type]
    model, effort = default_model, default_effort
    adjustments = []
    if recommended_model is not None:
        family = model_family(recommended_model)
        if MODEL_RANK[family] > MODEL_RANK[model]:
            adjustments.append(f'preflight raised the model from {model} to {family}')
            model = family
    if recommended_effort is not None:
        if recommended_effort not in EFFORT_RANK:
            raise RoutingError(f'unknown effort level: {recommended_effort!r}')
        if EFFORT_RANK[recommended_effort] > EFFORT_RANK[effort]:
            adjustments.append(f'preflight raised the effort from {effort} to {recommended_effort}')
            effort = recommended_effort
    if model == 'opus' and 'opus' not in available_families:
        if not sonnet_sufficient or 'sonnet' not in available_families:
            raise RoutingError('Opus is required but unavailable, and Sonnet was not judged sufficient')
        adjustments.append('Opus is unavailable; Sonnet was judged sufficient by the preflight')
        model = 'sonnet'
        if EFFORT_RANK[effort] < EFFORT_RANK['high']:
            adjustments.append(f'effort raised from {effort} to high to compensate')
            effort = 'high'
    elif model not in available_families:
        raise RoutingError(f'{model} is not available in this installation')
    return Decision(model, effort, task_type, default_model, default_effort, reason, adjustments)


def plan_decision(item, *, overrides=None, available_families=ALLOWED_FAMILIES):
    """Return the Decision for a plan story from its own routing fields, with no session.

    The setup analysis records `taskType` and, optionally, `model`, `effort` and
    `sonnetSufficient` on each story. They go through `select`, so the plan can raise
    capability above the table but never undercut it, and the Sonnet floor holds. A story
    without `taskType` routes as DEFAULT_TASK_TYPE. Raises ForbiddenModel for a model
    below the floor and RoutingError for an unknown type or level, or for Opus required
    while unavailable without `sonnetSufficient`.
    """
    task_type = item.get('taskType') or DEFAULT_TASK_TYPE
    if not isinstance(task_type, str):
        raise RoutingError(f'taskType must be a string: {task_type!r}')
    sufficient = item.get('sonnetSufficient', False)
    if not isinstance(sufficient, bool):
        raise RoutingError(f'sonnetSufficient must be true or false: {sufficient!r}')
    origin = 'plan' if item.get('taskType') else f'plan default ({DEFAULT_TASK_TYPE}; the story has no taskType)'
    return select(task_type, item.get('model'), item.get('effort'), reason=f'{origin}: {task_type}',
                  overrides=overrides, available_families=available_families, sonnet_sufficient=sufficient)


def forbidden_in_usage(models):
    """Return the names in a session's reported usage that break the model policy."""
    forbidden = []
    for name in models:
        try:
            model_family(name)
        except ForbiddenModel:
            forbidden.append(name)
    return forbidden
