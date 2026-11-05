"""Package checks: valid skill, working installer, clean sources, safe prompt rendering."""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import ROOT, SCRIPTS, SKILL
from autopilot_core import conventions, doctor, preflight, processes, project as project_module, quality, routing, runner

REQUIRED_REFERENCES = ('architecture', 'project-analysis', 'roadmap', 'task-preflight', 'model-routing',
                       'ralph-integration', 'persistent-memory', 'ajimu-conventions', 'git-workflow', 'safety',
                       'supervision', 'verification')


def run_tool(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)


class PackageTests(unittest.TestCase):
    def test_skill_package_is_valid(self):
        result = run_tool(ROOT / 'tools/validate_skill.py', ROOT)
        self.assertEqual(0, result.returncode, result.stdout)

    def test_frontmatter_requires_explicit_invocation(self):
        text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        self.assertTrue(text.startswith('---\nname: ajimu-autopilot-lite\n'))
        self.assertIn('disable-model-invocation: true', text.split('---')[1])
        self.assertLess(len(text.splitlines()), 150, 'details belong in references/')

    def test_expected_structure_exists(self):
        for name in REQUIRED_REFERENCES:
            self.assertTrue((SKILL / 'references' / f'{name}.md').is_file(), name)
        for path in ('scripts/autopilot.py', 'assets/prompts/preflight.md', 'assets/prompts/implementation.md',
                     'assets/prompts/final_fix.md', 'assets/prompts/review.md', 'assets/prompts/system_rules.md', 'assets/schemas/preflight.schema.json',
                     'assets/schemas/implementation.schema.json', 'assets/schemas/review.schema.json'):
            self.assertTrue((SKILL / path).is_file(), path)
        for path in ('README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore'):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_sources_parse_for_python_3_9(self):
        for path in list(SCRIPTS.rglob('*.py')) + list((ROOT / 'tools').glob('*.py')):
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3, 9))

    @unittest.skipUnless(hasattr(sys, 'stdlib_module_names'), 'needs Python 3.10 or later to list the standard library')
    def test_only_the_standard_library_is_imported(self):
        allowed = set(sys.stdlib_module_names) | {'autopilot_core'}
        for path in SCRIPTS.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
                for name in names:
                    self.assertIn(name.split('.')[0], allowed, f'{path}: {name}')

    def test_no_forbidden_switches_in_the_runner(self):
        # project.py names the bypass flag only in the list of configurations it rejects.
        for path in SCRIPTS.rglob('*.py'):
            source = path.read_text(encoding='utf-8')
            words = ["'--force'", "'-f'", "'--no-verify'", "'rebase'", "'--hard'"]
            if path.name != 'project.py':
                words.append("'--dangerously-skip-permissions'")
            for word in words:
                self.assertNotIn(word, source, f'{path} uses {word}')

    def test_every_prompt_marker_is_filled(self):
        for name, expected in (('preflight', {'version', 'task_id', 'task_json', 'dependencies', 'source', 'docs', 'checks',
                                              'recent_commits', 'hint'}),
                               ('implementation', {'version', 'task_id', 'attempt', 'max_attempts', 'task_json', 'preflight',
                                                   'findings', 'progress', 'checks', 'final_checks', 'roadmap', 'changelog'}),
                               ('final_fix', {'attempt', 'max_attempts', 'findings', 'tasks', 'progress', 'checks',
                                              'changelog'}),
                               ('review', {'version', 'task_id', 'mode', 'task_json', 'changed', 'check_results'})):
            self.assertEqual(expected, set(re.findall(r'\{\{(\w+)\}\}', preflight.load_prompt(name))), name)

    def test_task_text_cannot_inject_prompt_markers(self):
        rendered = preflight.render('{{a}} {{b}}', a='{{b}}', b='SECRET')
        self.assertEqual('{{b}} SECRET', rendered)

    def test_prompts_and_schemas_offer_no_haiku(self):
        schema = preflight.load_schema('preflight')
        self.assertEqual(['sonnet', 'opus'], schema['properties']['recommended_model']['enum'])
        for name in ('preflight', 'implementation', 'review', 'system_rules'):
            self.assertNotIn('haiku', preflight.load_prompt(name).lower())

    def test_preflight_schema_rows_match_the_policy(self):
        schema = preflight.load_schema('preflight')
        self.assertEqual(sorted(routing.POLICY), sorted(schema['properties']['task_type']['enum']))
        prompt = preflight.load_prompt('preflight')
        for task_type, (model, effort) in routing.POLICY.items():
            self.assertIn(f'| {task_type} | {model} | {effort} |', prompt)

    def test_documented_commands_exist_in_the_cli(self):
        help_text = run_tool(SCRIPTS / 'autopilot.py', '--help').stdout
        for path in (ROOT / 'README.md', SKILL / 'SKILL.md', SKILL / 'references' / 'supervision.md'):
            for command in set(re.findall(r'autopilot\.py"? ([a-z-]+)', path.read_text(encoding='utf-8'))):
                self.assertIn(command, help_text, f'{path.name}: {command}')

    def test_a_supervised_run_points_at_a_guide_that_exists(self):
        self.assertTrue(runner.SUPERVISION_GUIDE.is_file())


