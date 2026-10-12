"""Exercise installation against an empty home; never touch real accounts or Herdr."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from router import cli, model, onboarding


class OnboardingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.repo = (Path(self.temp.name) / 'checkout with spaces').resolve()
        (self.repo / 'bin').mkdir(parents=True)
        (self.repo / 'bin/herdr-router').write_text('#!/usr/bin/env python3\n')
        (self.repo / 'skill').mkdir()
        (self.repo / 'skill/SKILL.md').write_text('---\nname: herdr-route\ndescription: Delegate work through Herdr\n---\n')
        self.policy = model.load_default()
        self.policy['terminal_language'] = 'en'
        self.binaries = {'claude', 'codex', 'cursor-agent', 'kimi', 'opencode', 'droid', 'herdr'}
        for patch in [mock.patch.object(Path, 'home', return_value=self.home),
                      mock.patch.object(model, 'REPO_ROOT', self.repo),
                      mock.patch.object(model, 'load', return_value=self.policy),
                      mock.patch.dict(os.environ, {'PATH': '/fake/bin', 'HERDR_ROUTER_LANGUAGE': 'en'}, clear=True),
                      mock.patch.object(onboarding.shutil, 'which', side_effect=self.which),
                      mock.patch.object(onboarding, 'run_herdr', side_effect=self.herdr)]:
            patch.start()
            self.addCleanup(patch.stop)
        self.herdr_calls = []

    def which(self, binary, **kwargs):
        if binary == 'herdr-router' and (self.home / '.local/bin/herdr-router').exists():
            return str(self.home / '.local/bin/herdr-router')
        return '/fake/bin/' + binary if binary in self.binaries else None

    def herdr(self, binary, *args):
        self.herdr_calls.append(args)
        if args[:2] == ('plugin', 'link'):
            path = self.home / '.config/herdr/plugins.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([{'plugin_id': 'herdr-router', 'plugin_root': str(self.repo), 'enabled': True}]))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        self.assertEqual(err.getvalue(), '')
        return code, out.getvalue()

    def report(self, *args):
        code, out = self.run_cli(*args, '--json')
        data = json.loads(out)
        return code, data, {row['id']: row for row in data['checks']}

    def snapshot(self):
        return {str(p.relative_to(self.home)): (
            ('link', os.readlink(p)) if p.is_symlink() else ('file', p.read_bytes()) if p.is_file() else ('dir', None))
            for p in self.home.rglob('*')}

    def test_fresh_install_all_agents_and_read_only_doctor(self):
        code, data, rows = self.report('setup')
        self.assertEqual(code, 0, data)
        self.assertTrue(data['ok'])
        for kind in onboarding.SKILL_DIRS:
            self.assertEqual(rows['skill:' + kind]['status'], 'ok')
            self.assertEqual(onboarding.skill_path(kind).resolve(), self.repo / 'skill')
        self.assertEqual((self.home / '.local/bin/herdr-router').resolve(), self.repo / 'bin/herdr-router')
        config = tomllib.loads(onboarding.herdr_config().read_text())
        self.assertEqual(len(config['keys']['command']), 3)
        self.assertEqual(len(list(self.home.rglob('herdr-route'))), 3)
        before = self.snapshot()
        self.herdr_calls.clear()
        code, data, _ = self.report('doctor')
        self.assertEqual(code, 0, data)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.herdr_calls, [])

    def test_repeated_setup_does_not_add_bindings_backups_or_relink_plugin(self):
        self.report('setup')
        before = self.snapshot()
        self.herdr_calls.clear()
        code, data, _ = self.report('setup')
        self.assertEqual(code, 0, data)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.herdr_calls, [])

    def test_fresh_doctor_reports_missing_without_making_directories(self):
        before = self.snapshot()
        code, data, rows = self.report('doctor')
        self.assertEqual(code, 1)
        self.assertFalse(data['ok'])
        self.assertEqual(rows['skill:codex']['status'], 'missing')
        self.assertEqual(rows['plugin']['status'], 'missing')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.herdr_calls, [])

    def test_conflicting_skills_and_broken_links_are_preserved(self):
        path = onboarding.skill_path('claude')
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('private skill')
        broken = onboarding.skill_path('codex')
        broken.parent.mkdir(parents=True)
        broken.symlink_to(self.home / 'deleted checkout')
        cli_link = self.home / '.local/bin/herdr-router'
        cli_link.parent.mkdir(parents=True)
        cli_link.write_text('existing command')
        code, _, rows = self.report('setup')
        self.assertEqual(code, 1)
        self.assertEqual(rows['cli']['status'], 'conflict')
        self.assertEqual(rows['skill:claude']['status'], 'conflict')
        self.assertEqual(rows['skill:codex']['status'], 'conflict')
        self.assertEqual((path / 'SKILL.md').read_text(), 'private skill')
        self.assertEqual(os.readlink(broken), str(self.home / 'deleted checkout'))
        self.assertEqual(cli_link.read_text(), 'existing command')

    def test_keys_in_other_categories_are_preserved_and_config_backed_up(self):
        path = onboarding.herdr_config()
        path.parent.mkdir(parents=True)
        original = b'# private\n[ui]\ntheme="dark"\n[[keys.pane]]\nkey="prefix+D"\ncommand="close"\n'
        path.write_bytes(original)
        code, _, rows = self.report('setup')
        self.assertEqual(code, 1)
        self.assertEqual(rows['key:prefix+d']['status'], 'conflict')
        saved = tomllib.loads(path.read_text())
        self.assertEqual(saved['keys']['pane'][0]['command'], 'close')
        self.assertEqual(len(saved['keys']['command']), 2)
        backups = list(path.parent.glob('config.toml.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_custom_binding_does_not_gain_a_second_shortcut(self):
        path = onboarding.herdr_config()
        path.parent.mkdir(parents=True)
        path.write_text('[[keys.command]]\nkey="prefix+z"\ntype="plugin_action"\ncommand="herdr-router.route"\n')
        code, data, rows = self.report('setup')
        self.assertEqual(code, 0, data)
        self.assertEqual(rows['key:prefix+d']['status'], 'ok')
        self.assertEqual([e['key'] for e in tomllib.loads(path.read_text())['keys']['command']],
                         ['prefix+z', 'prefix+ctrl+d', 'prefix+shift+e'])

    def test_invalid_toml_is_preserved_and_unaffected_skills_are_installed(self):
        path = onboarding.herdr_config()
        path.parent.mkdir(parents=True)
        path.write_text('[broken')
        code, _, rows = self.report('setup')
        self.assertEqual(code, 1)
        self.assertEqual(rows['shortcuts']['status'], 'error')
        self.assertEqual(rows['skill:codex']['status'], 'ok')
        self.assertEqual(path.read_text(), '[broken')

    def test_environment_overrides_and_explicit_agents(self):
        config = self.home / 'custom/herdr.toml'
        with mock.patch.dict(os.environ, {'HERDR_CONFIG_PATH': str(config),
                                         'CLAUDE_CONFIG_DIR': str(self.home / 'custom/claude')}):
            code, data, rows = self.report('setup', '--agent', 'claude', '--agent', 'opencode')
            self.assertEqual(code, 0, data)
            self.assertTrue(config.exists())
            self.assertTrue((self.home / 'custom/claude/skills/herdr-route').is_symlink())
            self.assertTrue((self.home / '.agents/skills/herdr-route').is_symlink())
            self.assertNotIn('skill:codex', rows)

    def test_config_symlink_stays_a_symlink(self):
        target = self.home / 'dotfiles/herdr.toml'
        target.parent.mkdir()
        target.write_text('# dotfiles\n')
        path = onboarding.herdr_config()
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        code, data, _ = self.report('setup')
        self.assertEqual(code, 0, data)
        self.assertTrue(path.is_symlink())
        self.assertEqual(len(tomllib.loads(target.read_text())['keys']['command']), 3)

    def test_skip_flags_do_not_touch_plugin_or_config(self):
        code, data, rows = self.report('setup', '--no-shortcuts', '--no-plugin', '--agent', 'codex')
        self.assertEqual(code, 0, data)
        self.assertEqual(rows['plugin']['status'], 'skipped')
        self.assertEqual(rows['shortcuts']['status'], 'skipped')
        self.assertFalse(onboarding.herdr_config().exists())
        self.assertEqual(self.herdr_calls, [])

    def test_no_clis_or_herdr_returns_actionable_failure(self):
        self.binaries.clear()
        code, data, rows = self.report('doctor')
        self.assertEqual(code, 1)
        self.assertIn('install and sign in', rows['agents']['message'])
        self.assertEqual(rows['herdr']['status'], 'missing')
        self.assertFalse(data['ok'])

    def test_disabled_or_other_checkout_plugin_is_preserved(self):
        path = self.home / '.config/herdr/plugins.json'
        path.parent.mkdir(parents=True)
        original = json.dumps([{'plugin_id': 'herdr-router', 'plugin_root': str(self.repo), 'enabled': False}])
        path.write_text(original)
        code, _, rows = self.report('setup')
        self.assertEqual(code, 1)
        self.assertEqual(rows['plugin']['status'], 'conflict')
        self.assertEqual(path.read_text(), original)
        self.assertNotIn(('plugin', 'link', str(self.repo)), self.herdr_calls)

    def test_reload_failure_is_a_warning_and_config_is_saved(self):
        def call(binary, *args):
            if args[:2] == ('server', 'reload-config'):
                raise subprocess.TimeoutExpired(args, 15)
            self.herdr(binary, *args)
        with mock.patch.object(onboarding, 'run_herdr', side_effect=call):
            code, data, rows = self.report('setup')
        self.assertEqual(code, 0, data)
        self.assertEqual(rows['reload']['status'], 'warning')
        self.assertTrue(onboarding.herdr_config().exists())

    def test_invalid_policy_is_reported_instead_of_crashing(self):
        with mock.patch.object(model, 'load', side_effect=ValueError('broken policy')):
            code, _, rows = self.report('doctor')
        self.assertEqual(code, 1)
        self.assertEqual(rows['policy']['status'], 'error')
        self.assertIn('broken policy', rows['policy']['message'])

    def test_first_use_hint_and_machine_output(self):
        out = io.StringIO()
        out.isatty = lambda: True
        cli.print_setup_hint(self.policy, 'en', out)
        self.assertIn('setup', out.getvalue())
        self.assertIn('codex', out.getvalue())
        self.report('setup')
        out.seek(0)
        out.truncate()
        cli.print_setup_hint(self.policy, 'en', out)
        self.assertEqual(out.getvalue(), '')
        with mock.patch.object(cli, 'print_setup_hint') as hint, \
             mock.patch.object(cli, 'route_task') as route:
            route.return_value.to_dict.return_value = {'chosen': 'test'}
            code, result = self.run_cli('route', 'rename a variable', '--json', '--no-systemone')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(result), {'chosen': 'test'})
        hint.assert_not_called()

    def test_missing_skill_source_does_not_install_invalid_skills(self):
        (self.repo / 'skill/SKILL.md').unlink()
        code, _, rows = self.report('setup')
        self.assertEqual(code, 1)
        self.assertEqual(rows['skill-source']['status'], 'error')
        self.assertEqual(rows['skill:codex']['status'], 'error')
        self.assertFalse(onboarding.skill_path('codex').exists())

    def test_plugin_link_failure_does_not_prevent_other_integrations(self):
        with mock.patch.object(onboarding, 'run_herdr', side_effect=OSError('link failed')):
            code, _, rows = self.report('setup')
        self.assertEqual(code, 1)
        self.assertEqual(rows['plugin']['status'], 'error')
        self.assertEqual(rows['skill:codex']['status'], 'ok')
        self.assertTrue(onboarding.herdr_config().exists())

    def test_doctor_missing_broken_link_and_conflict_is_read_only(self):
        path = onboarding.skill_path('codex')
        path.parent.mkdir(parents=True)
        path.symlink_to(self.home / 'missing')
        before = self.snapshot()
        code, _, rows = self.report('doctor')
        self.assertEqual(code, 1)
        self.assertEqual(rows['skill:codex']['status'], 'conflict')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.herdr_calls, [])

    def test_real_entrypoint_on_a_clean_machine_with_fake_clis(self):
        fake_bin = self.home / 'fake-bin'
        fake_bin.mkdir()
        fake_herdr = fake_bin / 'herdr'
        fake_herdr.write_text('#!' + sys.executable + '\n' + """import json, sys
