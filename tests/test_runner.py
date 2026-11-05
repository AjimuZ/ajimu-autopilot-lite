"""End-to-end runner behavior with scripted sessions, real Git and a local bare remote."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, SCRIPTS, Sandbox, done_output, ready_preflight, review_pass
from autopilot_core import conventions, gitops, prd, quality, runner as runner_module
from autopilot_core.store import LockHeld, RunnerLock, read_json, write_json_atomic


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class RunnerTests(Sandbox):
    def state(self):
        return read_json(self.layout.state_path)

    def plan(self):
        return prd.load(self.layout.prd_path)

    def record(self, task_id='T-1'):
        return read_json(self.layout.record_path(task_id))

    def happy(self, task_id='T-1', **implementation):
        return dict(preflight=[{'structured': ready_preflight(task_id)}],
                    implementation=[{'edits': self.edit(), 'structured': done_output(**implementation)}],
                    review=[{'structured': review_pass()}])

    # ---- the full path --------------------------------------------------------------

    def test_task_goes_from_pending_to_published(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_COMPLETE, code, self.lines)
        self.assertEqual(['preflight', 'implementation', 'review'], [e['phase'] for e in self.launches()])
        self.assertEqual('completed', self.state()['tasks']['T-1']['status'])
        self.assertTrue(self.plan()['userStories'][0]['passes'])
        self.assertIn('Add greeting output', self.remote_log())
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(head, self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual(head, self.state()['tasks']['T-1']['pushed_commit'])
        self.assertEqual('autopilot/roadmap', self.git('branch', '--show-current'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_commit_has_owner_identity_task_line_and_no_trailers(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        message = self.git('log', '-1', '--format=%B')
        self.assertIn('Task: T-1', message)
        self.assertNotIn('Co-Authored-By', message)
        self.assertEqual('Test Owner <owner@example.invalid>', self.git('log', '-1', '--format=%an <%ae>'))
        changed = self.git('show', '--name-only', '--format=', 'HEAD').splitlines()
        for path in ('app.txt', 'scripts/ralph/prd.json', 'scripts/ralph/progress.txt'):
            self.assertIn(path, changed)
        self.assertFalse([path for path in changed if '.ajimu-autopilot' in path])

    def test_each_session_is_a_new_process_with_selected_model_and_effort(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='refactor_architectural', effort='xhigh')}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run())
        launches = {entry['phase']: entry['argv'] for entry in self.launches()}
        argv = launches['implementation']
        self.assertEqual('opus', argv[argv.index('--model') + 1])
        self.assertEqual('xhigh', argv[argv.index('--effort') + 1])
        self.assertEqual('opus', argv[argv.index('--fallback-model') + 1])
        self.assertNotIn('--resume', argv)
        self.assertNotIn('--continue', argv)
        self.assertEqual('sonnet', launches['preflight'][launches['preflight'].index('--model') + 1])
        execution = self.record()['executions'][-1]
        self.assertEqual('opus', execution['selected_model'])
        self.assertEqual('xhigh', execution['selected_effort'])
        self.assertFalse(execution['effort_verifiable'])
        self.assertIsNone(execution['effective_effort'])
        self.assertEqual(['claude-opus-5-5'], execution['actual_models'])

    def test_conventions_reach_every_implementation_and_review_session(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        by_phase = {entry['phase']: entry['has_conventions'] for entry in self.launches()}
        self.assertEqual({'preflight': False, 'implementation': True, 'review': True}, by_phase)

    def test_missing_conventions_acknowledgement_fails_the_attempt(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'] = [{'edits': self.edit(), 'structured': done_output(conventions_ack='wrong')}]
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('did not confirm the conventions', entry['findings'][0])

    # ---- preflight outcomes ----------------------------------------------------------

    def test_no_implementation_without_a_passed_preflight(self):
        self.layout = self.make_repository([self.story()])
        invalid = {'structured': {'task_id': 'T-1'}}
        self.scenario(preflight=[invalid, invalid, invalid])
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_BLOCKED, code)
        self.assertEqual(3, len(self.launches('preflight')))
        self.assertEqual([], self.launches('implementation'))
        self.assertEqual('blocked', self.state()['tasks']['T-1']['status'])

    def test_haiku_recommendation_blocks_rather_than_runs(self):
        self.layout = self.make_repository([self.story()])
        forbidden = {'structured': ready_preflight('T-1', recommended_model='haiku')}
        self.scenario(preflight=[forbidden, forbidden, forbidden])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run())
        self.assertEqual([], self.launches('implementation'))
        for launch in self.launches():
            self.assertNotIn('haiku', launch['argv'][launch['argv'].index('--model') + 1])

    def test_corrected_task_is_reevaluated_before_implementation(self):
        self.layout = self.make_repository([self.story()])
        corrected = ready_preflight('T-1', outcome='corrected', corrected_task={'description': 'Precise description.'},
                                    corrections=[{'field': 'description', 'reason': 'ambiguous', 'evidence': 'doc'}])
        phases = self.happy()
        phases['preflight'] = [{'structured': corrected}, {'structured': ready_preflight('T-1')}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('preflight')))
        self.assertEqual('Precise description.', self.plan()['userStories'][0]['description'])
        record = self.record()
        self.assertEqual('Change the app.', record['original']['description'])
        self.assertEqual('Change the app.', record['corrections'][0]['before']['description'])
        self.assertEqual('ready', record['preflight']['outcome'])

    def test_split_replaces_the_task_and_children_get_their_own_preflight(self):
        self.layout = self.make_repository([self.story()])
        children = [{'title': f'Part {n}', 'description': 'Part.', 'acceptanceCriteria': ['Works']} for n in (1, 2)]
        split = ready_preflight('T-1', outcome='split', split_tasks=children,
                                corrections=[{'field': 'scope', 'reason': 'too large', 'evidence': 'many files'}])
        self.scenario(
            preflight=[{'structured': split}, {'structured': ready_preflight('T-1.1')}, {'structured': ready_preflight('T-1.2')}],
            implementation=[{'edits': self.edit(content='one\n'), 'structured': done_output('Add the first part')},
                            {'edits': self.edit(content='two\n'), 'structured': done_output('Add the second part')}],
            review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(['T-1.1', 'T-1.2'], [item['id'] for item in self.plan()['userStories']])
        self.assertEqual(['T-1.1', 'T-1.2'], self.record()['superseded_by'])
        self.assertIn('Split task T-1 into 2 tasks', self.remote_log())

    def test_already_implemented_needs_independent_verification(self):
        self.layout = self.make_repository([self.story()])
        evidence = [{'criterion': 'Works', 'met': True, 'evidence': 'app.txt:1'}]
        claim = ready_preflight('T-1', outcome='already_implemented', acceptance_evidence=evidence)
        self.scenario(preflight=[{'structured': claim}], review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual([], self.launches('implementation'))
        self.assertEqual(1, len(self.launches('review')))
        self.assertTrue(self.plan()['userStories'][0]['passes'])
        self.assertIn('Record T-1 as already implemented', self.remote_log())

    def test_rejected_already_implemented_claim_goes_back_to_preflight(self):
        self.layout = self.make_repository([self.story()])
        evidence = [{'criterion': 'Works', 'met': True, 'evidence': 'app.txt:1'}]
        claim = ready_preflight('T-1', outcome='already_implemented', acceptance_evidence=evidence)
        rejected = review_pass(verdict='fail', findings=['No such behavior exists'],
                               criteria=[{'criterion': 'Works', 'met': False, 'evidence': 'none'}])
        phases = self.happy()
        phases['preflight'] = [{'structured': claim}, {'structured': ready_preflight('T-1')}]
        phases['review'] = [{'structured': rejected}, {'structured': review_pass()}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('implementation')))
        self.assertEqual('failed', self.record()['executions'][0]['result'])

    def test_product_decision_is_recorded_as_a_block(self):
        self.layout = self.make_repository([self.story()])
        blocked = ready_preflight('T-1', outcome='blocked', functional_change=True,
                                  blocked_reason='Pricing rules need a product decision')
        self.scenario(preflight=[{'structured': blocked}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run())
        self.assertIn('Pricing rules', self.state()['tasks']['T-1']['blocked_reason'])
        self.assertIn('Record blocked task T-1', self.remote_log())

    # ---- failures ---------------------------------------------------------------------

    def test_failing_check_retries_with_findings_and_then_blocks(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        broken = {'edits': self.edit(content='BROKEN\n'), 'structured': done_output()}
        phases = dict(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[broken, broken, broken,
                                      {'edits': self.edit('other.txt', 'ok\n'), 'structured': done_output('Add other output')}],
                      review=[{'structured': review_pass()}])
        self.scenario(**phases)
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_BLOCKED, code, self.lines)
        self.assertEqual(4, len(self.launches('implementation')))
        state = self.state()['tasks']
        self.assertEqual('blocked', state['T-1']['status'])
        self.assertEqual('completed', state['T-2']['status'], 'independent tasks proceed')
        self.assertEqual(3, state['T-1']['attempts'])
        self.assertEqual(3, len(self.record()['executions']))

    def test_blocked_task_preserves_partial_work_without_committing_it(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'edits': {**self.edit(content='partial\n'), **self.edit('new.txt', 'new file\n')},
            'structured': done_output(status='blocked', blocked_reason='Needs a decision')}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertTrue(entry['preserved_commit'])
        self.assertEqual('base\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertIn('partial', self.git('show', f'{entry["preserved_commit"]}:app.txt'))
        self.assertIn('new file', self.git('show', f'{entry["preserved_commit"]}^3:new.txt'))
        self.assertIn('ajimu-autopilot', self.git('stash', 'list'))
        self.assertTrue(self.git('rev-parse', 'refs/ajimu-autopilot/preserved/T-1-attempt'))

    def test_success_claim_without_any_change_is_refused(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{'structured': done_output()}])
        self.runner(max_iterations=1).run()
        self.assertIn('changed no project files', self.state()['tasks']['T-1']['findings'][0])
        self.assertFalse(self.plan()['userStories'][0]['passes'])

    def test_rejected_commit_message_is_never_committed(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy(subject='Add export generated by a tool')
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('commit message rejected', entry['findings'][0])
        self.assertEqual([], [line for line in self.remote_log() if 'generated' in line])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_failing_commit_hook_is_respected_and_the_work_kept(self):
        self.layout = self.make_repository([self.story()])
        hook = self.project / '.git' / 'hooks' / 'pre-commit'
        # Only the task's commit is refused; the plan checkpoint before it has to get through.
        hook.write_text('#!/bin/sh\ngit diff --cached --name-only | grep -q app.txt && exit 1\nexit 0\n', encoding='utf-8')
        hook.chmod(0o755)
        self.scenario(**self.happy())
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('refused', entry['findings'][0])
        self.assertFalse(self.plan()['userStories'][0]['passes'])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual('Create the sample project', self.git('log', '-1', '--format=%s', 'main'))

    def test_secret_in_the_change_is_refused(self):
        self.layout = self.make_repository([self.story()])
        secret = 'token = "ghp_' + 'a' * 36 + '"\n'
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(content=secret), 'structured': done_output()}])
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertIn('possible GitHub token', entry['findings'][0])
        self.assertNotIn('ghp_', json.dumps(entry))

    def test_disabled_test_is_refused(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'edits': self.edit('tests/test_app.py', '@unittest.skip("flaky")\ndef test_a(): pass\n'),
            'structured': done_output()}])
        self.runner(max_iterations=1).run()
        self.assertIn('appears to be disabled', self.state()['tasks']['T-1']['findings'][0])

    def test_secret_scan_survives_file_names_diff_settings_and_lookalike_lines(self):
        self.layout = self.make_repository([self.story()])
        names = ('caffè.txt', 'with space.txt', 'plain.txt')
        for name in names:
            (self.project / name).write_text('base\n', encoding='utf-8')
        self.git('add', *names)
        self.git('commit', '--quiet', '-m', 'Add three files')
        secret = 'token = "ghp_' + 'a' * 36 + '"\n'
        (self.project / names[0]).write_text('base\n' + secret, encoding='utf-8')
        (self.project / names[1]).write_text('base\n' + secret, encoding='utf-8')
        (self.project / names[2]).write_text('base\n++ a line that looks like a header\n' + secret, encoding='utf-8')
        for setting, value in (('diff.noprefix', 'false'), ('diff.noprefix', 'true'), ('diff.mnemonicPrefix', 'true')):
            with self.subTest(setting=setting, value=value):
                self.git('config', setting, value)
                findings = ' | '.join(quality.scan_change(self.project, gitops.Git(self.project)))
                for name in names:
                    self.assertIn(f'{name}: possible GitHub token', findings)

    def test_neutral_observations_do_not_fail_a_passing_review(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['review'] = [{'structured': review_pass(findings=['The diff has no unrelated edits.'])}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('implementation')))

    def test_review_booleans_and_unmet_criteria_fail_even_with_a_pass_verdict(self):
        for name, review in (('docs', review_pass(documentation_ok=False)), ('security', review_pass(security_ok=False)),
                             ('criteria', review_pass(item_criteria=()))):
            with self.subTest(name):
                layout = self.make_repository([self.story()])
                self.layout = layout
                phases = self.happy()
                phases['review'] = [{'structured': review}]
                self.scenario(**phases)
                self.runner(max_iterations=1).run()
                self.assertEqual('ready', self.state()['tasks']['T-1']['status'])
                self.assertFalse(self.plan()['userStories'][0]['passes'])
                self.tearDownRepository()

    def tearDownRepository(self):
        import shutil
        shutil.rmtree(self.project, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))
        shutil.rmtree(self.remote, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))

    def test_failed_review_sends_findings_to_the_next_attempt(self):
        self.layout = self.make_repository([self.story()])
        failed = review_pass(verdict='fail', findings=['Criterion has no test'])
        phases = self.happy()
        phases['implementation'].append({'edits': self.edit(content='fixed\n'), 'structured': done_output()})
        phases['review'] = [{'structured': failed}, {'structured': review_pass()}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_runner_ticks_the_roadmap_only_after_its_checks_pass(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [ ] T-9 Add greeting\n- [ ] T-8 Other\n')
        failing = dict(preflight=[{'structured': ready_preflight('T-9')}],
                       implementation=[{'edits': self.edit(content='BROKEN\n'), 'structured': done_output()}])
        self.scenario(**failing)
        self.runner(max_iterations=1, only_task='T-9').run()
        self.assertIn('- [ ] T-9', (self.project / 'ROADMAP.md').read_text(encoding='utf-8'))
        self.scenario(implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass(item_criteria=prd.story(self.plan(), 'T-9')['acceptanceCriteria'])}])
        self.assertEqual(0, self.runner(only_task='T-9').run(), self.lines)
        roadmap = self.git('show', 'HEAD:ROADMAP.md')
        self.assertIn('- [x] T-9 Add greeting', roadmap)
        self.assertIn('- [ ] T-8 Other', roadmap)

    def test_missing_roadmap_line_asks_for_a_manual_update(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [ ] T-9 Add greeting\n')
        (self.project / 'ROADMAP.md').write_text('# Roadmap\n\nRewritten by hand.\n', encoding='utf-8')
        self.git('add', 'ROADMAP.md')
        self.git('commit', '--quiet', '-m', 'Rewrite the roadmap')
        self.scenario(preflight=[{'structured': ready_preflight('T-9')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}])
        self.runner(max_iterations=1).run()
        self.assertIn('was not found in ROADMAP.md', self.state()['tasks']['T-9']['findings'][0])

    def roadmap_task_scenario(self, task_id='T-9'):
        return dict(preflight=[{'structured': ready_preflight(task_id)}],
                    implementation=[{'edits': self.edit(), 'structured': done_output()}],
                    review=[{'structured': review_pass(item_criteria=prd.story(self.plan(), task_id)['acceptanceCriteria'])}])

    def test_todo_bullet_completes_on_the_first_attempt(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- TODO: T-9 Add greeting\n')
        self.scenario(**self.roadmap_task_scenario())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('implementation')))
        self.assertIn('- [x] T-9 Add greeting', self.git('show', 'HEAD:ROADMAP.md'))

    def test_reopened_task_with_a_ticked_roadmap_line_completes(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [x] T-9 Add greeting\n')
        plan = self.plan()
        plan['userStories'][0]['passes'] = False
        prd.save(self.layout.prd_path, plan)
        self.scenario(**self.roadmap_task_scenario())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('implementation')))

    def test_split_children_tick_the_shared_roadmap_line_last(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [ ] T-9 Big work\n')
        criteria = prd.story(self.plan(), 'T-9')['acceptanceCriteria']
        children = [{'title': f'Part {n}', 'description': 'Part.', 'acceptanceCriteria': ['Works']} for n in (1, 2)]
        split = ready_preflight('T-9', outcome='split', split_tasks=children,
                                corrections=[{'field': 'scope', 'reason': 'too large', 'evidence': 'many files'}])
        self.assertTrue(criteria)
        self.scenario(preflight=[{'structured': split}, {'structured': ready_preflight('T-9.1')}, {'structured': ready_preflight('T-9.2')}],
                      implementation=[{'edits': self.edit(content='one\n'), 'structured': done_output('Add the first part')},
                                      {'edits': self.edit(content='two\n'), 'structured': done_output('Add the second part')}],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertIn('[ ] T-9', self.git('show', 'HEAD~1:ROADMAP.md'))
        self.assertIn('[x] T-9', self.git('show', 'HEAD:ROADMAP.md'))

    def test_roadmap_tick_keeps_crlf_line_endings(self):
        self.layout = self.make_repository([self.story()])
        path = self.project / 'R.md'
        path.write_bytes(b'# R\r\n\r\n- [ ] A\r\n- [ ] B\r\n')
        self.assertTrue(prd.tick_roadmap(self.project, {'source': {'path': 'R.md', 'line': 'A'}}))
        self.assertEqual(b'# R\r\n\r\n- [x] A\r\n- [ ] B\r\n', path.read_bytes())
        self.assertFalse(prd.tick_roadmap(self.project, {'source': {'path': 'R.md', 'line': 'Z'}}))

    def test_block_record_is_not_mistaken_for_the_finished_task(self):
        self.layout = self.make_repository([self.story()])
        blocked = ready_preflight('T-1', outcome='blocked', blocked_reason='Needs a key')
        self.scenario(preflight=[{'structured': blocked}])
        self.runner().run()
        self.assertNotIn('Task: T-1', self.git('log', '-1', '--format=%B'))

    def test_session_editing_runner_files_is_undone(self):
        self.layout = self.make_repository([self.story()])
        forged = self.layout.prd_path.read_text(encoding='utf-8').replace('"passes": false', '"passes": true')
        phases = self.happy()
        phases['implementation'][0]['edits'] = {**self.edit(), str(self.layout.prd_path): forged}
        phases['implementation'][0]['structured'] = done_output(status='blocked', blocked_reason='stop here')
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        self.assertFalse(self.plan()['userStories'][0]['passes'])

    def test_session_editing_runner_files_is_undone_when_a_limit_ends_it(self):
        self.layout = self.make_repository([self.story()])
        forged = self.layout.prd_path.read_text(encoding='utf-8').replace('"passes": false', '"passes": true')
        phases = self.happy()
        phases['implementation'].insert(0, {**self.LIMITED, 'edits': {str(self.layout.prd_path): forged}})
        self.scenario(**phases)
        worker, _ = self.waiting_runner()
        self.assertEqual(runner_module.EXIT_COMPLETE, worker.run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')), 'the forged completion must not end the task')
        self.assertIn('Add greeting output', self.remote_log())

    def test_task_id_the_validator_refuses_blocks_before_spending(self):
        self.layout = self.make_repository([self.story('AI-3')])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual([], self.launches())
        self.assertIn('cannot appear in a commit message', self.state()['tasks']['AI-3']['blocked_reason'])
        self.assertIn('Record plan changes', self.remote_log())

    def test_session_cannot_commit_behind_the_runner(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'run': ['git', '-C', str(self.project), 'commit', '--allow-empty', '-m', 'Sneak a commit'],
            'structured': done_output()}])
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_ERROR, code)
        self.assertIn('only the runner', self.lines[-1])

    # ---- model enforcement after the session -----------------------------------------

    def test_reported_haiku_usage_halts_the_run(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'][0]['models'] = ['claude-sonnet-5-5', 'claude-haiku-5-5']
        self.scenario(**phases)
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_ERROR, code)
        self.assertIn('forbidden model', self.lines[-1])
        self.assertEqual([], self.launches('review'))
        self.assertFalse(self.plan()['userStories'][0]['passes'])

    def test_silent_opus_downgrade_fails_when_sonnet_is_not_sufficient(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='refactor_architectural', sonnet_sufficient=False)}]
        phases['implementation'][0]['models'] = ['claude-sonnet-5-5']
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('opus was selected', entry['findings'][0])
        self.assertEqual(['sonnet'], self.record()['executions'][-1]['fallback'])

    def test_recorded_fallback_is_accepted_when_sonnet_is_sufficient(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='refactor_architectural', sonnet_sufficient=True)}]
        phases['implementation'][0]['models'] = ['claude-sonnet-5-5']
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(['sonnet'], self.record()['executions'][-1]['fallback'])

    def test_opus_probed_unavailable_routes_to_sonnet_or_blocks(self):
        self.layout = self.make_repository([self.story()])
        write_json_atomic(self.layout.capabilities_path, {'families': ['sonnet'], 'resolved': {'sonnet': 'claude-sonnet-5-5'}})
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='problem_very_complex', sonnet_sufficient=False)}]
        self.scenario(**phases)
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertIn('Opus is required', self.state()['tasks']['T-1']['blocked_reason'])
        self.assertEqual([], self.launches('implementation'))

    def test_inherited_overrides_do_not_reach_sessions(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        os.environ.update(ANTHROPIC_MODEL='claude-haiku-5-5', CLAUDE_CODE_EFFORT_LEVEL='low', CLAUDECODE='1')
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in ('ANTHROPIC_MODEL', 'CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDECODE')])
        self.assertEqual(0, self.runner().run(), self.lines)
        for launch in self.launches():
            for name in ('ANTHROPIC_MODEL', 'CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDECODE'):
                self.assertNotIn(name, launch['env'])
            self.assertEqual('claude-sonnet-5-5', launch['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'])

    # ---- publication ------------------------------------------------------------------

    def test_failed_push_keeps_the_commit_and_stops_new_work(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(**self.happy())
        hook = self.remote / 'hooks' / 'pre-receive'
        worker = self.runner()
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap')
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        code = worker.run()
        self.assertEqual(runner_module.EXIT_PAUSED, code, self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertEqual('pushing', entry['status'])
        self.assertTrue(self.plan()['userStories'][0]['passes'], 'the commit exists locally')
        self.assertEqual(entry['commit'], self.git('rev-parse', 'HEAD'))
        self.assertNotEqual(entry['commit'], self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual(1, len(self.launches('implementation')), 'T-2 must not start')

        hook.unlink()
        self.scenario(preflight=[{'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': self.edit('two.txt', 'x\n'), 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        messages = self.git('log', '--format=%s').splitlines()
        self.assertEqual(1, messages.count('Add greeting output'), 'no duplicate commit')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual(entry['commit'], self.state()['tasks']['T-1']['pushed_commit'])

    def test_push_refused_again_on_restart_stays_resumable(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap')
        hook = self.remote / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run(), self.lines)
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run(), self.lines)
        self.assertIn('resume', self.lines[-1])
        self.assertEqual('pushing', self.state()['tasks']['T-1']['status'])
        hook.unlink()
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('completed', self.state()['tasks']['T-1']['status'])

    def test_remote_lost_after_the_commit_pauses_and_resume_publishes(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'][0]['run'] = ['git', '-C', str(self.project), 'remote', 'set-url', 'origin', str(self.tmp / 'gone.git')]
        self.scenario(**phases)
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run(), self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertEqual('pushing', entry['status'])
        self.assertTrue(entry['push_error'])
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run(), self.lines)
        self.assertIn('resume', self.lines[-1])
        self.git('remote', 'set-url', 'origin', str(self.remote))
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))

    def test_rewritten_remote_history_is_reported_as_diverged(self):
        self.layout = self.make_repository([self.story()])
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap')
        other = self.tmp / 'other'
        self.git('clone', '--quiet', '-b', 'autopilot/roadmap', str(self.remote), str(other), cwd=self.tmp)
        self.git('config', 'user.name', 'Other', cwd=other)
        self.git('config', 'user.email', 'other@example.invalid', cwd=other)
        self.git('commit', '--quiet', '--amend', '-m', 'Rewrite the plan commit', cwd=other)
        self.git('push', '--quiet', '--force', 'origin', 'autopilot/roadmap', cwd=other)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('diverged', self.lines[-1])

    def test_project_below_the_repository_root_is_refused(self):
        from autopilot_core import project as project_module
        self.layout = self.make_repository([self.story()])
        nested = self.project / 'sub'
        nested.mkdir()
        report = project_module.setup(nested)
        self.assertTrue([warning for warning in report['warnings'] if 'root of its Git repository' in warning], report)
        write_json_atomic(nested / '.ajimu-autopilot/config.json', self.layout.config())
        worker = runner_module.Runner(nested, output=self.lines.append)
        self.assertEqual(runner_module.EXIT_ERROR, worker.run())
        self.assertIn('root of its Git repository', self.lines[-1])
        self.assertEqual([], self.launches())

    def test_no_force_push_when_the_remote_diverged(self):
        self.layout = self.make_repository([self.story()])
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap')
        other = self.tmp / 'other'
        self.git('clone', '--quiet', '-b', 'autopilot/roadmap', str(self.remote), str(other), cwd=self.tmp)
        self.git('config', 'user.name', 'Other', cwd=other)
        self.git('config', 'user.email', 'other@example.invalid', cwd=other)
        (other / 'other.txt').write_text('x', encoding='utf-8')
        self.git('add', '--all', cwd=other)
        self.git('commit', '--quiet', '-m', 'Work elsewhere', cwd=other)
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap', cwd=other)
        (self.project / 'local.txt').write_text('x', encoding='utf-8')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Work here')
        remote_before = self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('diverged', self.lines[-1])
        self.assertEqual(remote_before, self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual([], self.launches())

    def test_protected_branch_is_refused(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['git']['branch'] = 'main'
        config['git']['protected_branches'] = ['main']
        write_json_atomic(self.layout.config_path, config)
        plan = self.plan()
        plan['branchName'] = 'main'
        prd.save(self.layout.prd_path, plan)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('protected', self.lines[-1])
        self.assertEqual([], self.launches())

    def test_another_configured_branch_leaves_main_untouched(self):
        self.layout = self.make_repository([self.story()])
        before = self.git('rev-parse', 'main', cwd=self.remote)
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        self.assertEqual(before, self.git('rev-parse', 'main', cwd=self.remote))
        self.assertEqual(before, self.git('rev-parse', 'main'))

    def test_uncommitted_user_work_stops_the_run_untouched(self):
        self.layout = self.make_repository([self.story()])
        (self.project / 'app.txt').write_text('my own edit\n', encoding='utf-8')
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('uncommitted changes', self.lines[-1])
        self.assertEqual('my own edit\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual([], self.launches())

    # ---- limits and recovery ---------------------------------------------------------

    def test_iteration_limit_is_enforced_and_resume_continues(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()},
                                      {'edits': self.edit('b.txt', 'b\n'), 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner(max_iterations=1).run())
        self.assertEqual(['completed', 'pending'], [self.state()['tasks'][t]['status'] for t in ('T-1', 'T-2')])
        self.assertEqual(0, self.runner(max_iterations=5).run(), self.lines)

    def test_iteration_limit_applies_to_each_run_so_resume_continues(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()},
                                      {'edits': self.edit('b.txt', 'b\n'), 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner(max_iterations=1).run())
        self.assertEqual(0, self.runner(max_iterations=1).run(), self.lines)
        self.assertEqual(1, self.state()['iteration'])

    def test_unknown_task_is_refused_rather_than_reported_as_processed(self):
        self.layout = self.make_repository([self.story()])
        self.assertEqual(runner_module.EXIT_USAGE, self.runner(only_task='NOPE').run())
        self.assertIn('unknown task: NOPE', self.lines[-1])
        self.assertEqual([], self.launches())

    def test_stop_during_a_check_ends_it_and_refunds_the_attempt(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        stop = "import pathlib,time; pathlib.Path('.ajimu-autopilot/runtime/STOP').touch(); time.sleep(40)"
        config['checks'] = [{'name': 'slow', 'command': f'"{sys.executable}" -c "{stop}"', 'required': True}]
        write_json_atomic(self.layout.config_path, config)
        self.scenario(**self.happy())
        started = time.monotonic()
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run(), self.lines)
        self.assertLess(time.monotonic() - started, 35, 'the stop must not wait for the check')
        entry = self.state()['tasks']['T-1']
        self.assertEqual(('ready', 0, True), (entry['status'], entry['attempts'], entry['interrupted']))
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_log_that_cannot_be_deleted_does_not_end_the_run(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['limits']['keep_session_logs'] = 1
        write_json_atomic(self.layout.config_path, config)
        self.scenario(**self.happy())
        real = Path.unlink

        def held(path, *args, **kwargs):
            if path.suffix == '.jsonl':
                raise PermissionError('held by another process')
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', held):
            self.assertEqual(0, self.runner().run(), self.lines)

    def test_default_iteration_limit_is_thirty(self):
        self.layout = self.make_repository([self.story()])
        self.assertEqual(30, self.layout.config()['limits']['max_iterations'])

    def test_stop_request_halts_a_running_session_and_refunds_the_attempt(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'] = [{'sleep': 30, 'structured': done_output()}]
        self.scenario(**phases)
        worker = self.runner()
        original = worker.base_session

        def stop_soon(phase, *args, **kwargs):
            if phase == 'implementation':
                runner_module.request_stop(self.project)
            return original(phase, *args, **kwargs)
        worker.base_session = stop_soon
        self.assertEqual(runner_module.EXIT_PAUSED, worker.run(), self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertEqual(('ready', 0), (entry['status'], entry['attempts']))
        self.assertTrue(entry['interrupted'])
        self.assertFalse(self.layout.lock_path.exists())

    def test_session_timeout_counts_as_a_failed_attempt(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['limits']['session_timeout_minutes'] = 1
        write_json_atomic(self.layout.config_path, config)
        phases = self.happy()
        phases['implementation'] = [{'sleep': 90, 'structured': done_output()}]
        self.scenario(**phases)
        worker = self.runner(max_iterations=1)
        original = worker.base_session

        def shorten(*args, **kwargs):
            real = runner_module.claude_cli.SessionRequest
            def request(**values):
                values['timeout_seconds'] = 2 if values['phase'] == 'implementation' else values['timeout_seconds']
                return real(**values)
            runner_module.claude_cli.SessionRequest = request
            try:
                return original(*args, **kwargs)
            finally:
                runner_module.claude_cli.SessionRequest = real
        worker.base_session = shorten
        worker.run()
        self.assertIn('timed out', self.state()['tasks']['T-1']['findings'][0])

    def test_child_outliving_a_session_does_not_lose_its_result(self):
        from autopilot_core import claude_cli
        self.layout = self.make_repository([self.story()])
        self.scenario(probe=[{'linger': 4, 'structured': {'ack': 'ok'}}])
        request = claude_cli.SessionRequest(phase='probe', model='sonnet', effort='low', prompt='Return ack.', cwd=str(self.project),
                                            log_path=str(self.tmp / 'probe.jsonl'), system_append='rules', timeout_seconds=30)
        result = claude_cli.run_session(self.layout.config()['claude']['command'], request)
        self.assertEqual({'ack': 'ok'}, result.structured)
        # The child holds files in this test's folder, which Windows could not delete sooner.
        time.sleep(5)

    def waiting_runner(self, on_sleep=None, **options):
        """A runner whose waits advance a fake clock; returns it and the list of naps taken."""
        now, naps = [time.time()], []

        def sleep(seconds):
            naps.append(seconds)
            now[0] += seconds
            if on_sleep:
                on_sleep()
        return self.runner(clock=lambda: now[0], sleep=sleep, **options), naps

    LIMITED = {'rate_limited': True, 'is_error': True, 'api_error_status': 429}

    def test_usage_limit_waits_for_the_reset_and_continues_the_work(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'].insert(0, {**self.LIMITED, 'resets_at': int(time.time()) + 3600,
                                            'edits': self.edit(content='partial\n')})
        self.scenario(**phases)
        worker, naps = self.waiting_runner(max_iterations=1)
        self.assertEqual(runner_module.EXIT_COMPLETE, worker.run(), self.lines)
        self.assertGreater(sum(naps), 3600)
        self.assertLess(sum(naps), 3700)
        self.assertTrue(any('waiting until' in line for line in self.lines), self.lines)
        launches = self.launches('implementation')
        self.assertEqual(2, len(launches))
        self.assertIn('A previous attempt was interrupted', launches[1]['prompt'])
        state = self.state()
        self.assertEqual((1, 1), (state['tasks']['T-1']['attempts'], state['iteration']), 'no attempt or iteration charged')
        self.assertEqual('complete', state['run']['status'])
        self.assertGreater(worker.deadline - time.monotonic(), 720 * 60 + 3000, 'the wait is not charged to the global timeout')

    def test_usage_limit_without_a_future_reset_retries_periodically(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [self.LIMITED, {**self.LIMITED, 'resets_at': int(time.time()) - 60}] + phases['preflight']
        self.scenario(**phases)
        worker, naps = self.waiting_runner(max_iterations=1)
        self.assertEqual(runner_module.EXIT_COMPLETE, worker.run(), self.lines)
        self.assertAlmostEqual(600, sum(naps), delta=1)
        self.assertEqual(3, len(self.launches('preflight')))

    def test_usage_limit_during_review_keeps_the_work_and_the_preflight(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['review'].insert(0, self.LIMITED)
        phases['implementation'].append({'structured': done_output()})
        self.scenario(**phases)
        worker, _ = self.waiting_runner()
        self.assertEqual(runner_module.EXIT_COMPLETE, worker.run(), self.lines)
        self.assertEqual(1, len(self.launches('preflight')))
        self.assertEqual(2, len(self.launches('implementation')))
        self.assertEqual(1, self.state()['tasks']['T-1']['attempts'])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_usage_limit_named_only_in_the_result_text_waits(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'].insert(0, {'is_error': True, 'text': "You've hit your session limit, resets 3pm"})
        self.scenario(**phases)
        worker, naps = self.waiting_runner()
        self.assertEqual(runner_module.EXIT_COMPLETE, worker.run(), self.lines)
        self.assertTrue(naps)
        self.assertEqual(1, self.state()['tasks']['T-1']['attempts'])

    def test_rejected_event_on_a_finished_session_is_not_a_limit(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'][0]['rate_limited'] = True
        self.scenario(**phases)
        worker, naps = self.waiting_runner()
        self.assertEqual(runner_module.EXIT_COMPLETE, worker.run(), self.lines)
        self.assertEqual([], naps)
        self.assertEqual(1, len(self.launches('implementation')))

    def test_stop_during_a_usage_wait_pauses_and_resume_continues(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'].insert(0, {**self.LIMITED, 'edits': self.edit(content='partial\n')})
        self.scenario(**phases)
        seen = []

        def stop():
            seen.append(self.state()['run']['status'])
            runner_module.request_stop(self.project)
        worker, _ = self.waiting_runner(on_sleep=stop)
        self.assertEqual(runner_module.EXIT_PAUSED, worker.run(), self.lines)
        self.assertEqual(['waiting'], seen)
        self.assertEqual('paused', self.state()['run']['status'])
        self.assertFalse(self.layout.lock_path.exists())
        self.assertEqual('partial\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        runner_module.clear_stop(self.project)
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_pause_request_ends_a_usage_wait_because_nothing_is_in_flight(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'].insert(0, {**self.LIMITED, 'edits': self.edit(content='partial\n')})
        self.scenario(**phases)
        worker, _ = self.waiting_runner(on_sleep=lambda: runner_module.request_pause(self.project))
        self.assertEqual(runner_module.EXIT_PAUSED, worker.run(), self.lines)
        self.assertEqual('partial\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_interrupt_during_a_usage_wait_pauses_and_resume_continues(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'].insert(0, {**self.LIMITED, 'edits': self.edit(content='partial\n')})
        self.scenario(**phases)

        def interrupt():
            raise KeyboardInterrupt
        worker, _ = self.waiting_runner(on_sleep=interrupt)
        self.assertEqual(runner_module.EXIT_PAUSED, worker.run(), self.lines)
        self.assertEqual('paused', self.state()['run']['status'])
        self.assertIn('resume', self.lines[-1])
        self.assertFalse(self.layout.lock_path.exists())
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_second_runner_is_refused(self):
        self.layout = self.make_repository([self.story()])
        with RunnerLock(self.layout.lock_path):
            with self.assertRaises(LockHeld):
                self.runner().run()
        self.assertEqual([], self.launches())

    def test_lock_of_a_dead_process_is_reclaimed(self):
        self.layout = self.make_repository([self.story()])
        process = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        import socket
        write_json_atomic(self.layout.lock_path, {'pid': int(process.stdout), 'host': socket.gethostname(), 'started_at': 'x'})
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)

    def _crash_after(self, method):
        """Run a real attempt that dies right after `method`, as a killed process would."""
        worker = self.runner()
        original = getattr(worker, method)

        class Crash(BaseException):
            pass

        def crash(*args, **kwargs):
            result = original(*args, **kwargs)
            raise Crash()
        setattr(worker, method, crash)
        with self.assertRaises(Crash):
            worker.run()
        self.layout.lock_path.unlink(missing_ok=True)

    def test_crash_during_implementation_resumes_with_the_work_in_place(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        self.scenario(**phases)
        worker = self.runner()
        original = worker.base_session

        class Crash(BaseException):
            pass

        def crash(phase, *args, **kwargs):
            result = original(phase, *args, **kwargs)
            if phase == 'implementation':
                raise Crash()
            return result
        worker.base_session = crash
        with self.assertRaises(Crash):
            worker.run()
        self.layout.lock_path.unlink(missing_ok=True)
        self.assertEqual('running', self.state()['tasks']['T-1']['status'])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.scenario(implementation=[{'structured': done_output()}], review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, self.git('log', '--format=%s').splitlines().count('Add greeting output'))
        self.assertEqual(1, len(self.launches('preflight')), 'the valid preflight is reused')

    def test_crash_during_a_new_preflight_keeps_partial_work_resumable(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(content='BROKEN\n'), 'structured': done_output()}])
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner(max_iterations=1).run(), self.lines)
        plan = self.plan()
        plan['userStories'][0]['description'] = 'Change the app differently.'
        prd.save(self.layout.prd_path, plan)
        worker = self.runner()
        original = worker.base_session

        class Crash(BaseException):
            pass

        def crash(phase, *args, **kwargs):
            if phase == 'preflight':
                raise Crash()
            return original(phase, *args, **kwargs)
        worker.base_session = crash
        with self.assertRaises(Crash):
            worker.run()
        self.layout.lock_path.unlink(missing_ok=True)
        self.assertEqual('preflight', self.state()['tasks']['T-1']['status'])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(content='fixed\n'), 'structured': done_output()}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, self.state()['tasks']['T-1']['attempts'], 'the earlier attempt still counts')

    def test_block_clears_the_interrupted_mark_with_the_work_set_aside(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'].insert(0, {**self.LIMITED, 'edits': self.edit(content='partial\n')})
        self.scenario(**phases)
        worker, _ = self.waiting_runner(on_sleep=lambda: runner_module.request_stop(self.project))
        self.assertEqual(runner_module.EXIT_PAUSED, worker.run(), self.lines)
        self.assertTrue(self.state()['tasks']['T-1']['interrupted'])
        runner_module.clear_stop(self.project)
        plan = self.plan()
        plan['userStories'][0]['description'] = 'Change the app differently.'
        prd.save(self.layout.prd_path, plan)
        self.scenario(preflight=[{'structured': ready_preflight('T-1', outcome='blocked', blocked_reason='Needs a decision')}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertFalse(self.state()['tasks']['T-1']['interrupted'])
        self.assertEqual('base\n', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_crash_after_commit_before_push_does_not_duplicate(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self._crash_after('commit_task')
        entry = self.state()['tasks']['T-1']
        self.assertEqual('pushing', entry['status'])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, self.git('log', '--format=%s').splitlines().count('Add greeting output'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))

    def test_crash_between_commit_and_state_write_adopts_the_commit(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        worker = self.runner()
        real_commit = gitops.Git.commit

        class Crash(BaseException):
            pass

        def commit_then_die(git, message_file):
            real_commit(git, message_file)
            if 'Task: T-1' in Path(message_file).read_text(encoding='utf-8'):
                raise Crash()
            return git.head()
        gitops.Git.commit = commit_then_die
        self.addCleanup(setattr, gitops.Git, 'commit', real_commit)
        with self.assertRaises(Crash):
            worker.run()
        gitops.Git.commit = real_commit
        self.layout.lock_path.unlink(missing_ok=True)
        self.assertEqual('committing', self.state()['tasks']['T-1']['status'])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, self.git('log', '--format=%s').splitlines().count('Add greeting output'))

    def test_lost_runtime_state_cannot_repeat_published_work(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        launches = len(self.launches())
        self.layout.state_path.unlink()
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(launches, len(self.launches()))

    def test_metadata_folder_never_reaches_git(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('', self.git('ls-files', '.ajimu-autopilot'))
        self.assertEqual('', self.git('ls-tree', '-r', '--name-only', 'autopilot/roadmap', '.ajimu-autopilot', cwd=self.remote))
        self.assertIn('.ajimu-autopilot/', self.git('show', 'autopilot/roadmap:.gitignore', cwd=self.remote).splitlines())
        self.assertIn('Ignore Autopilot metadata', self.remote_log())
        self.assertTrue(self.layout.record_path('T-1').is_file(), 'the record stays on disk')
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_metadata_tracked_by_an_earlier_release_is_untracked_and_kept(self):
        self.layout = self.make_repository([self.story()], branch=None)
        (self.project / '.gitignore').unlink()
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan as an earlier release did')
        self.git('push', '--quiet', 'origin', 'main')
        self.assertTrue(self.git('ls-files', '.ajimu-autopilot'))
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('', self.git('ls-files', '.ajimu-autopilot'))
        self.assertTrue(self.layout.config_path.is_file(), 'local configuration survives')
        self.assertEqual('', self.git('ls-tree', '-r', '--name-only', 'main', '.ajimu-autopilot', cwd=self.remote))
        self.assertIn('Stop tracking Autopilot metadata', self.remote_log('main'))

    def test_hand_written_ignore_rule_is_respected_and_not_duplicated(self):
        self.layout = self.make_repository([self.story()])
        (self.project / '.gitignore').write_bytes(b'build/\n.ajimu-autopilot/*\n')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Ignore the folder by hand')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('build/\n.ajimu-autopilot/*\n', (self.project / '.gitignore').read_text(encoding='utf-8'))
        self.assertEqual('', self.git('ls-files', '.ajimu-autopilot'))

    def test_owner_edit_to_gitignore_stops_the_run_uncommitted(self):
        self.layout = self.make_repository([self.story()])
        with open(self.project / '.gitignore', 'a', encoding='utf-8') as handle:
            handle.write('my-own-rule/\n')
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('uncommitted changes', self.lines[-1])
        self.assertIn('.gitignore', self.lines[-1])
        self.assertEqual([], self.launches())

    def test_ignore_rule_keeps_existing_content_and_line_endings(self):
        from autopilot_core import project as project_module
        self.layout = self.make_repository([self.story()])
        path = self.project / '.gitignore'
        path.write_bytes(b'build/\r\nno-newline')
        self.assertTrue(project_module.Project(self.project).ensure_runtime())
        self.assertEqual(b'build/\r\nno-newline\r\n.ajimu-autopilot/\r\n', path.read_bytes())
        self.assertFalse(project_module.Project(self.project).ensure_runtime())

    def test_fresh_clone_never_commits_runtime_files(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner(max_iterations=1).run(), self.lines)
        clone = self.tmp / 'clone'
        self.git('clone', '--quiet', '-b', 'autopilot/roadmap', str(self.remote), str(clone), cwd=self.tmp)
        self.git('config', 'user.name', 'Test Owner', cwd=clone)
        self.git('config', 'user.email', 'owner@example.invalid', cwd=clone)
        self.assertFalse((clone / '.ajimu-autopilot').exists(), 'the folder does not travel through Git')
        # A clone is configured again by setup; plan and completion come from prd.json.
        from autopilot_core import project as project_module
        project_module.setup(clone, branch='autopilot/roadmap')
        write_json_atomic(clone / '.ajimu-autopilot/config.json', self.layout.config())
        self.scenario(preflight=[{'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': {str(clone / 'two.txt'): 'x\n'}, 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}])
        worker = runner_module.Runner(clone, output=self.lines.append)
        self.assertEqual(0, worker.run(), self.lines)
        tracked = self.git('ls-files', cwd=clone)
        self.assertNotIn('.ajimu-autopilot', tracked)
        self.assertEqual(1, self.git('log', '--format=%s', cwd=clone).splitlines().count('Add greeting output'))
        self.assertEqual(2, len(self.launches('implementation')), 'the finished task was not repeated')

    def test_task_reopened_by_hand_runs_again(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        plan = self.plan()
        plan['userStories'][0]['passes'] = False
        prd.save(self.layout.prd_path, plan)
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Reopen the task')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(content='again\n'), 'structured': done_output('Change the app again')}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_budget_settings_of_earlier_releases_limit_nothing(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        config = self.layout.config()
        config['limits'].update(max_budget_usd_per_session=0.001, max_budget_usd_total=0.001)
        write_json_atomic(self.layout.config_path, config)
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()},
                                      {'edits': self.edit('b.txt', 'b\n'), 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertEqual(6, len(self.launches()))
        for launch in self.launches():
            self.assertNotIn('--max-budget-usd', launch['argv'])

    def test_setup_removes_the_budget_settings_of_earlier_releases(self):
        from autopilot_core import project as project_module
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['limits'].update(max_budget_usd_per_session=25, max_budget_usd_total=150, max_iterations=7)
        write_json_atomic(self.layout.config_path, config)
        project_module.setup(self.project)
        stored = read_json(self.layout.config_path)['limits']
        self.assertNotIn('max_budget_usd_per_session', stored)
        self.assertNotIn('max_budget_usd_total', stored)
        self.assertEqual(7, stored['max_iterations'])

    def test_limit_crossed_by_the_last_task_still_reports_completion(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner(max_iterations=1).run(), self.lines)
        self.assertEqual('complete', self.state()['run']['status'])

    def test_unblock_requeues_the_task(self):
        self.layout = self.make_repository([self.story()])
        blocked = ready_preflight('T-1', outcome='blocked', blocked_reason='Needs a key')
        self.scenario(preflight=[{'structured': blocked}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run())
        runner_module.unblock(self.project, 'T-1')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('completed', self.state()['tasks']['T-1']['status'])


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class DefaultBranchTests(Sandbox):
    """The default working branch is main, committed and pushed to directly."""

    def happy(self):
        return dict(preflight=[{'structured': ready_preflight('T-1')}],
                    implementation=[{'edits': self.edit(), 'structured': done_output()}],
                    review=[{'structured': review_pass()}])

    def test_new_project_defaults_to_main_with_nothing_protected(self):
        layout = self.make_repository([self.story()], branch=None)
        config = layout.config()
        self.assertEqual('main', config['git']['branch'])
        self.assertEqual([], config['git']['protected_branches'])
        self.assertEqual('main', prd.load(layout.prd_path)['branchName'])

    def test_task_is_committed_and_pushed_on_main(self):
        self.make_repository([self.story()], branch=None)
        # The remote's default branch used to be refused automatically; it must not be any more.
        self.git('fetch', '--quiet', 'origin')
        self.git('remote', 'set-head', 'origin', 'main')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('main', self.git('branch', '--show-current'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'main', cwd=self.remote))
        self.assertIn('Add greeting output', self.remote_log('main'))
        self.assertEqual(['main'], self.git('branch', '--format=%(refname:short)').splitlines())
        self.assertEqual(['main'], self.git('branch', '--format=%(refname:short)', cwd=self.remote).splitlines())

    def test_run_started_elsewhere_moves_to_main(self):
        self.make_repository([self.story()], branch=None)
        self.git('switch', '--quiet', '-c', 'feature')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('main', self.git('branch', '--show-current'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'main', cwd=self.remote))

    def test_owner_unpushed_commits_on_main_are_published_first(self):
        self.make_repository([self.story()], branch=None)
        (self.project / 'mine.txt').write_text('x', encoding='utf-8')
        self.git('add', 'mine.txt')
        self.git('commit', '--quiet', '-m', 'Add my own file')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertIn('Add my own file', self.remote_log('main'))

    def test_main_behind_the_remote_stops_without_merging(self):
        self.make_repository([self.story()], branch=None)
        other = self.tmp / 'other'
        self.git('clone', '--quiet', '-b', 'main', str(self.remote), str(other), cwd=self.tmp)
        self.git('config', 'user.name', 'Other', cwd=other)
        self.git('config', 'user.email', 'other@example.invalid', cwd=other)
        (other / 'other.txt').write_text('x', encoding='utf-8')
        self.git('add', '--all', cwd=other)
        self.git('commit', '--quiet', '-m', 'Work elsewhere', cwd=other)
        self.git('push', '--quiet', 'origin', 'main', cwd=other)
        remote_before = self.git('rev-parse', 'main', cwd=self.remote)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('behind', self.lines[-1])
        self.assertEqual(remote_before, self.git('rev-parse', 'main', cwd=self.remote))
        self.assertEqual([], self.launches())

    def test_existing_project_keeps_its_branch(self):
        from autopilot_core import project as project_module
        layout = self.make_repository([self.story()], branch='autopilot/roadmap')
        project_module.setup(self.project)
        self.assertEqual('autopilot/roadmap', layout.config()['git']['branch'])
        self.assertEqual('autopilot/roadmap', prd.load(layout.prd_path)['branchName'])

    def test_validate_warns_when_the_branch_would_be_created(self):
        layout = self.make_repository([self.story()], branch=None)
        self.git('branch', '-m', 'main', 'master')
        self.git('push', '--quiet', 'origin', 'master')
        self.git('symbolic-ref', 'HEAD', 'refs/heads/master', cwd=self.remote)
        self.git('push', '--quiet', 'origin', '--delete', 'main')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'validate', '--project', str(self.project)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('[warn] working branch exists: main does not exist', result.stdout)
        self.assertTrue(layout.config_path.is_file())


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class SupervisedRunTests(Sandbox):
    """A supervised run ends at the first thing its session has to act on, and goes on when resumed."""

    BLOCKED = 'Needs a key'

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), *args, '--project', str(self.project)],
                              capture_output=True, text=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8', **env})

    def state(self):
        return read_json(self.layout.state_path)

    def message(self):
        return self.state()['run']['message']

    def notes(self, task_id='T-1'):
        return prd.story(prd.load(self.layout.prd_path), task_id)['notes']

    def work(self, name='app.txt', content='changed\n', subject='Add greeting output'):
        return {'edits': self.edit(name, content), 'structured': done_output(subject)}

    def blocked(self, task_id='T-1'):
        return {'structured': ready_preflight(task_id, outcome='blocked', blocked_reason=self.BLOCKED)}

    def two_tasks(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])

    # ---- when a run ends ------------------------------------------------------------

    def test_a_block_ends_the_run_before_the_next_task_and_resume_goes_on_without_it(self):
        self.two_tasks()
        self.scenario(preflight=[self.blocked(), {'structured': ready_preflight('T-2')}],
                      implementation=[self.work()], review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner(supervised=True).run(), self.lines)
        self.assertEqual(1, len(self.launches()), 'the next task did not start')
        self.assertIn(f'attention: T-1 is blocked (block 1 of this task): {self.BLOCKED}', self.message())
        self.assertIn(str(runner_module.SUPERVISION_GUIDE), self.message())
        self.assertFalse(self.layout.lock_path.exists())
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner(supervised=True).run(), self.lines)
        self.assertEqual('completed', self.state()['tasks']['T-2']['status'])
        self.assertNotIn('attention', self.message(), 'the block was raised once')

    def test_a_block_with_nothing_left_to_run_still_reports_it(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[self.blocked()])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner(supervised=True).run(), self.lines)
        self.assertIn('attention: T-1 is blocked', self.message())
        self.assertIn(str(runner_module.SUPERVISION_GUIDE), self.message())
        self.assertNotIn('resume', self.message(), 'restarting here would only loop')

    def test_the_same_failure_twice_ends_the_run_once(self):
        self.layout = self.make_repository([self.story()])
        broken = self.work(content='BROKEN\n')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[broken, broken, broken])
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner(supervised=True).run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')), 'the first failure goes to the normal retry')
        self.assertIn('attempt 2 failed like the one before it: check "test" failed', self.message())
        self.assertEqual('ready', self.state()['tasks']['T-1']['status'])
        self.assertEqual('BROKEN\n', (self.project / 'app.txt').read_text(encoding='utf-8'), 'the work stays in place')
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner(supervised=True).run(), self.lines)
        self.assertEqual(3, len(self.launches('implementation')))
        self.assertEqual('blocked', self.state()['tasks']['T-1']['status'])

    def test_a_different_failure_is_left_to_the_retries(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], review=[{'structured': review_pass()}],
                      implementation=[self.work(content='BROKEN\n'),
                                      {**self.work(content='BROKEN\n'), 'structured': done_output(conventions_ack='wrong')},
                                      self.work()])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner(supervised=True).run(), self.lines)
        self.assertEqual(3, len(self.launches('implementation')))

    def test_a_session_changing_the_plan_ends_the_run_after_its_task(self):
        self.two_tasks()
        forged = self.layout.prd_path.read_text(encoding='utf-8').replace('"passes": false', '"passes": true')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], review=[{'structured': review_pass()}],
                      implementation=[{'edits': {**self.edit(), str(self.layout.prd_path): forged},
                                       'structured': done_output()}])
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner(supervised=True).run(), self.lines)
        self.assertIn('attention: T-1: a session changed prd.json; the runner restored it', self.message())
        self.assertEqual(1, len(self.launches('preflight')))

    def test_an_anomaly_is_reported_even_when_the_plan_completes(self):
        self.layout = self.make_repository([self.story()])
        forged = self.layout.prd_path.read_text(encoding='utf-8').replace('"passes": false', '"passes": true')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], review=[{'structured': review_pass()}],
                      implementation=[{'edits': {**self.edit(), str(self.layout.prd_path): forged},
                                       'structured': done_output()}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner(supervised=True).run(), self.lines)
        self.assertIn('a session changed prd.json', self.message())

    def test_a_requested_stop_does_not_invite_carrying_on(self):
        self.layout = self.make_repository([self.story()])
        runner_module.request_stop(self.project)
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner(supervised=True).run(), self.lines)
        self.assertNotIn(str(runner_module.SUPERVISION_GUIDE), self.message())

    def test_a_clean_supervised_run_says_nothing_more(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[self.work()],
                      review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner(supervised=True).run(), self.lines)
        self.assertEqual('all tasks are complete and published', self.message())

    def test_an_unsupervised_run_reports_as_before(self):
        self.two_tasks()
        self.scenario(preflight=[self.blocked(), {'structured': ready_preflight('T-2')}],
                      implementation=[self.work()], review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual('no runnable task: 1 blocked (T-1), 0 waiting on them', self.message())

    # ---- what needs a person --------------------------------------------------------

    def test_a_deferred_task_is_skipped_reported_and_brought_back(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2),
                                            self.story('T-3', priority=3, dependsOn=['T-1'])])
        request = 'Provide the API key'
        result = self.cli('defer', '--task', 'T-1', '--reason', request)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], self.launches(), 'setting a task aside costs no session')
        status = json.loads(self.cli('status', '--json').stdout)
        self.assertEqual(request, status['blocked'][0]['needs_owner'])
        self.assertEqual({'T-3': ['T-1']}, status['waiting_on_blocked'])
        human = self.cli('status').stdout
        self.assertIn(f'  needs you: {request}', human)
        self.assertIn('waiting on blocked tasks: 1', human)

        self.scenario(preflight=[{'structured': ready_preflight('T-2')}], implementation=[self.work()],
                      review=[{'structured': review_pass()}])
        result = self.cli('run', '--supervised')
        self.assertEqual(runner_module.EXIT_BLOCKED, result.returncode, result.stdout + result.stderr)
        self.assertIn(f'T-1 stays set aside and needs you: {request}', result.stdout)
        self.assertIn(f'T-1 needs you: {request}', result.stdout)
        self.assertNotIn('attention', result.stdout, 'a task already set aside does not end the run again')
        self.assertEqual('completed', self.state()['tasks']['T-2']['status'])

        self.assertEqual(0, self.cli('unblock', '--task', 'T-1').returncode)
        self.assertEqual([], json.loads(self.cli('status', '--json').stdout)['blocked'])
        self.assertIsNone(self.state()['tasks']['T-1']['needs_owner'], 'a later block must not repeat an old request')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-3')}],
                      implementation=[self.work('other.txt', 'one\n', 'Add other output'),
                                      self.work('third.txt', 'two\n', 'Add third output')],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        result = self.cli('resume', '--supervised')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({'completed'}, {entry['status'] for entry in self.state()['tasks'].values()})

    def test_deferring_a_task_with_partial_work_sets_the_work_aside(self):
        self.layout = self.make_repository([self.story()])
        broken = self.work(content='BROKEN\n')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[broken, broken])
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner(supervised=True).run(), self.lines)
        result = self.cli('defer', '--task', 'T-1', '--reason', 'Restart the database')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        entry = self.state()['tasks']['T-1']
        self.assertEqual(('blocked', 'Restart the database'), (entry['status'], entry['needs_owner']['request']))
        self.assertIn('BROKEN', self.git('show', f'{entry["preserved_commit"]}:app.txt'))
        self.assertEqual('base\n', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_defer_refuses_what_it_cannot_set_aside(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[self.work()],
                      review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        finished = self.cli('defer', '--task', 'T-1', '--reason', 'x')
        self.assertEqual(runner_module.EXIT_USAGE, finished.returncode)
        self.assertIn('nothing to set aside', finished.stderr)
        unknown = self.cli('defer', '--task', 'T-9', '--reason', 'x')
        self.assertEqual(runner_module.EXIT_USAGE, unknown.returncode)
        self.assertIn('unknown task', unknown.stderr)
        with RunnerLock(self.layout.lock_path):
            active = self.cli('defer', '--task', 'T-1', '--reason', 'x')
        self.assertEqual(runner_module.EXIT_USAGE, active.returncode)
        self.assertIn('another runner is active', active.stderr)

    # ---- picking work up again -------------------------------------------------------

    def block_with_partial_work(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'edits': {**self.edit(content='partial\n'), **self.edit('new.txt', 'new file\n')},
            'structured': done_output(status='blocked', blocked_reason='A file is denied')}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual('base\n', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_restored_work_goes_through_the_normal_verification_and_commit(self):
        self.block_with_partial_work()
        result = self.cli('unblock', '--task', 'T-1', '--restore-work')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('partial work back in the tree', result.stdout)
        self.assertEqual('partial\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual('new file\n', (self.project / 'new.txt').read_text(encoding='utf-8'))
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[self.work(content='done\n')],
                      review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertIn('A previous attempt was interrupted', self.launches('implementation')[-1]['prompt'])
        self.assertEqual(['review'], [e['phase'] for e in self.launches()][-1:], 'an independent review still ran')
        changed = self.git('show', '--name-only', '--format=', 'HEAD').splitlines()
        self.assertIn('new.txt', changed)
        self.assertIn('Task: T-1', self.git('log', '-1', '--format=%B'))

    def test_restoring_work_refuses_a_tree_with_other_changes(self):
        self.block_with_partial_work()
        (self.project / 'app.txt').write_text('mine\n', encoding='utf-8')
        result = self.cli('unblock', '--task', 'T-1', '--restore-work')
        self.assertEqual(runner_module.EXIT_ERROR, result.returncode, result.stdout)
        self.assertIn('uncommitted changes', result.stderr)
        self.assertEqual('blocked', self.state()['tasks']['T-1']['status'])
        self.assertEqual('mine\n', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_restoring_work_says_when_there_was_none(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[self.blocked()])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        result = self.cli('unblock', '--task', 'T-1', '--restore-work')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('no partial work had been set aside', result.stdout)
        self.assertEqual('pending', self.state()['tasks']['T-1']['status'])

    def test_a_block_and_an_unblock_keep_the_notes_written_for_the_task(self):
        self.layout = self.make_repository([self.story(notes='Call scripts/x.sh exactly')])
        self.BLOCKED = 'Needs\na key'
        self.scenario(preflight=[self.blocked(), self.blocked()])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual('BLOCKED: Needs a key\nCall scripts/x.sh exactly', self.notes())
        runner_module.unblock(self.project, 'T-1')
        self.assertEqual('Call scripts/x.sh exactly', self.notes())
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertEqual(1, self.notes().count('BLOCKED'))
        plan = prd.load(self.layout.prd_path)
        plan['userStories'][0]['notes'] += '\nAlso use Y'
        prd.save(self.layout.prd_path, plan)
        runner_module.unblock(self.project, 'T-1')
        self.assertEqual('Call scripts/x.sh exactly\nAlso use Y', self.notes())

    # ---- pausing ---------------------------------------------------------------------

    def test_pause_ends_the_run_after_the_current_attempt_and_the_next_start_clears_it(self):
        self.two_tasks()
        finished = {**self.work(), 'edits': {**self.edit(), str(self.layout.pause_path): 'now\n'}}
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[finished, self.work('other.txt', 'ok\n', 'Add other output')],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run(), self.lines)
        self.assertIn('paused after the current attempt', self.message())
        self.assertEqual('completed', self.state()['tasks']['T-1']['status'], 'nothing was interrupted')
        self.assertEqual(1, len(self.launches('preflight')))
        self.assertEqual(runner_module.EXIT_COMPLETE, self.runner().run(), self.lines)
        self.assertFalse(self.layout.pause_path.exists())

    def test_pause_needs_an_active_runner(self):
        self.layout = self.make_repository([self.story()])
        idle = self.cli('pause')
        self.assertEqual(runner_module.EXIT_USAGE, idle.returncode)
        self.assertFalse(self.layout.pause_path.exists())
        with RunnerLock(self.layout.lock_path):
            active = self.cli('pause')
        self.assertEqual(0, active.returncode, active.stderr)
        self.assertTrue(self.layout.pause_path.exists())

    # ---- started from a session ------------------------------------------------------

    def test_project_code_does_not_inherit_the_channel_into_the_session(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['checks'] = [{'name': 'test', 'required': True, 'command':
                             f'"{sys.executable}" -c "import os,sys; sys.exit(1 if os.environ.get(\'CLAUDE_CODE_MESSAGING_TOKEN\') else 0)"'}]
        write_json_atomic(self.layout.config_path, config)
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[self.work()],
                      review=[{'structured': review_pass()}])
        result = self.cli('run', '--supervised', CLAUDE_CODE_MESSAGING_TOKEN='parent', CLAUDECODE='1', CLAUDE_EFFORT='low')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse({'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDECODE', 'CLAUDE_EFFORT'} & set(self.launches()[0]['env']))


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class CommandLineTests(Sandbox):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), *args, '--project', str(self.project)],
                              capture_output=True, text=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def test_setup_is_idempotent_and_starts_nothing(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        (self.project / 'ROADMAP.md').write_text('# R\n\n- [ ] A-1 Do it\n', encoding='utf-8')
        first = self.cli('setup')
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn('Development has not started', first.stdout)
        snapshot = (self.project / 'scripts/ralph/prd.json').read_text(encoding='utf-8')
        second = self.cli('setup')
        self.assertIn('nothing changed', second.stdout)
        self.assertEqual(snapshot, (self.project / 'scripts/ralph/prd.json').read_text(encoding='utf-8'))
        self.assertEqual([], self.launches())
        self.assertEqual('', subprocess.run(['git', 'log'], cwd=self.project, capture_output=True, text=True).stdout)

    def test_absolute_roadmap_path_is_stored_relative(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        (self.project / 'R.md').write_text('# R\n\n- [ ] A-1 Do it\n', encoding='utf-8')
        self.cli('setup', '--roadmap', str(self.project / 'R.md'))
        config = json.loads((self.project / '.ajimu-autopilot/config.json').read_text(encoding='utf-8'))
        self.assertEqual('R.md', config['docs']['roadmap'])
        outside = self.tmp / 'outside.md'
        outside.write_text('x', encoding='utf-8')
        result = self.cli('setup', '--roadmap', str(outside))
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertIn('inside the project', result.stderr)
        result = self.cli('setup', '--roadmap', '../outside.md')
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertIn('inside the project', result.stderr)

    def test_import_roadmap_stores_a_project_relative_source(self):
        self.make_repository([self.story()])
        (self.project / 'R.md').write_text('- [ ] A-1 Do it\n', encoding='utf-8')
        result = self.cli('import-roadmap', '--roadmap', str(self.project / 'R.md'))
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads((self.project / 'scripts/ralph/prd.json').read_text(encoding='utf-8'))
        self.assertEqual('R.md', plan['userStories'][-1]['source']['path'])
        outside = self.tmp / 'outside.md'
        outside.write_text('- [ ] B-1 Elsewhere\n', encoding='utf-8')
        result = self.cli('import-roadmap', '--roadmap', str(outside))
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertIn('inside the project', result.stderr)

    def test_setup_accepts_a_roadmap_with_repeated_lines(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        (self.project / 'ROADMAP.md').write_text('# A\n\n- [ ] Add tests\n\n# B\n\n- [ ] Add tests\n', encoding='utf-8')
        result = self.cli('setup')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('imported 2 new tasks', result.stdout)
        self.assertIn('nothing changed', self.cli('setup').stdout)

    def test_stop_outside_a_configured_project_changes_nothing(self):
        self.project.mkdir()
        result = self.cli('stop')
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertIn('not configured', result.stderr)
        self.assertEqual([], list(self.project.iterdir()))
        missing = self.tmp / 'missing'
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'stop', '--project', str(missing)],
                                capture_output=True, text=True)
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertFalse(missing.exists())

    def test_max_iterations_must_be_positive(self):
        self.make_repository([self.story()])
        for value in ('0', '-1'):
            result = self.cli('run', '--max-iterations', value)
            self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
            self.assertIn('positive', result.stderr)
        self.assertEqual([], self.launches())

    def test_plan_changes_wait_for_an_active_runner(self):
        layout = self.make_repository([self.story()])
        (self.project / 'R.md').write_text('- [ ] A-1 Do it\n', encoding='utf-8')
        before = layout.prd_path.read_bytes()
        with RunnerLock(layout.lock_path):
            for args in (('unblock', '--task', 'T-1'), ('import-roadmap', '--roadmap', 'R.md'), ('setup', '--roadmap', 'R.md')):
                result = self.cli(*args)
                self.assertEqual(runner_module.EXIT_USAGE, result.returncode, args)
                self.assertIn('another runner is active', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(before, layout.prd_path.read_bytes())

    def test_resume_keeps_the_stop_request_of_a_runner_still_active(self):
        layout = self.make_repository([self.story()])
        self.assertEqual(0, self.cli('stop').returncode)
        with RunnerLock(layout.lock_path):
            result = self.cli('resume')
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertIn('another runner is active', result.stderr)
        self.assertTrue(layout.stop_path.exists(), 'the active runner still has to stop')

    def test_live_probe_at_a_usage_limit_does_not_mark_a_model_unavailable(self):
        layout = self.make_repository([self.story()])
        limited = {'rate_limited': True, 'is_error': True, 'api_error_status': 429}
        self.scenario(probe=[{'structured': {'ack': 'ok'}}, limited])
        result = self.cli('validate', '--live')
        self.assertIn('[warn] live opus', result.stdout)
        self.assertIn('availability was not measured', result.stdout)
        self.assertEqual(['sonnet', 'opus'], read_json(layout.capabilities_path)['families'])
        self.assertEqual([], self.launches('preflight'), 'no preflight is spent while usage is out')

    def test_status_tells_a_dead_runner_from_an_active_one(self):
        layout = self.make_repository([self.story()])
        process = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        import socket
        write_json_atomic(layout.lock_path, {'pid': int(process.stdout), 'host': socket.gethostname(), 'started_at': 'x'})
        write_json_atomic(layout.state_path, {'iteration': 1, 'tasks': {'T-1': {'status': 'running', 'attempts': 1}},
                                              'run': {'status': 'running'}, 'current_task': 'T-1'})
        self.assertFalse(json.loads(self.cli('status', '--json').stdout)['runner_active'])
        self.assertIn('no runner is active', self.cli('status').stdout)
        layout.lock_path.unlink()
        with RunnerLock(layout.lock_path):
            self.assertTrue(json.loads(self.cli('status', '--json').stdout)['runner_active'])

    def test_status_treats_a_task_reopened_by_hand_as_open(self):
        layout = self.make_repository([self.story()])
        write_json_atomic(layout.state_path, {'iteration': 1, 'tasks': {'T-1': {'status': 'completed', 'attempts': 1}},
                                              'run': {'status': 'complete'}, 'current_task': None})
        status = json.loads(self.cli('status', '--json').stdout)
        self.assertEqual([], status['completed'])
        self.assertEqual([{'id': 'T-1', 'status': 'pending'}], status['remaining'])

    def test_malformed_plan_or_checks_are_reported_without_a_traceback(self):
        layout = self.make_repository([self.story()])
        good_plan, good_config = layout.prd_path.read_text(encoding='utf-8'), layout.config_path.read_text(encoding='utf-8')
        for broken in ({'project': 'p', 'branchName': 'b', 'userStories': [self.story(id=['x'])]},
                       {'project': 'p', 'branchName': 'b', 'userStories': [self.story(dependsOn=5)]},
                       {'project': 'p', 'branchName': 'b', 'userStories': 5}, [1, 2]):
            write_json_atomic(layout.prd_path, broken)
            for args in (('validate',), ('status',), ('preflight', '--task', 'T-1'), ('import-roadmap', '--roadmap', 'check.py'),
                         ('setup',), ('run',)):
                with self.subTest(args=args, plan=broken):
                    result = self.cli(*args)
                    self.assertNotEqual(0, result.returncode)
                    self.assertNotIn('Traceback', result.stderr)
        layout.prd_path.write_text(good_plan, encoding='utf-8')
        config = json.loads(good_config)
        config['checks'] = ['npm test']
        write_json_atomic(layout.config_path, config)
        for args in (('validate',), ('run',), ('preflight', '--task', 'T-1'), ('setup',)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('Traceback', result.stderr)
                self.assertIn('checks must be', result.stdout + result.stderr)
        result = self.cli('validate', '--live')
        self.assertIn('live probes', result.stdout)
        self.assertEqual([], self.launches(), 'nothing is spent on a broken configuration')

    def test_byte_order_mark_in_a_hand_edited_file_is_accepted(self):
        layout = self.make_repository([self.story()])
        layout.config_path.write_bytes(b'\xef\xbb\xbf' + layout.config_path.read_bytes())
        result = self.cli('status')
        self.assertEqual(0, result.returncode, result.stderr)

    def test_validate_names_task_ids_the_validator_refuses(self):
        self.make_repository([self.story('AI-3')])
        result = self.cli('validate')
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn('[FAIL] task IDs in commit messages: AI-3', result.stdout)

    def test_validate_routes_custom_task_types_through_their_override(self):
        layout = self.make_repository([self.story()])
        config = layout.config()
        config['routing']['overrides'] = {'my_type': {'model': 'sonnet', 'effort': 'low'}}
        write_json_atomic(layout.config_path, config)
        result = self.cli('validate')
        self.assertIn('[ok  ] model routing', result.stdout)

    @unittest.skipIf(sys.platform == 'win32', 'TerminateProcess cannot be caught on Windows')
    def test_termination_signal_ends_the_session_and_pauses(self):
        layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'sleep': 60, 'structured': ready_preflight('T-1')}])
        process = subprocess.Popen([sys.executable, str(SCRIPTS / 'autopilot.py'), 'run', '--project', str(self.project)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 30
        while not self.launches() and time.monotonic() < deadline:
            time.sleep(0.2)
        session = self.launches()[0]['pid']
        process.terminate()
        output = process.communicate(timeout=30)
        self.assertEqual(runner_module.EXIT_PAUSED, process.returncode, output)
        with self.assertRaises(ProcessLookupError):
            os.kill(session, 0)
        self.assertFalse(layout.lock_path.exists())
        self.assertEqual('paused', read_json(layout.state_path)['run']['status'])

    def test_status_survives_titles_the_console_cannot_encode(self):
        self.make_repository([self.story(title='Support \U0001F680 launches')])
        env = {**os.environ, 'PYTHONIOENCODING': 'ascii'}
        self.scenario(preflight=[{'structured': ready_preflight('T-1', outcome='blocked', blocked_reason='Needs \U0001F680')}])
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'run', '--project', str(self.project)],
                                capture_output=True, text=True, env=env, encoding='ascii', errors='replace')
        self.assertEqual(runner_module.EXIT_BLOCKED, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_setup_keeps_existing_settings(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        self.cli('setup')
        path = self.project / '.ajimu-autopilot/config.json'
        config = json.loads(path.read_text(encoding='utf-8'))
        config['limits']['max_iterations'] = 7
        path.write_text(json.dumps(config), encoding='utf-8')
        self.cli('setup')
        self.assertEqual(7, json.loads(path.read_text(encoding='utf-8'))['limits']['max_iterations'])

    def test_validate_passes_and_reports_the_policy(self):
        self.make_repository([self.story()])
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        result = self.cli('validate')
        self.assertEqual(0, result.returncode, result.stdout)
        for expected in ('Haiku is rejected', 'commit message validator', 'remote access', 'valid'):
            self.assertIn(expected, result.stdout)

    def test_validate_flags_missing_checks_and_haiku_settings(self):
        layout = self.make_repository([self.story()])
        config = layout.config()
        config['checks'] = []
        config['preflight']['model'] = 'haiku'
        write_json_atomic(layout.config_path, config)
        result = self.cli('validate')
        self.assertEqual(1, result.returncode)
        self.assertIn('below the Sonnet floor', result.stdout)
        self.assertIn('project checks configured', result.stdout)

    def test_bypass_is_refused_in_configuration(self):
        layout = self.make_repository([self.story()])
        config = layout.config()
        config['claude']['command'] = ['claude', '--dangerously-skip-permissions']
        config['permissions']['bash_allow'] = ['git push *']
        write_json_atomic(layout.config_path, config)
        result = self.cli('validate')
        self.assertEqual(1, result.returncode)
        self.assertIn('must not carry permission', result.stdout)
        self.assertIn('reserved for the runner', result.stdout)

    def test_route_command_refuses_haiku(self):
        self.project.mkdir()
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'route', '--task-type', 'docs_simple',
                                 '--model', 'haiku'], capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn('refused', result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'route', '--task-type', 'docs_simple'],
                                capture_output=True, text=True)
        self.assertEqual('sonnet', json.loads(result.stdout)['model'])

    def test_standalone_preflight_does_not_implement(self):
        self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}])
        result = self.cli('preflight', '--task', 'T-1')
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual('ready', json.loads(result.stdout)['outcome'])
        self.assertEqual(['preflight'], [entry['phase'] for entry in self.launches()])

    def test_standalone_preflight_pauses_at_a_usage_limit_instead_of_waiting(self):
        self.make_repository([self.story()])
        self.scenario(preflight=[{'rate_limited': True, 'is_error': True, 'api_error_status': 429}])
        result = self.cli('preflight', '--task', 'T-1')
        self.assertEqual(runner_module.EXIT_PAUSED, result.returncode, result.stderr + result.stdout)
        self.assertIn('usage or service limit', result.stderr)

    def test_standalone_preflight_refuses_a_closed_task_without_a_traceback(self):
        layout = self.make_repository([self.story(passes=True), self.story('T-2', priority=2)])
        write_json_atomic(layout.state_path, {'iteration': 0, 'tasks': {'T-2': {'status': 'blocked', 'attempts': 0}},
                                              'run': {'status': 'blocked'}, 'current_task': None})
        for task_id, status in (('T-1', 'completed'), ('T-2', 'blocked')):
            result = self.cli('preflight', '--task', task_id)
            self.assertEqual(runner_module.EXIT_USAGE, result.returncode, result.stderr)
            self.assertIn(status, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        self.assertEqual([], self.launches())

    def test_standalone_preflight_recovers_a_crashed_task(self):
        layout = self.make_repository([self.story()])
        write_json_atomic(layout.state_path, {'iteration': 1, 'tasks': {'T-1': {'status': 'running', 'attempts': 1}},
                                              'run': {'status': 'running'}, 'current_task': 'T-1'})
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}])
        result = self.cli('preflight', '--task', 'T-1')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('ready', read_json(layout.state_path)['tasks']['T-1']['status'])

    def test_standalone_preflight_reports_a_git_problem_without_a_traceback(self):
        layout = self.make_repository([self.story()])
        config = layout.config()
        config['git']['protected_branches'] = ['autopilot/roadmap']
        write_json_atomic(layout.config_path, config)
        result = self.cli('preflight', '--task', 'T-1')
        self.assertEqual(runner_module.EXIT_ERROR, result.returncode, result.stderr)
        self.assertIn('protected_branches', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_standalone_preflight_of_a_split_task_is_not_reported_as_blocked(self):
        self.make_repository([self.story()])
        children = [{'title': f'Part {n}', 'description': 'Part.', 'acceptanceCriteria': ['Works']} for n in (1, 2)]
        split = ready_preflight('T-1', outcome='split', split_tasks=children,
                                corrections=[{'field': 'scope', 'reason': 'too large', 'evidence': 'many files'}])
        self.scenario(preflight=[{'structured': split}])
        result = self.cli('preflight', '--task', 'T-1')
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)

    def test_run_stop_resume_and_status_commands(self):
        layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.cli('stop').returncode)
        refused = self.cli('run')
        self.assertEqual(runner_module.EXIT_USAGE, refused.returncode)
        self.assertIn('resume', refused.stderr)
        resumed = self.cli('resume')
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        status = json.loads(self.cli('status', '--json').stdout)
        self.assertEqual(['T-1'], status['completed'])
        self.assertEqual([], status['remaining'])
        self.assertEqual(1, status['iteration'])
        self.assertEqual('sonnet', status['selection']['model'])
        self.assertEqual('medium', status['selection']['effort'])
        self.assertTrue(status['latest_checks']['results'][0]['passed'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), status['last_commit'])
        self.assertEqual(status['last_commit'], status['last_push']['commit'])
        human = self.cli('status').stdout
        for expected in ('completed: 1', 'selection: T-1 -> sonnet / medium', 'last push:'):
            self.assertIn(expected, human)
        self.assertTrue((ROOT / 'skills').is_dir())
        self.assertFalse(layout.lock_path.exists())


if __name__ == '__main__':
    unittest.main()
