"""Preflight contract and plan rules, without any process or repository."""
import copy
import tempfile
import unittest
from pathlib import Path

from support import ready_preflight
from autopilot_core import prd, preflight, roadmap


def story(task_id='T-1', **overrides):
    item = {'id': task_id, 'title': 'Add export', 'description': 'Export reports.', 'acceptanceCriteria': ['CSV downloads'],
            'priority': 1, 'passes': False, 'notes': '', 'dependsOn': []}
    item.update(overrides)
    return item


def plan(*stories):
    return {'project': 'p', 'branchName': 'autopilot/roadmap', 'description': '', 'userStories': list(stories)}


def answer(item, **overrides):
    return ready_preflight(item['id'], **{'analyzed_task_version': prd.task_version(item), **overrides})


class ValidationTests(unittest.TestCase):
    def test_ready_result_gets_a_routing_decision_bound_to_the_commit(self):
        item = story()
        record = preflight.validate_result(answer(item), item, 'abc')
        self.assertEqual('abc', record['head_commit'])
        self.assertEqual('sonnet', record['routing']['model'])

    def test_structural_contract_is_enforced(self):
        item = story()
        for name, broken in (('missing field', {k: v for k, v in answer(item).items() if k != 'risks'}),
                             ('unknown outcome', answer(item, outcome='maybe')),
                             ('extra field', {**answer(item), 'surprise': 1}),
                             ('other task', {**answer(item), 'task_id': 'T-9'}),
                             ('stale version', answer(item, analyzed_task_version='old')),
                             ('no result', None)):
            with self.subTest(name), self.assertRaises(preflight.PreflightInvalid):
                preflight.validate_result(broken, item, 'abc')

    def test_haiku_recommendation_never_passes(self):
        item = story()
        with self.assertRaises(preflight.PreflightInvalid):
            preflight.validate_result(answer(item, recommended_model='haiku'), item, 'abc')

    def test_corrected_needs_the_correction_and_its_reason(self):
        item = story()
        with self.assertRaises(preflight.PreflightInvalid):
            preflight.validate_result(answer(item, outcome='corrected'), item, 'abc')
        with self.assertRaises(preflight.PreflightInvalid):
            preflight.validate_result(answer(item, outcome='corrected', corrected_task={'title': 'x'}), item, 'abc')

    def test_blocked_needs_a_reason(self):
        item = story()
        with self.assertRaises(preflight.PreflightInvalid):
            preflight.validate_result(answer(item, outcome='blocked'), item, 'abc')

    def test_already_implemented_needs_evidence_for_each_criterion(self):
        item = story(acceptanceCriteria=['A', 'B'])
        partial = [{'criterion': 'A', 'met': True, 'evidence': 'file.py:1'}]
        with self.assertRaises(preflight.PreflightInvalid):
            preflight.validate_result(answer(item, outcome='already_implemented', acceptance_evidence=partial), item, 'abc')
        full = partial + [{'criterion': 'B', 'met': True, 'evidence': 'file.py:2'}]
        record = preflight.validate_result(answer(item, outcome='already_implemented', acceptance_evidence=full), item, 'abc')
        self.assertEqual('already_implemented', record['outcome'])

    def test_opus_required_but_unavailable_becomes_a_block(self):
        item = story()
        record = preflight.validate_result(answer(item, task_type='refactor_architectural', sonnet_sufficient=False),
                                           item, 'abc', available_families=('sonnet',))
        self.assertEqual('blocked', record['outcome'])
        self.assertIn('Opus', record['blocked_reason'])


