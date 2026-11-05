"""Model policy: Sonnet is the floor everywhere, and effort claims stay truthful."""
import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from autopilot_core import claude_cli, routing


class FamilyTests(unittest.TestCase):
    def test_sonnet_and_opus_aliases_and_ids_are_accepted(self):
        for name, family in (('sonnet', 'sonnet'), ('opus', 'opus'), ('claude-sonnet-5-5', 'sonnet'),
                             ('claude-opus-5-5', 'opus'), ('us.anthropic.claude-opus-4-6-v1:0', 'opus'),
                             ('claude-sonnet-4-6[1m]', 'sonnet')):
            with self.subTest(name=name):
                self.assertEqual(family, routing.model_family(name))

    def test_haiku_is_always_rejected(self):
        for name in ('haiku', 'HAIKU', 'claude-haiku-5-5', 'us.anthropic.claude-haiku-4-5-v1:0', 'claude-3-5-haiku-latest'):
            with self.subTest(name=name), self.assertRaises(routing.ForbiddenModel):
                routing.model_family(name)

    def test_unknown_and_empty_models_are_rejected_not_defaulted(self):
        for name in ('', None, 'fable', 'gpt-5', 'default', 'mysonnetfork', 'sonnetish'):
            with self.subTest(name=name), self.assertRaises(routing.ForbiddenModel):
                routing.model_family(name)


class SelectionTests(unittest.TestCase):
    def test_policy_table_matches_the_requirements(self):
        expected = {'docs_simple': ('sonnet', 'low'), 'ui_simple': ('sonnet', 'medium'),
                    'feature_standard': ('sonnet', 'medium'), 'feature_multi_component': ('sonnet', 'high'),
                    'bug_complex': ('sonnet', 'high'), 'integration_sensitive': ('opus', 'high'),
                    'refactor_architectural': ('opus', 'high'), 'problem_very_complex': ('opus', 'xhigh'),
                    'critical_hard_to_verify': ('opus', 'max')}
        self.assertEqual(expected, routing.POLICY)

    def test_selects_sonnet_and_opus_from_the_table(self):
        self.assertEqual(('sonnet', 'low'), (lambda d: (d.model, d.effort))(routing.select('docs_simple')))
        self.assertEqual(('opus', 'max'), (lambda d: (d.model, d.effort))(routing.select('critical_hard_to_verify')))

    def test_recommendation_can_raise_but_never_lower(self):
        raised = routing.select('feature_standard', 'opus', 'xhigh')
        self.assertEqual(('opus', 'xhigh'), (raised.model, raised.effort))
        self.assertEqual(2, len(raised.adjustments))
        kept = routing.select('integration_sensitive', 'sonnet', 'low')
        self.assertEqual(('opus', 'high'), (kept.model, kept.effort))

    def test_haiku_recommendation_is_refused(self):
        with self.assertRaises(routing.ForbiddenModel):
            routing.select('docs_simple', 'haiku', 'low')

    def test_override_below_the_floor_is_refused(self):
        with self.assertRaises(routing.ForbiddenModel):
            routing.select('docs_simple', overrides={'docs_simple': {'model': 'haiku', 'effort': 'low'}})

    def test_opus_unavailable_uses_sonnet_only_when_sufficient(self):
        decision = routing.select('refactor_architectural', available_families=('sonnet',), sonnet_sufficient=True)
        self.assertEqual('sonnet', decision.model)
        self.assertEqual('high', decision.effort)
        self.assertTrue(any('unavailable' in note for note in decision.adjustments))
        with self.assertRaises(routing.RoutingError):
            routing.select('refactor_architectural', available_families=('sonnet',), sonnet_sufficient=False)


class EffortTests(unittest.TestCase):
    def test_supported_levels_pass_unchanged(self):
        self.assertEqual(('xhigh', None), routing.compatible_effort('claude-sonnet-5-5', 'xhigh'))
        self.assertEqual(('max', None), routing.compatible_effort('claude-opus-5-5', 'max'))

    def test_unsupported_level_is_lowered_explicitly_and_recorded(self):
        effort, note = routing.compatible_effort('claude-sonnet-4-6', 'xhigh')
        self.assertEqual('high', effort)
        self.assertIn('lowered', note)

    def test_model_without_effort_support_sends_none_and_says_so(self):
        effort, note = routing.compatible_effort('claude-sonnet-4-5', 'high')
        self.assertIsNone(effort)
        self.assertIn('no effort', note)

    def test_unresolved_alias_keeps_the_request(self):
        self.assertEqual(('high', None), routing.compatible_effort(None, 'high'))

    def test_unknown_level_is_refused(self):
        with self.assertRaises(routing.RoutingError):
            routing.compatible_effort('claude-opus-5-5', 'ultra')

    def test_context_suffix_keeps_the_levels_of_the_model(self):
        self.assertEqual(('xhigh', None), routing.compatible_effort('claude-opus-5[1m]', 'xhigh'))
        self.assertEqual('high', routing.compatible_effort('claude-opus-4-6[1m]', 'xhigh')[0])