class InstallerTests(unittest.TestCase):
    def test_install_is_idempotent_and_keeps_foreign_files(self):
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / 'ajimu-autopilot-lite'
            self.assertEqual(1, run_tool(ROOT / 'tools/install.py', '--target', target, '--check').returncode)
            self.assertFalse(target.exists())
            first = run_tool(ROOT / 'tools/install.py', '--target', target)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertTrue((target / 'SKILL.md').is_file())
            self.assertFalse(list(target.rglob('__pycache__')))
            (target / 'local-note.md').write_text('mine', encoding='utf-8')
            second = run_tool(ROOT / 'tools/install.py', '--target', target)
            self.assertIn('already up to date', second.stdout)
            self.assertIn('kept: local-note.md', second.stdout)
            self.assertEqual(0, run_tool(ROOT / 'tools/install.py', '--target', target, '--check').returncode)
            installed = run_tool(target / 'scripts/autopilot.py', 'route', '--task-type', 'bug_complex')
            self.assertEqual(('sonnet', 'high'), tuple(json.loads(installed.stdout)[k] for k in ('model', 'effort')))


class ConventionsTests(unittest.TestCase):
    def fake_skill(self, root):
        folder = Path(root) / '.claude' / 'skills' / conventions.SKILL
        (folder / 'scripts').mkdir(parents=True)
        (folder / 'SKILL.md').write_text('rules', encoding='utf-8')
        (folder / conventions.VALIDATOR).write_text('import sys; sys.exit(0)', encoding='utf-8')
        return folder

    def test_project_copy_wins_over_personal_copy(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as home:
            self.fake_skill(home)
            local = self.fake_skill(project)
            self.assertEqual(local, conventions.locate(project, home).root)

    def test_missing_skill_is_reported_as_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(conventions.locate(None, home))

    def test_newest_install_in_the_plugin_cache_wins(self):
        with tempfile.TemporaryDirectory() as home:
            for stamp, version in enumerate(('1.0.0', '1.2.0', '1.10.0'), 1000):
                folder = Path(home) / '.claude/plugins/cache/market/ajimu-conventions' / version / 'skills' / conventions.SKILL
                (folder / 'scripts').mkdir(parents=True)
                (folder / 'SKILL.md').write_text(version, encoding='utf-8')
                (folder / conventions.VALIDATOR).write_text('import sys; sys.exit(0)', encoding='utf-8')
                os.utime(folder / 'SKILL.md', (stamp, stamp))
            self.assertEqual('1.10.0', conventions.locate(None, home).text)

    @unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
    def test_the_official_validator_decides(self):
        found = conventions.locate()
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'message'
            path.write_text('Keep drafts during polling\n\nTask: T-1\n', encoding='utf-8')
            self.assertTrue(conventions.check_message(found, path)[0])
            path.write_text('Keep drafts\n\nCo-Authored-By: A <a@example.invalid>\n', encoding='utf-8')
            self.assertFalse(conventions.check_message(found, path)[0])

    @unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
    def test_task_ids_the_validator_refuses_are_named(self):
        found = conventions.locate()
        self.assertEqual([], conventions.refused_task_ids(found, ['P01.1', 'RM-abcd1234']))
        self.assertEqual(['AI-3'], conventions.refused_task_ids(found, ['P01.1', 'AI-3']))


class ProcessTests(unittest.TestCase):
    def sleeper(self):
        return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **processes.GROUP_OPTIONS)

    def test_outcomes_of_a_bounded_wait(self):
        finished = subprocess.Popen([sys.executable, '-c', ''], **processes.GROUP_OPTIONS)
        self.assertEqual('exited', processes.wait_bounded(finished, 60))
        timed_out = self.sleeper()
        self.assertEqual('timed_out', processes.wait_bounded(timed_out, 1))
        self.assertIsNotNone(timed_out.poll())
        stopped = self.sleeper()
        self.assertEqual('stopped', processes.wait_bounded(stopped, 60, lambda: True))
        self.assertIsNotNone(stopped.poll())

    def test_an_interrupt_while_waiting_ends_the_process(self):
        def interrupt():
            raise KeyboardInterrupt
        process = self.sleeper()
        with self.assertRaises(KeyboardInterrupt):
            processes.wait_bounded(process, 60, interrupt)
        self.assertIsNotNone(process.poll())