class GateTests(unittest.TestCase):
    def record(self, item, head='abc', **overrides):
        record = preflight.validate_result(answer(item), item, head)
        record.update(overrides)
        return record

    def test_valid_record_passes(self):
        item = story()
        preflight.require_passed(self.record(item), item, 'abc')

    def test_every_invalid_state_is_refused(self):
        item = story()
        cases = {
            'absent': None,
            'not ready': self.record(item, outcome='corrected'),
            'other version': self.record(item, analyzed_task_version='old'),
            'other commit': self.record(item, head='old'),
            'no routing': self.record(item, routing=None),
            'forbidden model': self.record(item, routing={'model': 'haiku', 'effort': 'low'}),
            'bad effort': self.record(item, routing={'model': 'sonnet', 'effort': 'ultra'}),
        }
        for name, record in cases.items():
            with self.subTest(name), self.assertRaises(preflight.PreflightGateError):
                preflight.require_passed(record, item, 'abc')

    def test_editing_the_task_invalidates_the_preflight(self):
        item = story()
        record = self.record(item)
        edited = copy.deepcopy(item)
        edited['acceptanceCriteria'].append('New requirement')
        with self.assertRaises(preflight.PreflightGateError):
            preflight.require_passed(record, edited, 'abc')


class CorrectionTests(unittest.TestCase):
    def test_ambiguous_task_is_corrected_with_traceability(self):
        item = story(description='Make export better.')
        result = answer(item, outcome='corrected',
                        corrected_task={'description': 'Add CSV export to the reports page.',
                                        'acceptanceCriteria': ['CSV downloads', 'Empty reports export headers only']},
                        corrections=[{'field': 'description', 'reason': 'ambiguous', 'evidence': 'ROADMAP.md:4'}])
        record = preflight.validate_result(result, item, 'abc')
        updated, change = preflight.apply_outcome(plan(item), record, item)
        self.assertEqual('Add CSV export to the reports page.', updated['userStories'][0]['description'])
        self.assertEqual('Make export better.', change['before']['description'])
        self.assertEqual('ambiguous', change['reasons'][0]['reason'])
        self.assertNotEqual(prd.task_version(item), prd.task_version(updated['userStories'][0]))

    def test_dropping_a_criterion_needs_justification(self):
        item = story(acceptanceCriteria=['A', 'B'])
        with self.assertRaises(prd.PlanError):
            prd.apply_correction(plan(item), 'T-1', {'acceptanceCriteria': ['A']})
        updated, _ = prd.apply_correction(plan(item), 'T-1', {'acceptanceCriteria': ['A']}, ['B'])
        self.assertEqual(['A'], updated['userStories'][0]['acceptanceCriteria'])

    def test_functional_change_is_refused(self):
        item = story()
        result = answer(item, outcome='corrected', functional_change=True, corrected_task={'description': 'Other product.'},
                        corrections=[{'field': 'description', 'reason': 'x', 'evidence': 'y'}])
        record = preflight.validate_result(result, item, 'abc')
        with self.assertRaises(prd.PlanError):
            preflight.apply_outcome(plan(item), record, item)

    def test_correction_cannot_introduce_a_cycle_or_unknown_dependency(self):
        first, second = story('A'), story('B', dependsOn=['A'])
        with self.assertRaises(prd.PlanError):
            prd.apply_correction(plan(first, second), 'A', {'dependsOn': ['B']})
        with self.assertRaises(prd.PlanError):
            prd.apply_correction(plan(first, second), 'A', {'dependsOn': ['Z']})

    def test_correction_cannot_change_identity_or_completion(self):
        item = story()
        updated, changed = prd.apply_correction(plan(item), 'T-1', {'id': 'X', 'passes': True, 'title': 'New'})
        self.assertEqual(['title'], changed)
        self.assertEqual('T-1', updated['userStories'][0]['id'])
        self.assertFalse(updated['userStories'][0]['passes'])