from pathlib import Path
if sys.argv[1:3] == ['plugin', 'link']:
    path = Path.home() / '.config/herdr/plugins.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{'plugin_id': 'herdr-router', 'plugin_root': sys.argv[3], 'enabled': True}]))
""")
        fake_herdr.chmod(0o755)
        (fake_bin / 'codex').write_text('#!' + sys.executable + '\n')
        (fake_bin / 'codex').chmod(0o755)
        policy = dict(self.policy, env={'extra_path': []})
        policy_path = self.home / 'policy.json'
        policy_path.write_text(json.dumps(policy))
        env = {'HOME': str(self.home), 'PATH': str(fake_bin), 'HERDR_ROUTER_LANGUAGE': 'en'}
        entrypoint = Path(__file__).resolve().parent.parent / 'bin/herdr-router'
        command = [sys.executable, str(entrypoint), '--policy', str(policy_path)]
        setup = subprocess.run([*command, 'setup', '--json'], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(setup.returncode, 0, setup.stderr + setup.stdout)
        report = json.loads(setup.stdout)
        self.assertTrue(report['ok'])
        self.assertEqual([r['id'] for r in report['checks'] if r['id'].startswith('skill:')], ['skill:codex'])
        self.assertEqual((self.home / '.agents/skills/herdr-route').resolve(), entrypoint.parent.parent / 'skill')
        before = self.snapshot()
        env['PATH'] += os.pathsep + str(self.home / '.local/bin')
        doctor = subprocess.run([*command, 'doctor', '--json'], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)
        self.assertTrue(json.loads(doctor.stdout)['ok'])
        self.assertEqual(self.snapshot(), before)

    def test_chinese_report(self):
        with mock.patch.dict(os.environ, {'HERDR_ROUTER_LANGUAGE': 'zh'}):
            code, result = self.run_cli('doctor')
        self.assertEqual(code, 1)
        self.assertIn('运行 herdr-router setup', result)


if __name__ == '__main__':
    unittest.main()