class CheckTests(unittest.TestCase):
    def check(self, source):
        return {'name': 'c', 'command': f'"{sys.executable}" -c "{source}"'}

    def test_timeout_ends_the_descendants_of_the_shell(self):
        with tempfile.TemporaryDirectory() as scratch:
            started = time.monotonic()
            report = quality.run_checks(
                scratch, [self.check("import pathlib,time; print('begun', flush=True); time.sleep(5); pathlib.Path('m').touch()")], 1)
            self.assertLess(time.monotonic() - started, 4.5, 'the wait must not outlast the timeout')
            result = report['results'][0]
            self.assertEqual((False, None), (result['passed'], result['exit_code']))
            self.assertIn('timed out', result['output_tail'])
            self.assertIn('begun', result['output_tail'])
            time.sleep(5)
            self.assertFalse((Path(scratch) / 'm').exists(), 'the check went on after its timeout')

    def test_stop_request_ends_a_running_check(self):
        answers = iter((False, False, True))
        with tempfile.TemporaryDirectory() as scratch:
            started = time.monotonic()
            report = quality.run_checks(scratch, [self.check('import time; time.sleep(30)')], 60, lambda: next(answers))
            self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(report['stopped'])
        self.assertEqual([], report['results'])

    def test_output_keeps_both_streams_and_the_exit_code(self):
        with tempfile.TemporaryDirectory() as scratch:
            report = quality.run_checks(scratch, [self.check("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")], 60)
        result = report['results'][0]
        self.assertEqual((False, 3), (result['passed'], result['exit_code']))
        self.assertIn('out', result['output_tail'])
        self.assertIn('err', result['output_tail'])
        self.assertFalse(report['ok'])

    def test_a_check_reading_input_is_not_left_waiting(self):
        with tempfile.TemporaryDirectory() as scratch:
            report = quality.run_checks(scratch, [self.check('import sys; sys.exit(0 if sys.stdin.read() == str() else 1)')], 20)
        self.assertTrue(report['results'][0]['passed'], report)