class SplitTests(unittest.TestCase):
    def children(self, count=2):
        return [{'title': f'Part {n}', 'description': f'Do part {n}.', 'acceptanceCriteria': [f'Part {n} works']}
                for n in range(1, count + 1)]

    def test_oversized_task_splits_in_place_and_dependents_follow(self):
        before, big, after = story('A'), story('B', dependsOn=['A'], priority=2), story('C', dependsOn=['B'], priority=3)
        result = answer(big, outcome='split', split_tasks=self.children(),
                        corrections=[{'field': 'scope', 'reason': 'too large', 'evidence': 'six components'}])
        record = preflight.validate_result(result, big, 'abc')
        updated, change = preflight.apply_outcome(plan(before, big, after), record, big)
        self.assertEqual(['A', 'B.1', 'B.2', 'C'], [item['id'] for item in updated['userStories']])
        self.assertEqual(['B.1', 'B.2'], change['children'])
        self.assertEqual(['A'], prd.story(updated, 'B.1')['dependsOn'])
        self.assertEqual(['A', 'B.1'], prd.story(updated, 'B.2')['dependsOn'])
        self.assertEqual(['B.1', 'B.2'], prd.story(updated, 'C')['dependsOn'])
        self.assertEqual('B', prd.story(updated, 'B.1')['splitFrom'])

    def test_split_needs_two_parts(self):
        item = story()
        with self.assertRaises(preflight.PreflightInvalid):
            preflight.validate_result(answer(item, outcome='split', split_tasks=self.children(1)), item, 'abc')

    def test_nested_split_keeps_the_shared_roadmap_line_open(self):
        big = story('T', source={'path': 'R.md', 'section': 'S', 'line': 'Big'})
        updated, _ = prd.apply_split(plan(big), 'T', self.children())
        updated, _ = prd.apply_split(updated, 'T.1', self.children())
        self.assertEqual(['T.1.1', 'T.1.2', 'T.2'], [item['id'] for item in updated['userStories']])
        self.assertTrue(prd.siblings_open(updated, prd.story(updated, 'T.2')))
        for task_id in ('T.1.1', 'T.1.2'):
            prd.story(updated, task_id)['passes'] = True
        self.assertFalse(prd.siblings_open(updated, prd.story(updated, 'T.2')))

    def test_repeated_roadmap_lines_are_independent_for_ticking(self):
        one = story('A', source={'path': 'R.md', 'section': 'S', 'line': 'Same'})
        two = story('A-2', source={'path': 'R.md', 'section': 'S', 'line': 'Same'})
        self.assertFalse(prd.siblings_open(plan(one, two), one))