class CommandLineTests(unittest.TestCase):
    def request(self, **overrides):
        values = dict(phase='implementation', model='sonnet', effort='high', prompt='x', cwd='.', log_path='x.jsonl')
        values.update(overrides)
        return claude_cli.SessionRequest(**values)

    def test_model_and_effort_reach_the_command_line(self):
        argv = claude_cli.build_argv(['claude'], self.request(model='opus', effort='max'))
        self.assertEqual('opus', argv[argv.index('--model') + 1])
        self.assertEqual('max', argv[argv.index('--effort') + 1])

    def test_fallback_can_only_repeat_the_selected_model(self):
        argv = claude_cli.build_argv(['claude'], self.request(model='opus'))
        self.assertEqual('opus', argv[argv.index('--fallback-model') + 1])

    def test_haiku_cannot_produce_a_command_line(self):
        with self.assertRaises(routing.ForbiddenModel):
            claude_cli.build_argv(['claude'], self.request(model='haiku'))

    def test_permission_bypass_never_appears(self):
        joined = ' '.join(claude_cli.build_argv(['claude'], self.request()))
        self.assertNotIn('dangerously', joined)
        self.assertNotIn('bypassPermissions', joined)
        self.assertIn('dontAsk', joined)

    def test_environment_overrides_are_removed_and_background_model_pinned(self):
        env = claude_cli.child_environment(self.request(sonnet_id='claude-sonnet-5-5'), {
            'PATH': 'x', 'ANTHROPIC_MODEL': 'claude-haiku-5-5', 'ANTHROPIC_SMALL_FAST_MODEL': 'claude-haiku-5-5',
            'CLAUDE_CODE_EFFORT_LEVEL': 'low', 'CLAUDECODE': '1', 'CLAUDE_CODE_SESSION_ID': 'parent', 'CLAUDE_EFFORT': 'low',
            'CLAUDE_CODE_EXECPATH': 'x', 'AI_AGENT': 'x'})
        for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL', 'CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDECODE',
                     'CLAUDE_CODE_SESSION_ID', 'CLAUDE_EFFORT', 'CLAUDE_CODE_EXECPATH', 'AI_AGENT'):
            self.assertNotIn(name, env)
        self.assertEqual('claude-sonnet-5-5', env['ANTHROPIC_DEFAULT_HAIKU_MODEL'])
        self.assertEqual('sonnet', env['CLAUDE_CODE_SUBAGENT_MODEL'])
        self.assertEqual('1', env['CLAUDE_CODE_SUBAGENT_MODEL_FORCE'])

    def test_usage_report_detects_forbidden_models_and_fallbacks(self):
        result = claude_cli.SessionResult(actual_models=['claude-sonnet-5-5', 'claude-haiku-5-5'])
        self.assertEqual(['claude-haiku-5-5'], claude_cli.model_report(result, 'sonnet')['forbidden'])
        downgrade = claude_cli.model_report(claude_cli.SessionResult(actual_models=['claude-sonnet-5-5']), 'opus')
        self.assertEqual(['sonnet'], downgrade['fallback'])
        self.assertTrue(downgrade['downgraded'])
        silent = claude_cli.model_report(claude_cli.SessionResult(actual_models=['claude-opus-5-5']), 'sonnet')
        self.assertEqual(['opus'], silent['fallback'])
        self.assertFalse(silent['downgraded'])


class StreamTests(unittest.TestCase):
    def test_line_separators_inside_a_string_do_not_lose_the_result(self):
        event = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'a b\u0085c'}
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'stream.jsonl'
            path.write_text('not json\n' + json.dumps(event, ensure_ascii=False) + '\n', encoding='utf-8')
            result = claude_cli.parse_stream(path)
        self.assertTrue(result.has_result)
        self.assertEqual('a b\u0085c', result.result_text)

    def test_synthetic_entries_are_not_models_that_ran(self):
        event = {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': '',
                 'modelUsage': {'<synthetic>': {}, 'claude-sonnet-5-5': {'canonicalModel': 'claude-sonnet-5-5'}}}
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'stream.jsonl'
            path.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'model': '<synthetic>'}) + '\n'
                            + json.dumps(event) + '\n', encoding='utf-8')
            result = claude_cli.parse_stream(path)
        self.assertEqual(['claude-sonnet-5-5'], result.actual_models)
        self.assertEqual([], claude_cli.model_report(result, 'sonnet')['forbidden'])


if __name__ == '__main__':
    unittest.main()
