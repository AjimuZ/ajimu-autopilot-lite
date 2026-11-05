"""Lite behavior: routing from the plan, per-task and final check stages, and the final fix loop."""
import json
import tempfile
import unittest
from pathlib import Path

from support import Sandbox, done_output
from autopilot_core import conventions, doctor, prd, project as project_module, routing, runner as runner_module
from autopilot_core.store import read_json, write_json_atomic


class PlanRoutingTests(unittest.TestCase):
    def story(self, **fields):
        return {'id': 'T-1', 'title': 'T', 'description': 'D', 'acceptanceCriteria': ['Works'], 'priority': 1,
                'passes': False, **fields}

    def test_task_type_selects_the_table_row(self):
        decision = routing.plan_decision(self.story(taskType='refactor_architectural'))
        self.assertEqual(('opus', 'high'), (decision.model, decision.effort))

    def test_a_story_without_type_routes_as_a_standard_feature(self):
        decision = routing.plan_decision(self.story())
        self.assertEqual(('sonnet', 'medium', 'feature_standard'), (decision.model, decision.effort, decision.task_type))
        self.assertIn('no taskType', decision.reason)

    def test_the_plan_can_raise_but_never_undercut_the_table(self):
        raised = routing.plan_decision(self.story(taskType='docs_simple', model='opus', effort='high'))
        self.assertEqual(('opus', 'high'), (raised.model, raised.effort))
        kept = routing.plan_decision(self.story(taskType='bug_complex', model='sonnet', effort='low'))
        self.assertEqual(('sonnet', 'high'), (kept.model, kept.effort))

    def test_haiku_and_unknown_values_are_refused(self):
        with self.assertRaises(routing.ForbiddenModel):
            routing.plan_decision(self.story(model='haiku'))
        with self.assertRaises(routing.RoutingError):
            routing.plan_decision(self.story(taskType='whatever'))
        with self.assertRaises(routing.RoutingError):
            routing.plan_decision(self.story(effort='extreme'))

    def test_opus_unavailable_needs_sonnet_sufficient(self):
        with self.assertRaises(routing.RoutingError):
            routing.plan_decision(self.story(taskType='integration_sensitive'), available_families=('sonnet',))
        decision = routing.plan_decision(self.story(taskType='integration_sensitive', sonnetSufficient=True),
                                         available_families=('sonnet',))
        self.assertEqual(('sonnet', 'high'), (decision.model, decision.effort))

    def test_plan_validation_names_routing_fields_of_the_wrong_type(self):
        plan = prd.new_plan('p', 'main')
        plan['userStories'] = [self.story(taskType=3, sonnetSufficient='yes')]
        problems = '; '.join(prd.validate(plan))
        self.assertIn('taskType must be a string', problems)
        self.assertIn('sonnetSufficient must be true or false', problems)

    def test_routing_problems_skip_finished_tasks(self):
        plan = prd.new_plan('p', 'main')
        plan['userStories'] = [self.story(model='haiku', passes=True), self.story(id='T-2', taskType='nope')]
        problems = runner_module.routing_problems(plan)
        self.assertEqual(1, len(problems))
        self.assertTrue(problems[0].startswith('T-2:'))