class PlanTests(unittest.TestCase):
    def test_validation_reports_each_problem(self):
        broken = plan(story('A', dependsOn=['Z']), story('A'), story('B', acceptanceCriteria=[]), story('../x'))
        problems = ' | '.join(prd.validate(broken))
        for expected in ('unknown dependency Z', 'duplicate id', 'acceptanceCriteria', 'id must match'):
            self.assertIn(expected, problems)

    def test_cycles_are_named(self):
        problems = prd.validate(plan(story('A', dependsOn=['B']), story('B', dependsOn=['A'])))
        self.assertTrue(any('cycle' in problem and 'A' in problem for problem in problems))

    def test_selection_follows_priority_and_dependencies(self):
        tasks = plan(story('A', priority=5), story('B', priority=1, dependsOn=['A']), story('C', priority=9))
        self.assertEqual('A', prd.select_next(tasks, {})['id'])
        tasks['userStories'][0]['passes'] = True
        self.assertEqual('B', prd.select_next(tasks, {'A': 'completed'})['id'])

    def test_blocked_tasks_are_skipped_and_their_dependents_wait(self):
        tasks = plan(story('A'), story('B', dependsOn=['A']), story('C', priority=2))
        self.assertEqual('C', prd.select_next(tasks, {'A': 'blocked'})['id'])
        self.assertEqual({'B': ['A']}, prd.waiting_on_blocked(tasks, {'A': 'blocked'}))

    def test_interrupted_task_resumes_first(self):
        tasks = plan(story('A', priority=1), story('B', priority=9))
        self.assertEqual('B', prd.select_next(tasks, {'B': 'running'})['id'])

    def test_in_flight_task_still_waits_for_its_dependencies(self):
        tasks = plan(story('A', priority=5), story('B', priority=1, dependsOn=['A']))
        self.assertEqual('A', prd.select_next(tasks, {'B': 'ready'})['id'])

    def test_malformed_fields_are_reported_instead_of_raising(self):
        for broken, expected in ((story(id=['x']), 'id must match'), (story(dependsOn=5), 'dependsOn must be'),
                                 (story(dependsOn=[['A']]), 'dependsOn must be')):
            with self.subTest(expected):
                self.assertIn(expected, ' | '.join(prd.validate(plan(broken))))

    def test_load_valid_names_the_problem(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'prd.json'
            path.write_text('{"project": "p", "branchName": "b", "userStories": 5}', encoding='utf-8')
            with self.assertRaisesRegex(prd.PlanError, 'must be a list'):
                prd.load_valid(path)


SAMPLE = """# Product roadmap

## 1. P01 — Core

- [x] P01.1 Store projects on disk.
- [ ] P01.2 Validate project names.
  Names must be unique per owner.
  - [ ] Reject empty names

Exit: invalid names never reach storage.

## 2. P02 — Import

**Depends on:** P01. **Areas:** import.

- [ ] P02.1 Import a Git bundle.
- TODO: Support shallow bundles
"""


class RoadmapTests(unittest.TestCase):
    def test_sample_roadmap_keeps_structure(self):
        stories, summary = roadmap.parse(SAMPLE)
        by_id = {item['id']: item for item in stories}
        self.assertEqual({'imported': 4, 'marked_done': 1, 'sections': 3}, summary)
        self.assertTrue(by_id['P01.1']['passes'])
        self.assertIn('not verified', by_id['P01.1']['notes'])
        self.assertIn('Names must be unique per owner.', by_id['P01.2']['description'])
        self.assertIn('Reject empty names', by_id['P01.2']['description'])
        self.assertIn('invalid names never reach storage.', by_id['P01.2']['acceptanceCriteria'])
        self.assertEqual(['P01.1', 'P01.2'], by_id['P02.1']['dependsOn'])
        self.assertEqual('P01.2 Validate project names.', by_id['P01.2']['source']['line'])
        self.assertEqual('2. P02 — Import', by_id['P02.1']['source']['section'])
        self.assertEqual([], prd.validate(plan(*stories)))

    def test_hash_ids_are_stable_across_imports(self):
        first, _ = roadmap.parse('- [ ] Add login\n')
        second, _ = roadmap.parse('# Heading\n\n- [ ]   Add   login\n')
        self.assertEqual(first[0]['id'], second[0]['id'])
        self.assertTrue(first[0]['id'].startswith('RM-'))

    def test_merge_never_overwrites_existing_tasks(self):
        existing = plan(story('P01.2', description='Corrected by a preflight.', passes=True))
        stories, _ = roadmap.parse(SAMPLE)
        added = roadmap.merge(existing, stories)
        self.assertNotIn('P01.2', added)
        self.assertEqual('Corrected by a preflight.', prd.story(existing, 'P01.2')['description'])
        self.assertTrue(prd.story(existing, 'P01.2')['passes'])
        self.assertEqual([], roadmap.merge(existing, stories))
        self.assertEqual([], prd.validate(existing))

    def test_repeated_lines_become_separate_tasks_with_stable_ids(self):
        text = '# A\n\n- [ ] Add tests\n- [ ] P1 One\n\n# B\n\n- [ ] Add tests\n- [ ] P1 Two\n- [ ] P1-2 Three\n'
        stories, _ = roadmap.parse(text)
        ids = [item['id'] for item in stories]
        self.assertEqual(5, len(set(ids)), ids)
        self.assertEqual(ids, [item['id'] for item in roadmap.parse(text)[0]])
        tasks = plan()
        self.assertEqual(ids, roadmap.merge(tasks, stories))
        self.assertEqual([], prd.validate(tasks))
        self.assertEqual([], roadmap.merge(tasks, roadmap.parse(text)[0]))

    def test_split_or_renamed_task_is_not_imported_again(self):
        text = '- [ ] P01.1 Big work\n- [ ] P01.2 Other work\n'
        tasks = plan()
        roadmap.merge(tasks, roadmap.parse(text)[0])
        children = [{'title': f'Part {n}', 'description': 'Part.', 'acceptanceCriteria': ['Works']} for n in (1, 2)]
        tasks, _ = prd.apply_split(tasks, 'P01.1', children)
        for task_id in ('P01.1.1', 'P01.1.2'):
            tasks, _ = prd.apply_split(tasks, task_id, children)
        prd.story(tasks, 'P01.2')['id'] = 'RENAMED'
        self.assertEqual([], roadmap.merge(tasks, roadmap.parse(text)[0]))

    def test_prose_is_not_mistaken_for_exit_or_dependency_lines(self):
        text = ('# S\n\nExit the wizard when saved\n\n- [ ] Item\n  The result depends on the input format.\n'
                '- TODO: Other. Depends on: P01\n')
        stories, _ = roadmap.parse(text)
        self.assertEqual(2, len(stories))
        self.assertEqual(list(roadmap.DEFAULT_CRITERIA), stories[0]['acceptanceCriteria'])
        self.assertIn('depends on the input format', stories[0]['description'])

    def test_exit_and_dependency_lines_are_recognised_in_their_written_forms(self):
        for line in ('Exit: done', '**Exit:** done', '**Exit**: done', 'Exit criteria: done', '**Exit checks:** done'):
            with self.subTest(line):
                self.assertIn('done', roadmap.parse(f'# S\n\n{line}\n\n- [ ] Item\n')[0][0]['acceptanceCriteria'])
        for line in ('Depends on: P1.', '**Depends on:** P1', '**Depends on**: P1. **Areas:** x.'):
            with self.subTest(line):
                text = f'# P1\n\n- [ ] P1.1 First\n\n# P2\n\n{line}\n\n- [ ] P2.1 Second\n'
                self.assertEqual(['P1.1'], roadmap.parse(text)[0][1]['dependsOn'])

    def test_byte_order_mark_does_not_hide_the_first_heading(self):
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / 'R.md').write_bytes('﻿# P1 Core\n\n- [ ] Item\n'.encode('utf-8'))
            stories, summary = roadmap.parse_file(scratch, 'R.md')
            self.assertEqual(1, summary['sections'])
            self.assertEqual('P1 Core', stories[0]['source']['section'])