class SettingsTests(unittest.TestCase):
    def test_settings_file_that_is_not_an_object_is_ignored(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / '.claude').mkdir()
            (root / '.claude/settings.json').write_text('[1]', encoding='utf-8')
            doctor.check_settings(project_module.Project(root), doctor.Report())

    def test_project_folder_name_does_not_make_its_settings_managed(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'managed-app'
            (root / '.claude').mkdir(parents=True)
            (root / '.claude/settings.json').write_text('{"availableModels": ["haiku"]}', encoding='utf-8')
            report = doctor.Report()
            doctor.check_settings(project_module.Project(root), report)
        self.assertNotIn('managed availableModels', [item['name'] for item in report.items])


class ProjectTests(unittest.TestCase):
    def test_check_detection(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            self.assertEqual([], project_module.detect_checks(root))
            (root / 'package.json').write_text(json.dumps({'scripts': {
                'test': 'vitest', 'lint': 'eslint .', 'build': 'vite build', 'dev': 'vite'}}), encoding='utf-8')
            commands = [check['command'] for check in project_module.detect_checks(root)]
            self.assertEqual(['npm run lint', 'npm run build', 'npm test'], commands)

    def test_placeholder_npm_test_is_not_a_check(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'package.json').write_text(json.dumps({'scripts': {
                'test': 'echo "Error: no test specified" && exit 1'}}), encoding='utf-8')
            self.assertEqual([], project_module.detect_checks(root))

    def test_permission_rules_stay_narrow(self):
        checks = [{'name': 'a', 'command': 'python -m unittest discover -s tests'}, {'name': 'b', 'command': 'npm run lint'},
                  {'name': 'c', 'command': 'pytest'}, {'name': 'd', 'command': 'make a && make b'}]
        self.assertEqual(['python -m unittest *', 'npm run lint *', 'pytest *'], project_module.bash_rules_for_checks(checks))

    def test_unusable_usage_retry_interval_is_rejected(self):
        from autopilot_core import project as project_module
        config = project_module.merge_defaults({}, project_module.DEFAULT_CONFIG)
        self.assertEqual(5, config['limits']['usage_retry_minutes'])
        config['limits']['usage_retry_minutes'] = 0
        self.assertTrue([p for p in project_module.validate_config(config) if 'usage_retry_minutes' in p])

    def test_unlimited_or_unsafe_configuration_is_rejected(self):
        config = project_module.merge_defaults({}, project_module.DEFAULT_CONFIG)
        self.assertEqual([], project_module.validate_config(config))
        config['limits']['max_iterations'] = 0
        config['permissions']['extra_allow'] = ['Bash']
        config['permissions']['bash_allow'] = ['*']
        problems = ' '.join(project_module.validate_config(config))
        for expected in ('max_iterations', 'extra_allow', 'bash_allow'):
            self.assertIn(expected, problems)

    def test_malformed_checks_and_limits_are_rejected(self):
        for checks in (['npm test'], [{'name': 'test'}], [{'name': '', 'command': 'x'}], 'npm test', 5):
            with self.subTest(checks=checks):
                config = project_module.merge_defaults({'checks': checks}, project_module.DEFAULT_CONFIG)
                self.assertTrue([p for p in project_module.validate_config(config) if p.startswith('checks')])
        config = project_module.merge_defaults({}, project_module.DEFAULT_CONFIG)
        config['limits'].update(preflight_max_rounds='3', keep_session_logs=-1)
        problems = ' '.join(project_module.validate_config(config))
        for expected in ('preflight_max_rounds', 'keep_session_logs'):
            self.assertIn(expected, problems)

    def test_setup_explains_malformed_checks(self):
        with tempfile.TemporaryDirectory() as scratch:
            project_module.write_json_atomic(Path(scratch) / '.ajimu-autopilot/config.json', {'checks': ['npm test']})
            with self.assertRaisesRegex(ValueError, 'checks must be'):
                project_module.setup(scratch)

    def test_roadmap_paths_are_kept_inside_the_project(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'project'
            root.mkdir()
            self.assertEqual('docs/R.md', project_module.project_relative(root, 'docs/../docs/./R.md'))
            self.assertEqual('R.md', project_module.project_relative(root, root / 'R.md'))
            for outside in ('../R.md', Path(scratch) / 'R.md'):
                with self.subTest(outside=outside), self.assertRaisesRegex(ValueError, 'inside the project'):
                    project_module.project_relative(root, outside)

    def test_progress_keeps_patterns_on_top_without_duplicates(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'progress.txt'
            project_module.add_progress(path, '2026-01-01T00:00:00Z - A', ['- first'], ['Use X for Y'])
            project_module.add_progress(path, '2026-01-02T00:00:00Z - B', ['- second'], ['Use X for Y', 'Use Z'])
            text = path.read_text(encoding='utf-8')
            self.assertEqual(1, text.count('- Use X for Y'))
            self.assertLess(text.index('- Use Z'), text.index('- first'))
            context = project_module.progress_context(path, recent_entries=1)
            self.assertIn('Use X for Y', context)
            self.assertIn('- second', context)
            self.assertNotIn('- first', context)


class ScanTests(unittest.TestCase):
    def test_secret_patterns(self):
        hits = quality.scan_text_for_secrets('key = "ghp_' + 'a' * 36 + '"\n-----BEGIN RSA PRIVATE KEY-----')
        self.assertIn('GitHub token', hits)
        self.assertIn('private key', hits)
        self.assertEqual([], quality.scan_text_for_secrets('password = input("Password: ")'))

    def test_sensitive_paths(self):
        for path in ('.env', 'config/.env.production', 'id_rsa', 'cert.pem', '.claude/settings.local.json'):
            self.assertTrue(quality.SENSITIVE_PATH.search(path), path)
        for path in ('.env.example', 'src/environment.py', 'docs/keys.md'):
            self.assertFalse(quality.SENSITIVE_PATH.search(path), path)

    def test_audit_tool_flags_private_material(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'clean.md').write_text('Nothing here.\n', encoding='utf-8')
            self.assertEqual(0, run_tool(ROOT / 'tools/audit_publication.py', root).returncode)
            (root / '.env').write_text('A=1\n', encoding='utf-8')
            (root / 'notes.md').write_text('See C:' + '\\Users\\someone\\project\n', encoding='utf-8')
            result = run_tool(ROOT / 'tools/audit_publication.py', root)
            self.assertEqual(1, result.returncode)
            self.assertIn('.env', result.stdout)
            self.assertIn('personal Windows path', result.stdout)


if __name__ == '__main__':
    unittest.main()
