"""Explicit, single-attempt Git workspaces. Inspection never writes to disk."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
from typing import Callable

from .model import state_dir

_QUERY_SECONDS = 5
_DEADLINE = ContextVar("workspace_deadline", default=None)
_FIELDS = {'kind', 'cwd', 'path', 'branch', 'common_dir', 'source_root',
           'source_cwd', 'relative_cwd', 'base_head', 'state', 'creation_ms'}


class WorkspaceError(Exception):
    def __init__(self, code, message, *, workspace=None):
        super().__init__(f'{code}: {message}')
        self.code, self.message, self.workspace = code, message, workspace


def _error(code, message, workspace=None):
    return WorkspaceError(code, message, workspace=workspace)


def _absolute(value):
    return isinstance(value, str) and bool(value) and '\0' not in value and os.path.isabs(value)


def validate_metadata(value, cwd: str) -> bool:
    """Validate JSON shape and lexical path relationships without filesystem access."""
    if not isinstance(value, dict) or not _absolute(cwd) or value.get('cwd') != cwd:
        return False
    if value.get('kind') == 'directory':
        return set(value) == {'kind', 'cwd'}
    if set(value) != _FIELDS or value.get('kind') != 'worktree':
        return False
    if not all(_absolute(value[k]) for k in ('cwd', 'path', 'common_dir', 'source_root', 'source_cwd')):
        return False
    relative = value['relative_cwd']
    if (not isinstance(relative, str) or '\0' in relative or os.path.isabs(relative)
            or '..' in relative.split(os.sep) or os.path.normpath(relative) != relative):
        return False
    if os.path.normpath(os.path.join(value['source_root'], relative)) != value['source_cwd']:
        return False
    state = value['state']
    if not isinstance(state, str) or state not in {'creating', 'ready', 'unknown', 'failed'}:
        return False
    expected = os.path.normpath(os.path.join(value['path'], relative)) if state == 'ready' else value['source_cwd']
    if cwd != expected:
        return False
    if not isinstance(value['branch'], str) or not value['branch'].startswith('router/'):
        return False
    try:
        if str(uuid.UUID(value['branch'][7:])) != value['branch'][7:]:
            return False
    except (ValueError, AttributeError):
        return False
    if not isinstance(value['base_head'], str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['base_head']):
        return False
    elapsed = value['creation_ms']
    return elapsed is None or (type(elapsed) is int and elapsed >= 0)


def _env():
    env = os.environ.copy()
    # Never inherit an alternate repository, index or worktree from the caller.
    for key in list(env):
        if key.startswith('GIT_'):
            del env[key]
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    return env


def _remaining():
    deadline = _DEADLINE.get()
    remaining = deadline - time.monotonic() if deadline is not None else _QUERY_SECONDS
    if remaining <= 0:
        raise _error('workspace_query_timeout', 'Git preparation timed out')
    return remaining


def _git(cwd, *args, allow_failure=False):
    process = None
    try:
        limit = _remaining()
        process = subprocess.Popen(['git', '-C', cwd, *args], env=_env(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix')
        output, _ = process.communicate(timeout=min(limit, _remaining()))
    except subprocess.TimeoutExpired:
        _stop(process)
        raise _error('workspace_query_timeout', 'Git inspection timed out') from None
    except WorkspaceError:
        if process is not None:
            _stop(process)
        raise
    except (KeyboardInterrupt, SystemExit):
        if process is not None:
            _stop(process)
        raise
    except OSError:
        if process is not None:
            _stop(process)
        raise _error('workspace_git_error', 'Git inspection could not run') from None
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
    if process.returncode and not allow_failure:
        raise _error('workspace_git_error', 'Git inspection failed')
    return process.returncode, output.decode('utf-8', errors='surrogateescape')


def _out(cwd, *args):
    return _git(cwd, *args)[1].removesuffix('\n')


def _ref(cwd, *, require_branch=False):
    code, ref = _git(cwd, 'symbolic-ref', '-q', 'HEAD', allow_failure=True)
    if code == 1 and require_branch:
        raise _error('workspace_identity_changed', 'Worktree is no longer on its registered branch')
    if code not in (0, 1):
        raise _error('workspace_git_error', 'Git inspection failed')
    return ref.strip()


def _source_identity(cwd):
    code, bare = _git(cwd, 'rev-parse', '--is-bare-repository', allow_failure=True)
    if code:
        raise _error('workspace_not_git', 'Source is not a Git repository')
    if bare.strip() == 'true':
        raise _error('workspace_bare', 'Bare repositories are unsupported')
    root = os.path.realpath(_out(cwd, 'rev-parse', '--show-toplevel'))
    common = os.path.realpath(_out(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    code, head = _git(cwd, 'rev-parse', '--verify', 'HEAD^{commit}', allow_failure=True)
    if code:
        raise _error('workspace_unborn', 'Source requires a committed HEAD')
    return {'root': root, 'common_dir': common, 'head': head.strip(), 'ref': _ref(cwd)}


def _snapshot(cwd, *, clean):
    snapshot = _source_identity(cwd)
    status = _out(cwd, 'status', '--porcelain=v1', '-z', '--untracked-files=normal', '--ignore-submodules=none')
    if clean and status:
        raise _error('workspace_dirty', 'Source has tracked or untracked changes')
    return {**snapshot, 'status': status}


def _supported(cwd):
    if _out(cwd, 'rev-parse', '--show-superproject-working-tree'):
        raise _error('workspace_unsupported', 'Submodule workspaces are unsupported')
    root = _out(cwd, 'rev-parse', '--show-toplevel')
    if _out(root, 'ls-tree', '-z', 'HEAD', '--', '.gitmodules'):
        raise _error('workspace_unsupported', 'Submodule repositories are unsupported')
    for key in ('core.sparseCheckout', 'core.sparseCheckoutCone'):
        code, value = _git(cwd, 'config', '--bool', '--get', key, allow_failure=True)
        if code not in (0, 1) or value.strip() == 'true':
            raise _error('workspace_unsupported', 'Sparse checkout is unsupported')
    entries = _out(root, 'ls-files', '--stage', '-z').split('\0')
    if any(entry.startswith('160000 ') for entry in entries):
        raise _error('workspace_unsupported', 'Submodules are unsupported')


def _reuse_snapshot(cwd):
    return {'root': os.path.realpath(_out(cwd, 'rev-parse', '--show-toplevel')),
            'common_dir': os.path.realpath(_out(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')),
            'ref': _ref(cwd, require_branch=True)}


def _identity(meta, snapshot):
    if (snapshot['root'] != meta['path'] or snapshot['common_dir'] != meta['common_dir']
            or snapshot['ref'] != 'refs/heads/' + meta['branch']
            or os.path.realpath(meta['cwd']) != meta['cwd']):
        raise _error('workspace_identity_changed', 'Worktree identity does not match metadata', meta)
    records = _out(meta['cwd'], 'worktree', 'list', '--porcelain', '-z').split('\0\0')
    expected = {'worktree ' + meta['path'], 'branch refs/heads/' + meta['branch']}
    if not any(expected.issubset(set(record.split('\0'))) for record in records):
        raise _error('workspace_identity_changed', 'Worktree registration does not match metadata', meta)


def _inspect(cwd: str, *, isolation=None, parent_workspace=None) -> dict:
    if not isinstance(cwd, str) or not cwd or '\0' in cwd or not os.path.isdir(cwd):
        raise _error('workspace_invalid_cwd', 'Source directory is invalid')
    cwd = os.path.realpath(cwd)
    if isolation not in (None, 'shared', 'worktree'):
        raise _error('workspace_invalid_isolation', 'Workspace isolation is invalid')
    if isinstance(parent_workspace, dict) and parent_workspace.get('kind') == 'directory' and isolation == 'worktree':
        raise _error('workspace_isolation_conflict', 'A directory parent requires shared isolation')
    parent_tree = isinstance(parent_workspace, dict) and parent_workspace.get('kind') == 'worktree'
    if parent_tree:
        if isolation == 'shared':
            raise _error('workspace_isolation_conflict', 'A worktree parent requires worktree isolation')
        meta = dict(parent_workspace)
        if not validate_metadata(meta, cwd) or meta['state'] != 'ready':
            raise _error('workspace_invalid_metadata', 'Parent worktree metadata is invalid')
        snap = _reuse_snapshot(cwd)
        _identity(meta, snap)
        return {'mode': 'reuse', 'source_cwd': cwd, 'workspace': meta, 'snapshot': snap}
    directory = {'kind': 'directory', 'cwd': cwd}
    if isolation != 'worktree':
        return {'mode': 'shared', 'source_cwd': cwd, 'workspace': directory}
    snap = _snapshot(cwd, clean=False)
    if snap['status']:
        raise _error('workspace_dirty', 'Source has tracked or untracked changes')
    output_root = os.path.realpath(str(state_dir() / 'worktrees'))
    if os.path.commonpath([output_root, snap['root']]) == snap['root']:
        raise _error('workspace_invalid_path', 'Worktree output must be outside the source repository')
    _supported(cwd)
    relative = os.path.relpath(cwd, snap['root'])
    if relative != '.':
        code, kind = _git(snap['root'], 'cat-file', '-t', snap['head'] + ':' + relative, allow_failure=True)
        if code or kind.strip() != 'tree':
            raise _error('workspace_invalid_cwd', 'Source subdirectory is absent from HEAD')
    return {'mode': 'create', 'source_cwd': cwd, 'workspace': directory, 'snapshot': snap}


def _recheck(plan, *, status=True):
    if plan['mode'] == 'reuse':
        _identity(plan['workspace'], _reuse_snapshot(plan['source_cwd']))
        return
    snap = (_snapshot(plan['source_cwd'], clean=False) if status
            else _source_identity(plan['source_cwd']))
    if any(value != plan['snapshot'].get(key) for key, value in snap.items()):
        raise _error('workspace_source_changed', 'Workspace changed during routing', plan['workspace'])


def _stop(process):
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            # Descendants can survive even if the group leader has exited.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait()


def _materialize(plan: dict, dispatch_id: str, timeout_ms: int = 60000, *,
                before_create: Callable[[dict], None] | None = None) -> dict:
    mode = plan['mode']
    if mode == 'shared':
        return dict(plan['workspace'])
    _recheck(plan)
    if mode == 'reuse':
        return dict(plan['workspace'])
    try:
        valid_id = str(uuid.UUID(dispatch_id)) == dispatch_id
    except (ValueError, AttributeError):
        valid_id = False
    if not valid_id or type(timeout_ms) is not int or timeout_ms <= 0:
        raise _error('workspace_invalid_request', 'Workspace creation request is invalid')
    snap = plan['snapshot']
    repo_hash = hashlib.sha256(os.fsencode(snap['common_dir'])).hexdigest()
    path = os.path.realpath(str(state_dir() / 'worktrees' / repo_hash / dispatch_id))
    if os.path.commonpath([path, snap['root']]) == snap['root']:
        raise _error('workspace_invalid_path', 'Worktree output must be outside the source repository')
    relative = os.path.relpath(plan['source_cwd'], snap['root'])
    meta = {'kind': 'worktree', 'cwd': plan['source_cwd'], 'path': path,
            'branch': 'router/' + dispatch_id, 'common_dir': snap['common_dir'],
            'source_root': snap['root'], 'source_cwd': plan['source_cwd'],
            'relative_cwd': relative, 'base_head': snap['head'],
            'state': 'creating', 'creation_ms': None}
    if not validate_metadata(meta, meta['cwd']):
        raise _error('workspace_invalid_metadata', 'Planned worktree metadata is invalid', meta)
    def vacant():
        refs = _out(plan['source_cwd'], 'for-each-ref', '--format=%(refname)', 'refs/heads/router').splitlines()
        if any(ref == 'refs/heads/router' or ref.startswith('refs/heads/' + meta['branch'] + '/') for ref in refs):
            raise _error('workspace_branch_conflict', 'Worktree branch namespace is occupied', meta)
        code, _ = _git(plan['source_cwd'], 'show-ref', '--verify', '--quiet', 'refs/heads/' + meta['branch'], allow_failure=True)
        if os.path.lexists(path) or code == 0:
            raise _error('workspace_collision', 'Worktree path or branch is occupied', meta)
        if code != 1:
            raise _error('workspace_git_error', 'Branch inspection failed', meta)
        records = _out(plan['source_cwd'], 'worktree', 'list', '--porcelain', '-z').split('\0')
        if 'worktree ' + path in records:
            raise _error('workspace_collision', 'Worktree path is registered', meta)
    try:
        vacant()
    except (WorkspaceError, KeyboardInterrupt, SystemExit) as exc:
        meta['state'] = 'failed'
        exc.workspace = meta
        raise
    # Checkpoint errors propagate unchanged, before any creation mutation.
    try:
        if before_create is not None:
            before_create(dict(meta))
    except BaseException as exc:
        meta['state'] = 'failed'
        exc.workspace = meta
        raise
    # Close identity races after the checkpoint without repeating the status scan.
    try:
        _recheck(plan, status=False)
        vacant()
    except WorkspaceError as exc:
        meta['state'] = 'failed'
        exc.workspace = meta
        raise
    except (KeyboardInterrupt, SystemExit) as exc:
        meta['state'] = 'failed'
        exc.workspace = meta
        raise
    started = time.monotonic()
    process = None
    try:
        _remaining()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(['git', '-C', plan['source_cwd'], 'worktree', 'add', '-b', meta['branch'], path, snap['head']],
                                   env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name == 'posix')
        process.wait(timeout=_remaining())
        if process.returncode:
            meta['state'] = 'failed'
            raise _error('worktree_create_failed', 'Worktree creation failed; inspect checkout, hooks, filters, or storage', meta)
        actual_cwd = os.path.realpath(os.path.join(path, relative))
        if not os.path.isdir(actual_cwd):
            raise _error('worktree_create_failed', 'Requested worktree directory is missing', meta)
        ready = {**meta, 'cwd': actual_cwd, 'state': 'ready'}
        actual = _reuse_snapshot(actual_cwd)
        _identity(ready, actual)
        if _out(actual_cwd, 'rev-parse', '--verify', 'HEAD^{commit}') != snap['head']:
            raise _error('worktree_create_failed', 'Created worktree HEAD does not match', meta)
        meta = ready
    except subprocess.TimeoutExpired:
        _stop(process)
        meta['state'] = 'unknown'
        raise _error('worktree_timeout', 'Worktree creation timed out', meta) from None
    except (KeyboardInterrupt, SystemExit) as exc:
        if process is not None:
            _stop(process)
        meta['state'] = 'unknown' if process is not None else 'failed'
        exc.workspace = meta
        raise
    except WorkspaceError as exc:
        if process is not None and process.poll() is None:
            _stop(process)
        if exc.code == 'workspace_query_timeout':
            meta['state'] = 'unknown' if process is not None else 'failed'
            raise _error('worktree_timeout', 'Worktree verification timed out', meta) from None
        meta['state'] = 'failed'
        exc.workspace = meta
        raise
    except OSError:
        meta['state'] = 'failed'
        raise _error('worktree_create_failed', 'Worktree creation could not run; inspect Git availability or storage', meta) from None
    finally:
        meta['creation_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return meta


def inspect(cwd: str, *, isolation: str | None = None,
            parent_workspace: dict | None = None, timeout_ms: int = 60000) -> dict:
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise _error('workspace_invalid_request', 'Git preparation budget is invalid')
    started = time.monotonic()
    token = _DEADLINE.set(started + timeout_ms / 1000)
    try:
        plan = _inspect(cwd, isolation=isolation, parent_workspace=parent_workspace)
        plan['inspection_ms'] = max(0, int((time.monotonic() - started) * 1000))
        return plan
    finally:
        _DEADLINE.reset(token)


def materialize(plan: dict, dispatch_id: str, timeout_ms: int = 60000, *,
                before_create: Callable[[dict], None] | None = None) -> dict:
    if plan['mode'] == 'shared':
        return dict(plan['workspace'])
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise _error('workspace_invalid_request', 'Git preparation budget is invalid')
    token = _DEADLINE.set(time.monotonic() + (timeout_ms - plan.get('inspection_ms', 0)) / 1000)
    try:
        return _materialize(plan, dispatch_id, timeout_ms, before_create=before_create)
    finally:
        _DEADLINE.reset(token)