class RoadmapTickTests(unittest.TestCase):
    def tick(self, content, line):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'R.md'
            path.write_bytes(content)
            result = prd.tick_roadmap(scratch, {'source': {'path': 'R.md', 'line': line}})
            return result, path.read_bytes()

    def test_todo_bullet_becomes_a_ticked_checkbox(self):
        self.assertEqual((True, b'# R\r\n\r\n- [x] Do it\r\n- TODO: Later\r\n'),
                         self.tick(b'# R\r\n\r\n- TODO: Do it\r\n- TODO: Later\r\n', 'Do it'))
        self.assertEqual((True, b'* [x] Do it\n'), self.tick(b'* fixme - Do it\n', 'Do it'))

    def test_ticked_todo_bullet_keeps_its_task_id(self):
        before = roadmap.parse('- TODO: Do it\n')[0][0]['id']
        self.assertEqual(before, roadmap.parse(self.tick(b'- TODO: Do it\n', 'Do it')[1].decode('utf-8'))[0][0]['id'])

    def test_line_already_ticked_counts_as_done_and_stays_the_same(self):
        self.assertEqual((True, b'- [x] Do it\n'), self.tick(b'- [x] Do it\n', 'Do it'))

    def test_a_rewritten_line_is_not_found_and_stays_the_same(self):
        self.assertEqual((False, b'- [ ] Do it soon\n'), self.tick(b'- [ ] Do it soon\n', 'Do it'))

    def test_repeated_lines_are_ticked_one_at_a_time(self):
        self.assertEqual((True, b'- [x] Do it\n- [ ] Do it\n'), self.tick(b'- [ ] Do it\n- [ ] Do it\n', 'Do it'))
        self.assertEqual((True, b'- [x] Do it\n- [x] Do it\n'), self.tick(b'- [x] Do it\n- [ ] Do it\n', 'Do it'))


if __name__ == '__main__':
    unittest.main()
