"""Real Git behavior in tiny temporary repositories; no external agents or network."""
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from router import workspace as w


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.state = self.tmp / 'state'
        self.stack.enter_context(mock.patch.dict(os.environ, {'HERDR_PLUGIN_STATE_DIR': str(self.state)}))
        self.repo = self.tmp / "源 repo ' $(touch unsafe)"
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'sub').mkdir()
        (self.repo / 'sub' / 'file').write_text('初始\n')
        (self.repo / '.gitignore').write_text('assets/\nbuild/\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', '-C', str(cwd or self.repo), *args],
                                       env=w._env(), stderr=subprocess.DEVNULL).decode().strip()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(w.WorkspaceError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def create(self, cwd=None):
        return w.materialize(w.inspect(str(cwd or self.repo), isolation='worktree'), str(uuid.uuid4()))

    def test_shared_never_queries_git_even_dirty_nonrepo(self):
        (self.repo / 'dirty').write_text('local')
        with mock.patch.object(w, '_git', side_effect=AssertionError('Git forbidden')):
            for cwd in (self.repo, self.tmp):
                plan = w.inspect(str(cwd))
                self.assertEqual(plan['mode'], 'shared')
                self.assertNotIn('snapshot', plan)
                self.assertEqual(w.materialize(plan, 'unused'), {'kind': 'directory', 'cwd': str(cwd)})
        self.assertFalse(self.state.exists())

    def test_real_create_registration_childdir_and_reuse(self):
        assets = self.repo / 'assets'
        assets.mkdir()
        (assets / 'local').write_bytes(b'ignored asset')
        before = (self.repo / 'sub' / 'file').read_bytes()
        plan = w.inspect(str(self.repo / 'sub'), isolation='worktree')
        ident = str(uuid.uuid4())
        checkpoint = []
        meta = w.materialize(plan, ident, before_create=lambda value: checkpoint.append(value))
        self.assertEqual(meta['branch'], 'router/' + ident)
        self.assertEqual(Path(meta['path']).name, ident)
        self.assertEqual(Path(meta['cwd']), Path(meta['path']) / 'sub')
        self.assertEqual(meta['state'], 'ready')
        self.assertTrue(w.validate_metadata(meta, meta['cwd']))
        self.assertEqual(checkpoint[0]['state'], 'creating')
        self.assertFalse((Path(meta['path']) / 'assets').exists())
        self.assertEqual((self.repo / 'sub' / 'file').read_bytes(), before)
        self.assertEqual((assets / 'local').read_bytes(), b'ignored asset')
        self.assertIn('branch refs/heads/' + meta['branch'], self.git('worktree', 'list', '--porcelain'))
        (Path(meta['cwd']) / 'file').write_text('new commit')
        self.git('add', '.', cwd=meta['cwd'])
        self.git('commit', '-qm', 'next', cwd=meta['cwd'])
        (Path(meta['cwd']) / 'dirty').write_text('keep')
        reused = w.inspect(meta['cwd'], parent_workspace=meta)
        # Changes during model assessment are also allowed for a reused workspace.
        (Path(meta['cwd']) / 'another').write_text('new')
        with mock.patch.object(w, '_snapshot', side_effect=AssertionError('No HEAD/status scan')):
            self.assertEqual(w.materialize(reused, str(uuid.uuid4())), meta)
        self.assertEqual((Path(meta['cwd']) / 'dirty').read_text(), 'keep')
        self.assertEqual(w.inspect(meta['cwd'], isolation='worktree', parent_workspace=meta)['mode'], 'reuse')

    def test_nested_tracked_subdirectory_and_gitmodules_rejection(self):
        nested = self.repo / 'sub' / '深 directory' / 'nested'
        nested.mkdir(parents=True)
        (nested / 'tracked').write_text('content')
        self.git('add', '.')
        self.git('commit', '-qm', 'nested')
        meta = self.create(nested)
        self.assertEqual(meta['relative_cwd'], 'sub/深 directory/nested')
        self.assertEqual((Path(meta['cwd']) / 'tracked').read_text(), 'content')
        (self.repo / '.gitmodules').write_text('[submodule "old"]\n path = old\n url = unused\n')
        self.git('add', '.gitmodules')
        self.git('commit', '-qm', 'modules')
        self.error('workspace_unsupported', w.inspect, str(self.repo), isolation='worktree')

    def test_source_inside_real_submodule_is_rejected(self):
        child = self.tmp / 'child'
        child.mkdir()
        self.git('init', '-q', cwd=child)
        self.git('config', 'user.email', 'test@example.invalid', cwd=child)
        self.git('config', 'user.name', 'Test', cwd=child)
        (child / 'file').write_text('tracked')
        self.git('add', '.', cwd=child)
        self.git('commit', '-qm', 'child', cwd=child)
        self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', str(child), 'module')
        self.git('commit', '-qam', 'module')
        self.error('workspace_unsupported', w.inspect, str(self.repo / 'module'), isolation='worktree')

    def test_hooks_run_and_checkpoint_precedes_checkout(self):
        marker = self.tmp / 'hook-ran'
        hook = self.repo / '.git' / 'hooks' / 'post-checkout'
        hook.write_text('#!/bin/sh\nprintf hook > ' + "'" + str(marker) + "'" + '\n')
        hook.chmod(0o755)
        def checkpoint(meta):
            self.assertFalse(marker.exists())
            self.assertFalse(Path(meta['path']).exists())
        meta = w.materialize(w.inspect(str(self.repo), isolation='worktree'),
                             str(uuid.uuid4()), before_create=checkpoint)
        self.assertEqual(meta['state'], 'ready')
        self.assertEqual(marker.read_text(), 'hook')

    def test_json_metadata_always_returns_bool(self):
        meta = self.create()
        for key in meta:
            for bad in (None, [], {}, True, 4):
                self.assertIsInstance(w.validate_metadata({**meta, key: bad}, meta['cwd']), bool)
        for bad in (None, [], {}, True, 'text'):
            self.assertFalse(w.validate_metadata(bad, meta['cwd']))

    def test_dirty_unborn_nonrepo_bare_missing_sources(self):
        (self.repo / 'untracked').write_text('local')
        with mock.patch.object(w, '_supported', side_effect=AssertionError('Reject dirty source before index scan')):
            self.error('workspace_dirty', w.inspect, str(self.repo), isolation='worktree')
        (self.repo / 'untracked').unlink()
        (self.repo / 'sub' / 'file').write_text('changed')
        self.error('workspace_dirty', w.inspect, str(self.repo), isolation='worktree')
        unborn = self.tmp / 'unborn'
        unborn.mkdir()
        self.git('init', '-q', cwd=unborn)
        self.error('workspace_unborn', w.inspect, str(unborn), isolation='worktree')
        self.error('workspace_not_git', w.inspect, str(self.tmp), isolation='worktree')
        bare = self.tmp / 'bare'
        self.git('init', '--bare', '-q', str(bare))
        self.error('workspace_bare', w.inspect, str(bare), isolation='worktree')
        self.error('workspace_invalid_cwd', w.inspect, str(self.tmp / 'absent'))

    def test_sparse_submodule_and_ignored_directory_rejected(self):
        self.git('config', 'core.sparseCheckout', 'true')
        self.error('workspace_unsupported', w.inspect, str(self.repo), isolation='worktree')
        self.git('config', 'core.sparseCheckout', 'false')
        head = self.git('rev-parse', 'HEAD')
        self.git('update-index', '--add', '--cacheinfo', '160000,' + head + ',module')
        self.git('commit', '-qm', 'gitlink')
        # This synthetic gitlink has no checkout, so the source is dirty first.
        self.error('workspace_dirty', w.inspect, str(self.repo), isolation='worktree')
        self.error('workspace_unsupported', w._supported, str(self.repo))
        self.git('rm', '--cached', 'module')
        self.git('commit', '-qm', 'remove gitlink')
        (self.repo / 'build').mkdir()
        self.error('workspace_invalid_cwd', w.inspect, str(self.repo / 'build'), isolation='worktree')
        self.assertFalse(self.state.exists())

    def test_changed_source_before_creation(self):
        for change in ('status', 'head'):
            plan = w.inspect(str(self.repo), isolation='worktree')
            if change == 'status':
                (self.repo / 'local').write_text('new')
            else:
                self.git('commit', '--allow-empty', '-qm', 'next')
            self.error('workspace_source_changed', w.materialize, plan, str(uuid.uuid4()))
            self.assertFalse(self.state.exists())
            if change == 'status':
                (self.repo / 'local').unlink()

    def test_callback_failure_prevents_all_mutation(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        original = RuntimeError('checkpoint failed')
        def fail(meta):
            self.assertTrue(w.validate_metadata(meta, meta['cwd']))
            self.assertFalse(self.state.exists())
            raise original
        with self.assertRaises(RuntimeError) as caught:
            w.materialize(plan, str(uuid.uuid4()), before_create=fail)
        self.assertIs(caught.exception, original)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(self.git('worktree', 'list', '--porcelain').split('worktree ')), 2)

    def test_collisions_and_source_state_path_preserved(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        ident = str(uuid.uuid4())
        branch = 'router/' + ident
        self.git('branch', branch)
        self.error('workspace_collision', w.materialize, plan, ident)
        self.assertEqual(self.git('rev-parse', branch), plan['snapshot']['head'])
        self.git('branch', '-d', branch)
        captured = []
        def occupy(meta):
            captured.append(meta)
            Path(meta['path']).mkdir(parents=True)
            (Path(meta['path']) / 'keep').write_text('keep')
        self.error('workspace_collision', w.materialize, plan, ident, before_create=occupy)
        self.assertEqual((Path(captured[0]['path']) / 'keep').read_text(), 'keep')
        with mock.patch.dict(os.environ, {'HERDR_PLUGIN_STATE_DIR': str(self.repo / 'inside')}):
            self.error('workspace_invalid_path', w.inspect, str(self.repo), isolation='worktree')
            self.error('workspace_invalid_path', w.materialize, plan, str(uuid.uuid4()))
        self.assertFalse((self.repo / 'inside').exists())

    def test_parent_isolation_conflicts_and_identity_mismatch(self):
        directory = {'kind': 'directory', 'cwd': str(self.repo)}
        self.error('workspace_isolation_conflict', w.inspect, str(self.repo), isolation='worktree', parent_workspace=directory)
        meta = self.create()
        self.error('workspace_isolation_conflict', w.inspect, meta['cwd'], isolation='shared', parent_workspace=meta)
        for key in ('path', 'common_dir'):
            bad = {**meta, key: str(self.tmp / 'different')}
            code = 'workspace_invalid_metadata' if key == 'path' else 'workspace_identity_changed'
            self.error(code, w.inspect, meta['cwd'], parent_workspace=bad)
        self.git('checkout', '-qb', 'other', cwd=meta['cwd'])
        self.error('workspace_identity_changed', w.inspect, meta['cwd'], parent_workspace=meta)

    def test_pure_validation_and_environment(self):
        meta = self.create()
        with mock.patch('os.path.realpath', side_effect=AssertionError), mock.patch('os.stat', side_effect=AssertionError), mock.patch.object(w, '_git', side_effect=AssertionError):
            self.assertTrue(w.validate_metadata(meta, meta['cwd']))
            self.assertFalse(w.validate_metadata({**meta, 'relative_cwd': '../escape'}, meta['cwd']))
            self.assertFalse(w.validate_metadata({**meta, 'creation_ms': True}, meta['cwd']))
            self.assertFalse(w.validate_metadata({**meta, 'extra': 1}, meta['cwd']))
        with mock.patch.dict(os.environ, {'GIT_DIR': '/bad', 'GIT_INDEX_FILE': '/bad', 'GIT_LFS_SKIP_SMUDGE': '1'}):
            env = w._env()
            self.assertEqual({k for k in env if k.startswith('GIT_')}, {'GIT_TERMINAL_PROMPT', 'GIT_OPTIONAL_LOCKS'})
            self.assertEqual(w.inspect(str(self.repo), isolation='worktree')['mode'], 'create')

    def test_budget_includes_inspection_but_not_model_wait(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        plan['inspection_ms'] = 60000
        self.error('workspace_query_timeout', w.materialize, plan, str(uuid.uuid4()), timeout_ms=60000)
        self.assertFalse(self.state.exists())
        plan['inspection_ms'] = 0
        time.sleep(0.02)
        self.assertEqual(w.materialize(plan, str(uuid.uuid4()))['state'], 'ready')

    def test_successful_command_without_worktree_is_not_ready(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        original = subprocess.Popen
        def fake_add(argv, **kwargs):
            if 'worktree' in argv and 'add' in argv:
                return original(['git', '--version'], **kwargs)
            return original(argv, **kwargs)
        with mock.patch.object(w.subprocess, 'Popen', side_effect=fake_add):
            exc = self.error('worktree_create_failed', w.materialize, plan, str(uuid.uuid4()))
        self.assertEqual(exc.workspace['state'], 'failed')
        self.assertFalse(Path(exc.workspace['path']).exists())

    def test_read_query_has_small_timeout_and_no_diagnostics(self):
        original = subprocess.Popen
        def slow(argv, **kwargs):
            return original(['/bin/sh', '-c', 'sleep 10'], **kwargs)
        started = time.monotonic()
        with mock.patch.object(w.subprocess, 'Popen', side_effect=slow):
            exc = self.error('workspace_query_timeout', w.inspect, str(self.repo), isolation='worktree', timeout_ms=30)
        self.assertLess(time.monotonic() - started, 1)
        self.assertNotIn(str(self.repo), str(exc))
        self.assertFalse(self.state.exists())

    def test_queries_use_adjustable_total_budget_and_standalone_fallback(self):
        with mock.patch.object(w, '_QUERY_SECONDS', 0.000001):
            plan = w.inspect(str(self.repo), isolation='worktree', timeout_ms=60000)
            self.assertEqual(w.materialize(plan, str(uuid.uuid4()), timeout_ms=60000)['state'], 'ready')
        with mock.patch.object(w, '_QUERY_SECONDS', 0.03), mock.patch.object(w.subprocess, 'Popen') as popen:
            process = popen.return_value
            process.communicate.return_value = (b'ok', None)
            process.returncode = 0
            self.assertEqual(w._git(str(self.repo), '--version'), (0, 'ok'))
            self.assertLessEqual(process.communicate.call_args.kwargs['timeout'], 0.03)

    def test_budget_expires_immediately_after_spawn_stops_owned_process(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        original = subprocess.Popen
        owned = []
        def expire_after_spawn(argv, **kwargs):
            if 'worktree' in argv and 'add' in argv:
                Path(argv[-2]).mkdir()
                process = original([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
                owned.append(process)
                w._DEADLINE.set(time.monotonic() - 1)
                return process
            return original(argv, **kwargs)
        with mock.patch.object(w.subprocess, 'Popen', side_effect=expire_after_spawn):
            exc = self.error('worktree_timeout', w.materialize, plan, str(uuid.uuid4()))
        self.assertIsNotNone(owned[0].poll())
        self.assertEqual(exc.workspace['state'], 'unknown')
        self.assertTrue(Path(exc.workspace['path']).exists())
        self.assertGreaterEqual(exc.workspace['creation_ms'], 0)

    def test_post_creation_verification_timeout_preserves_unknown_worktree(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        snapshot = w._reuse_snapshot
        def expire_verification(cwd, **kwargs):
            if cwd != str(self.repo):
                raise w.WorkspaceError('workspace_query_timeout', 'Git preparation timed out')
            return snapshot(cwd, **kwargs)
        with mock.patch.object(w, '_reuse_snapshot', side_effect=expire_verification):
            exc = self.error('worktree_timeout', w.materialize, plan, str(uuid.uuid4()))
        self.assertEqual(exc.workspace['state'], 'unknown')
        self.assertEqual(exc.workspace['cwd'], str(self.repo))
        self.assertTrue((Path(exc.workspace['path']) / 'sub' / 'file').exists())
        self.assertIn(exc.workspace['branch'], self.git('worktree', 'list', '--porcelain'))

    def test_create_scans_status_twice_and_supported_once(self):
        with mock.patch.object(w, '_git', wraps=w._git) as queries, mock.patch.object(w, '_supported', wraps=w._supported) as supported:
            plan = w.inspect(str(self.repo), isolation='worktree')
            self.assertEqual(w.materialize(plan, str(uuid.uuid4()), before_create=lambda meta: None)['state'], 'ready')
        status = [call for call in queries.call_args_list if call.args[1] == 'status']
        self.assertEqual(len(status), 2)
        self.assertTrue(all('--untracked-files=normal' in call.args for call in status))
        self.assertEqual(supported.call_count, 1)

    def test_callback_head_change_is_rejected_before_add(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        def change_head(meta):
            self.git('commit', '--allow-empty', '-qm', 'changed during checkpoint')
        exc = self.error('workspace_source_changed', w.materialize, plan, str(uuid.uuid4()), before_create=change_head)
        self.assertEqual(exc.workspace['state'], 'failed')
        self.assertFalse(Path(exc.workspace['path']).exists())
        self.assertFalse(self.state.exists())

    def test_branch_namespace_conflict_preserves_repository(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        ident = str(uuid.uuid4())
        for branch in ('router', 'router/' + ident + '/child'):
            self.git('branch', branch)
            before = self.git('show-ref')
            exc = self.error('workspace_branch_conflict', w.materialize, plan, ident)
            self.assertEqual(self.git('show-ref'), before)
            self.assertFalse(self.state.exists())
            self.assertFalse(Path(exc.workspace['path']).exists())
            self.git('branch', '-d', branch)

    def test_pre_spawn_timeout_and_interrupt_are_failed(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        for failure in (w.WorkspaceError('workspace_query_timeout', 'Git preparation timed out'), KeyboardInterrupt(), SystemExit()):
            # Only fail the final budget check immediately preceding directory creation.
            with mock.patch.object(w, '_recheck'), mock.patch.object(w, '_git', return_value=(1, '')), mock.patch.object(w, '_out', return_value=''), mock.patch.object(w, '_remaining', side_effect=failure):
                with self.assertRaises(type(failure)) as caught:
                    w.materialize(plan, str(uuid.uuid4()))
            self.assertEqual(caught.exception.workspace['state'], 'failed')
            self.assertFalse(Path(caught.exception.workspace['path']).exists())
        self.assertFalse(self.state.exists())

    def test_checkpoint_interrupt_is_failed_and_preserves_exception(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        for original in (KeyboardInterrupt(), SystemExit()):
            def fail(meta):
                raise original
            with self.assertRaises(type(original)) as caught:
                w.materialize(plan, str(uuid.uuid4()), before_create=fail)
            self.assertIs(caught.exception, original)
            self.assertEqual(original.workspace['state'], 'failed')
            self.assertFalse(self.state.exists())

    def test_checkout_filter_failure_does_not_expose_stderr(self):
        (self.repo / '.gitattributes').write_text('sub/file filter=fail\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-qm', 'filter declaration')
        self.git('config', 'filter.fail.smudge', 'echo secret-credential >&2; exit 1')
        self.git('config', 'filter.fail.clean', 'cat')
        self.git('config', 'filter.fail.required', 'true')
        plan = w.inspect(str(self.repo), isolation='worktree')
        exc = self.error('worktree_create_failed', w.materialize, plan, str(uuid.uuid4()))
        self.assertEqual(exc.workspace['state'], 'failed')
        self.assertNotIn('secret-credential', str(exc))
        self.assertIn('filters', exc.message)

    def test_detached_parent_rejected_with_identity_error(self):
        meta = self.create()
        plan = w.inspect(meta['cwd'], parent_workspace=meta)
        self.git('checkout', '--detach', cwd=meta['cwd'])
        self.error('workspace_identity_changed', w.inspect, meta['cwd'], parent_workspace=meta)
        self.error('workspace_identity_changed', w.materialize, plan, str(uuid.uuid4()))

    @unittest.skipUnless(os.name == 'posix', 'Process groups require POSIX')
    def test_timeout_kills_owned_filter_group_retains_unknown_metadata(self):
        plan = w.inspect(str(self.repo), isolation='worktree')
        fake = self.tmp / 'bin'
        fake.mkdir()
        real_git = subprocess.check_output(['which', 'git'], text=True).strip()
        script = fake / 'git'
        pid_file = self.tmp / 'child.pid'
        heartbeat = self.tmp / 'heartbeat'
        script.write_text('#!/usr/bin/env python3\nimport os,sys,subprocess,time,signal\n'
                          'if "add" not in sys.argv:\n os.execv(' + repr(real_git) + ', [' + repr(real_git) + ']+sys.argv[1:])\n'
                          'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                          'child=subprocess.Popen([sys.executable,"-c",' + repr('import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN)\nwhile True:\n open(' + repr(str(heartbeat)) + ',"a").write("x");time.sleep(0.02)') + '])\n'
                          'open(' + repr(str(pid_file)) + ',"w").write(str(child.pid))\n'
                          'os.makedirs(sys.argv[-2])\ntime.sleep(30)\n')
        script.chmod(0o755)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {'PATH': str(fake) + os.pathsep + os.environ['PATH']}):
            exc = self.error('worktree_timeout', w.materialize, plan, str(uuid.uuid4()), timeout_ms=1500)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(exc.workspace['state'], 'unknown')
        self.assertGreaterEqual(exc.workspace['creation_ms'], 0)
        self.assertTrue(Path(exc.workspace['path']).exists())
        self.assertTrue(w.validate_metadata(exc.workspace, exc.workspace['cwd']))
        self.assertTrue(pid_file.exists())
        size = heartbeat.stat().st_size
        time.sleep(0.15)
        self.assertEqual(heartbeat.stat().st_size, size, 'Owned child survived timeout')
        self.assertNotIn('ready', str(exc))
        original_wait = subprocess.Popen.wait
        for interruption in (KeyboardInterrupt, SystemExit):
            heartbeat.unlink()
            interrupted = [False]
            def interrupt_wait(process, *args, **kwargs):
                if 'worktree' in process.args and 'add' in process.args and not interrupted[0]:
                    interrupted[0] = True
                    deadline = time.monotonic() + 2
                    while not heartbeat.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    raise interruption()
                return original_wait(process, *args, **kwargs)
            with mock.patch.dict(os.environ, {'PATH': str(fake) + os.pathsep + os.environ['PATH']}), mock.patch.object(subprocess.Popen, 'wait', interrupt_wait):
                with self.assertRaises(interruption) as caught:
                    w.materialize(plan, str(uuid.uuid4()))
            self.assertEqual(caught.exception.workspace['state'], 'unknown')
            self.assertTrue(Path(caught.exception.workspace['path']).exists())
            size = heartbeat.stat().st_size
            time.sleep(0.1)
            self.assertEqual(heartbeat.stat().st_size, size)



if __name__ == '__main__':
    unittest.main()