class ConfigurationTests(unittest.TestCase):
    def test_lite_defaults(self):
        config = project_module.DEFAULT_CONFIG
        self.assertEqual('plan', config['preflight']['mode'])
        self.assertFalse(config['review']['enabled'])
        self.assertEqual(0, config['verify']['final_every_tasks'])
        self.assertEqual([], project_module.validate_config(project_module.merge_defaults({}, config)))

    def test_detected_tests_are_deferred_to_the_end(self):
        with tempfile.TemporaryDirectory() as scratch:
            Path(scratch, 'package.json').write_text(json.dumps({'scripts': {
                'typecheck': 'tsc', 'lint': 'eslint .', 'build': 'vite build', 'test': 'vitest run'}}), encoding='utf-8')
            stages = {check['name']: check['when'] for check in project_module.detect_checks(scratch)}
        self.assertEqual({'typecheck': 'task', 'lint': 'task', 'build': 'task', 'test': 'final'}, stages)

    def test_stages_select_the_checks(self):
        checks = [{'name': 'build', 'command': 'b'}, {'name': 'test', 'command': 't', 'when': 'final'}]
        self.assertEqual(['build'], [c['name'] for c in project_module.checks_for(checks, 'task')])
        self.assertEqual(['build', 'test'], [c['name'] for c in project_module.checks_for(checks, 'final')])
        self.assertTrue(project_module.has_final_checks(checks))
        self.assertFalse(project_module.has_final_checks(checks[:1]))

    def test_bad_lite_settings_are_rejected(self):
        config = project_module.merge_defaults({}, project_module.DEFAULT_CONFIG)
        config['preflight']['mode'] = 'none'
        config['verify'].update(final_every_tasks=-1, fix_model='haiku', fix_effort='huge')
        config['checks'] = [{'name': 'test', 'command': 't', 'when': 'sometimes'}]
        problems = '; '.join(project_module.validate_config(config))
        for expected in ('preflight.mode', 'final_every_tasks', 'Sonnet floor', 'fix_effort', '"when"'):
            self.assertIn(expected, problems)


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class LiteRunTests(Sandbox):
    def state(self):
        return read_json(self.layout.state_path)

    def done(self, content='changed\n', subject='Add greeting output'):
        return {'edits': self.edit(content=content), 'structured': done_output(subject)}

    def argv_value(self, argv, flag):
        return argv[argv.index(flag) + 1]

    def test_a_task_costs_one_session_routed_from_the_plan(self):
        self.layout = self.make_repository([self.story(taskType='refactor_architectural')], lite=True)
        self.scenario(implementation=[self.done()])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        launches = self.launches()
        self.assertEqual(['implementation'], [entry['phase'] for entry in launches])
        argv = launches[0]['argv']
        self.assertEqual(('opus', 'high'), (self.argv_value(argv, '--model'), self.argv_value(argv, '--effort')))
        prompt = launches[0]['prompt']
        self.assertIn('Do not run the full test suite', prompt)
        self.assertIn('final.py', prompt)
        self.assertNotIn('final.py', prompt.split('Checks after this task:')[1].split('\n')[0])
        record = read_json(self.layout.record_path('T-1'))
        self.assertEqual('refactor_architectural', record['routing']['task_type'])
        self.assertIsNone(record.get('preflight'))
        final = self.state()['final']
        self.assertEqual(('passed', self.git('rev-parse', 'HEAD')), (final['status'], final['verified_commit']))
        self.assertIn('Add greeting output', self.remote_log())

    def test_the_slow_check_runs_once_at_the_end(self):
        self.layout = self.make_repository([self.story('T-1'), self.story('T-2', priority=2)], lite=True)
        self.scenario(implementation=[self.done('one\n', 'Add the first part'), self.done('two\n', 'Add the second part')])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        per_task = [line for line in self.lines if line.startswith('T-') and 'running' in line]
        self.assertEqual(['T-1: running build', 'T-2: running build'], per_task)
        self.assertEqual(1, sum('final verification: running build, test' in line for line in self.lines), self.lines)

    def test_a_final_failure_is_fixed_and_the_fix_published_on_its_own(self):
        self.layout = self.make_repository([self.story()], lite=True)
        self.scenario(implementation=[self.done('SLOWFAIL\n')],
                      final_fix=[{'edits': self.edit(content='repaired\n'), 'structured': done_output('Repair the slow path')}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual(['implementation', 'final_fix'], [entry['phase'] for entry in self.launches()])
        fix = self.launches('final_fix')[0]
        self.assertIn('check "test" failed', fix['prompt'])
        self.assertIn('T-1: Task T-1', fix['prompt'])
        self.assertEqual(('sonnet', 'high'), (self.argv_value(fix['argv'], '--model'), self.argv_value(fix['argv'], '--effort')))
        self.assertEqual(['Repair the slow path', 'Add greeting output'], self.remote_log()[:2])
        self.assertNotIn('Task:', self.git('log', '-1', '--format=%B'))
        self.assertEqual('repaired\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual('passed', self.state()['final']['status'])
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_a_final_failure_that_persists_halts_and_sets_the_fix_aside(self):
        self.layout = self.make_repository([self.story()], lite=True)
        attempt = {'edits': self.edit(content='SLOWFAIL again\n'), 'structured': done_output('Repair the slow path')}
        self.scenario(implementation=[self.done('SLOWFAIL\n')], final_fix=[attempt, attempt])
        worker = self.runner(supervised=True)
        self.assertEqual(runner_module.EXIT_BLOCKED, worker.run(), self.lines)
        self.assertEqual(2, len(self.launches('final_fix')))
        final = self.state()['final']
        self.assertEqual('failed', final['status'])
        self.assertTrue(final['preserved_commit'])
        self.assertEqual('', self.git('status', '--porcelain'))
        self.assertIn('final verification failed', self.lines[-1])
        self.assertIn('attention: the final verification still fails', self.lines[-1])
        self.assertTrue(self.plan_passes('T-1'), 'the task stays delivered')
        self.scenario(final_fix=[{'edits': self.edit(content='fine\n'), 'structured': done_output('Repair the slow path')}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual('passed', self.state()['final']['status'])

    def test_a_fix_session_that_gives_up_ends_the_verification(self):
        self.layout = self.make_repository([self.story()], lite=True)
        blocked = done_output(status='blocked', blocked_reason='the expected output is a product decision')
        self.scenario(implementation=[self.done('SLOWFAIL\n')], final_fix=[{'structured': blocked}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('final_fix')))
        self.assertIn('product decision', self.state()['final']['findings'][0])

    def test_final_every_tasks_verifies_between_tasks(self):
        self.layout = self.make_repository([self.story('T-1'), self.story('T-2', priority=2)], lite=True)
        config = self.layout.config()
        config['verify']['final_every_tasks'] = 1
        write_json_atomic(self.layout.config_path, config)
        self.scenario(implementation=[self.done('one\n', 'Add the first part'), self.done('two\n', 'Add the second part')])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        passed = [index for index, line in enumerate(self.lines) if line.startswith('final verification passed')]
        second = next(index for index, line in enumerate(self.lines) if line.startswith('iteration 2'))
        self.assertEqual(2, len(passed), self.lines)
        self.assertLess(passed[0], second)

    def test_an_unchanged_project_is_not_verified_twice(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)], lite=True)
        self.scenario(implementation=[self.done()])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner(only_task='T-1').run(), self.lines)
        self.assertFalse(any('final verification' in line for line in self.lines), 'a single-task run skips it')
        self.scenario(implementation=[{'structured': done_output(status='blocked', blocked_reason='needs a person')}])
        self.lines.clear()
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual(1, sum('final verification: running' in line for line in self.lines))
        self.lines.clear()
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertFalse(any('final verification: running' in line for line in self.lines), self.lines)

    def test_without_final_checks_there_is_no_final_stage(self):
        self.layout = self.make_repository([self.story()], lite=True)
        config = self.layout.config()
        for check in config['checks']:
            check['when'] = 'task'
        write_json_atomic(self.layout.config_path, config)
        self.scenario(implementation=[self.done()])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertIn('T-1: running build, test', self.lines)
        self.assertFalse(any('final verification' in line for line in self.lines))

    def test_an_interrupted_fix_owns_the_tree_and_is_verified_first(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)], lite=True)
        self.scenario(implementation=[self.done()])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner(only_task='T-1').run(), self.lines)
        state = self.state()
        state['final'].update(status='fixing', commit_message='Repair the slow path\n')
        write_json_atomic(self.layout.state_path, state)
        (self.project / 'app.txt').write_text('partial fix\n', encoding='utf-8')
        self.scenario(implementation=[self.done('two\n', 'Add the second part')])
        self.lines.clear()
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual(['Add the second part', 'Repair the slow path'], self.remote_log()[:2])

    def test_a_forbidden_plan_model_stops_before_any_session(self):
        self.layout = self.make_repository([self.story(model='haiku')], lite=True)
        self.assertEqual(runner_module.EXIT_USAGE, self.runner().run())
        self.assertIn('Sonnet floor', self.lines[-1])
        self.assertEqual([], self.launches())

    def test_opus_missing_blocks_only_that_task(self):
        self.layout = self.make_repository([self.story(taskType='integration_sensitive'),
                                            self.story('T-2', priority=2, dependsOn=[])], lite=True)
        write_json_atomic(self.layout.capabilities_path, {'families': ['sonnet']})
        self.scenario(implementation=[self.done()])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual('blocked', self.state()['tasks']['T-1']['status'])
        self.assertIn('model routing from the plan', self.state()['tasks']['T-1']['blocked_reason'])
        self.assertEqual('completed', self.state()['tasks']['T-2']['status'])

    def test_validate_reports_routing_and_stages(self):
        self.layout = self.make_repository([self.story(taskType='docs_simple'), self.story('T-2', priority=2)], lite=True)
        items = {item['name']: item for item in doctor.run(self.project).items}
        self.assertEqual('sonnet/low: 1, sonnet/medium: 1', items['task routing from the plan']['detail'])
        self.assertIn('T-2', items['tasks without taskType']['detail'])
        self.assertEqual('after each task: build; at the end: test', items['check stages']['detail'])

    def plan_passes(self, task_id):
        return prd.story(prd.load(self.layout.prd_path), task_id)['passes']


if __name__ == '__main__':
    unittest.main()
