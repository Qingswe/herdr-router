"""Durable, single-attempt dispatch sessions; reads never initialize the store.

Each write owns one short transaction. No transaction survives a call into
Herdr, and a confirmed prompt records activity, not task completion.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import uuid

from .model import state_dir
from . import workspace as workspaces


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
_STATUSES = {"in_progress", "confirmed", "failed"}
_PROMPT_STATUSES = {"not_sent", "unknown", "confirmed"}
_COLUMN_TYPES = {
    "schema_version": "INTEGER", "id": "TEXT", "dispatch_id": "TEXT",
    "parent_id": "TEXT", "phase": "TEXT", "task": "TEXT", "handoff": "TEXT",
    "decision": "TEXT", "cwd": "TEXT", "workspace": "TEXT",
    "created_at": "INTEGER", "updated_at": "INTEGER", "stage": "TEXT",
    "status": "TEXT", "prompt_status": "TEXT", "agent": "TEXT",
    "pane_id": "TEXT", "error": "TEXT", "owner_pid": "INTEGER",
}
_COLUMNS = ", ".join(_COLUMN_TYPES)
_SCHEMA = """
CREATE TABLE sessions (
    schema_version INTEGER NOT NULL,
    id TEXT PRIMARY KEY NOT NULL,
    dispatch_id TEXT NOT NULL UNIQUE,
    parent_id TEXT REFERENCES sessions(id),
    phase TEXT NOT NULL,
    task TEXT NOT NULL,
    handoff TEXT,
    decision TEXT NOT NULL,
    cwd TEXT NOT NULL,
    workspace TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_status TEXT NOT NULL,
    agent TEXT,
    pane_id TEXT,
    error TEXT,
    owner_pid INTEGER NOT NULL
)
"""


class SessionError(Exception):
    """A safe public error: never include tasks, SQL, or backend diagnostics."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _store_error(error: BaseException | None = None) -> SessionError:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error, sqlite3.Error) and isinstance(code, int) and (code & 0xff) in {
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return SessionError("session_store_busy", "session store is busy")
    return SessionError("session_store_error", "could not access a valid session store")


def _invalid() -> SessionError:
    return SessionError("session_invalid", "invalid session fields")


def _id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _label(value: object) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 64
            and not any(ord(char) < 32 for char in value))


def _path(value: object) -> bool:
    return isinstance(value, str) and "\0" not in value and os.path.isabs(value)


def _handoff(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"path", "text"}:
        return False
    text = value["text"]
    return (_path(value["path"]) and isinstance(text, str) and bool(text.strip())
            and len(text.encode("utf-8")) <= 48 * 1024)


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None


def _reject_constant(value: str):
    raise ValueError("invalid JSON constant")


def _decode(row: sqlite3.Row) -> dict:
    try:
        record = dict(row)
        for key in ("decision", "workspace", "handoff"):
            if record[key] is not None:
                record[key] = json.loads(record[key], parse_constant=_reject_constant)
        valid = (
            type(record["schema_version"]) is int
            and record["schema_version"] == SCHEMA_VERSION
            and _id(record["id"]) and record["dispatch_id"] == record["id"]
            and (record["parent_id"] is None or (
                _id(record["parent_id"]) and record["parent_id"] != record["id"]
                and record["handoff"] is not None))
            and _label(record["phase"]) and isinstance(record["task"], str)
            and _handoff(record["handoff"]) and isinstance(record["decision"], dict)
            and isinstance(record["decision"].get("prompt"), str)
            and _path(record["cwd"])
            and workspaces.validate_metadata(record["workspace"], record["cwd"])
            and type(record["created_at"]) is int and record["created_at"] >= 0
            and type(record["updated_at"]) is int
            and record["updated_at"] >= record["created_at"]
            and _label(record["stage"]) and record["status"] in _STATUSES
            and record["prompt_status"] in _PROMPT_STATUSES
            and (record["status"] != "confirmed" or record["prompt_status"] == "confirmed")
            and all(record[key] is None or isinstance(record[key], str)
                    for key in ("agent", "pane_id", "error"))
            and type(record["owner_pid"]) is int and record["owner_pid"] > 0
        )
        if not valid:
            raise _store_error()
        record["decision"]["prompt"].encode("utf-8")
        return record
    except (KeyError, TypeError, ValueError, RecursionError):
        raise _store_error() from None


def _configure(conn: sqlite3.Connection, *, write: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if write:
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        conn.execute("PRAGMA query_only = ON")


def _check_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    columns = conn.execute("PRAGMA table_info(sessions)").fetchall()
    if (version != SCHEMA_VERSION
            or {col["name"]: col["type"] for col in columns} != _COLUMN_TYPES
            or not any(col["name"] == "id" and col["pk"] == 1 for col in columns)):
        raise _store_error()


def _initialize(path: Path) -> None:
    """Publish a fully initialized 0600 DB without overwriting another writer.

    Building beside the store and linking atomically avoids exposing an empty
    database to concurrent first writers. Existing stores are never rebuilt.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sessions-", suffix=".sqlite3", dir=path.parent)
    os.close(fd)
    conn = None
    try:
        conn = sqlite3.connect(Path(temporary).as_uri() + "?mode=rw", uri=True,
                               timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        _configure(conn, write=True)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        conn.close()
        conn = None
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if conn is not None:
            conn.close()
        os.unlink(temporary)


@contextmanager
def _transaction(*, write: bool, initialize: bool = False):
    conn = None
    try:
        path = (state_dir() / "sessions.sqlite3").absolute()
        try:
            path.stat()
        except FileNotFoundError:
            if not initialize:
                yield None
                return
            _initialize(path)
        mode = "rw" if write else "ro"
        conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True,
                               timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        _configure(conn, write=write)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        _check_schema(conn)
        yield conn
        conn.commit()
    except (OSError, sqlite3.Error, ValueError) as e:
        raise _store_error(e) from None
    finally:
        if conn is not None:
            # Closing also rolls back an unsuccessful transaction.
            conn.close()


def _get(conn: sqlite3.Connection | None, session_id: str) -> dict:
    row = None if conn is None else conn.execute(
        f"SELECT {_COLUMNS} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise SessionError("session_not_found", "session does not exist")
    return _decode(row)


def create(session_id: str, *, decision: dict, cwd: str, parent_id: str | None = None,
           phase: str = "task", task: str | None = None, handoff: dict | None = None,
           workspace: dict | None = None) -> dict:
    """Snapshot one new attempt; continuation only links to a confirmed parent."""
    try:
        if (not _id(session_id) or (parent_id is not None and not isinstance(parent_id, str))
                or not _label(phase) or not isinstance(decision, dict)
                or not isinstance(decision.get("prompt"), str)
                or not _handoff(handoff)):
            raise _invalid()
        if not isinstance(cwd, str) or not cwd or "\0" in cwd:
            raise SessionError("invalid_cwd", "session cwd must be an existing directory")
        if task is None:
            task = decision["prompt"]
        if not isinstance(task, str):
            raise _invalid()
        decision_json, handoff_json = _json(decision), _json(handoff) if handoff is not None else None
        # Ensure strings can be stored as SQLite UTF-8 without echoing them on failure.
        for value in (task, decision_json, handoff_json or "", phase, cwd):
            value.encode("utf-8")
    except (ValueError, RecursionError):
        raise _invalid() from None

    with _transaction(write=True, initialize=True) as conn:
        cwd = os.path.realpath(cwd)
        try:
            directory = stat.S_ISDIR(os.stat(cwd).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            directory = False
        if not directory:
            raise SessionError("invalid_cwd", "session cwd must be an existing directory")
        if workspace is None:
            workspace = {"kind": "directory", "cwd": cwd}
        if not workspaces.validate_metadata(workspace, cwd):
            raise _invalid()
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise SessionError("session_exists", "session already exists")
        if parent_id is not None:
            parent = _get(conn, parent_id)
            if parent["prompt_status"] != "confirmed":
                raise SessionError("session_parent_unconfirmed", "parent activity is not confirmed")
            if os.path.realpath(parent["cwd"]) != cwd:
                raise SessionError("session_cwd_mismatch", "continuation must use the parent directory")
            if handoff is None:
                raise SessionError("handoff_required", "continuation requires a handoff snapshot")
            if workspace != parent["workspace"]:
                raise SessionError("workspace_isolation_conflict", "continuation must preserve the parent workspace")
        now = time.time_ns() // 1_000_000
        values = (SCHEMA_VERSION, session_id, session_id, parent_id, phase, task,
                  handoff_json, decision_json, cwd, _json(workspace),
                  now, now, "prepare", "in_progress", "not_sent", None, None, None, os.getpid())
        conn.execute(f"INSERT INTO sessions ({_COLUMNS}) VALUES ({', '.join('?' for _ in values)})",
                     values)
        return _get(conn, session_id)


def set_workspace(session_id: str, workspace: dict) -> dict:
    """Persist planned/ready isolation before launch, atomically with its cwd."""
    cwd = workspace.get("cwd") if isinstance(workspace, dict) else None
    if not workspaces.validate_metadata(workspace, cwd):
        raise _invalid()
    encoded = _json(workspace)
    try:
        encoded.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None
    with _transaction(write=True) as conn:
        record = _get(conn, session_id)
        if record["status"] != "in_progress" or record["prompt_status"] != "not_sent":
            raise SessionError("session_invalid", "workspace can only change before task submission")
        previous = record["workspace"]
        if previous["kind"] == "directory":
            source = workspace.get("source_cwd") if workspace["kind"] == "worktree" else cwd
            if source != record["cwd"]:
                raise SessionError("workspace_isolation_conflict", "workspace must originate from the attempt directory")
        if previous["kind"] == "worktree":
            identity = ("path", "branch", "common_dir", "source_root", "source_cwd", "relative_cwd", "base_head")
            if workspace["kind"] != "worktree" or any(previous[k] != workspace[k] for k in identity):
                raise SessionError("workspace_isolation_conflict", "worktree identity cannot change within an attempt")
            if previous["state"] == "ready" and workspace["state"] != "ready":
                raise SessionError("session_invalid", "a prepared workspace cannot revert to a creation state")
        if record["parent_id"] is not None:
            parent = _get(conn, record["parent_id"])
            if workspace != parent["workspace"]:
                raise SessionError("workspace_isolation_conflict", "continuation must preserve the parent workspace")
        conn.execute("UPDATE sessions SET cwd = ?, workspace = ?, updated_at = ? WHERE id = ?",
                     (cwd, encoded, max(record["updated_at"], time.time_ns() // 1_000_000), session_id))
        return _get(conn, session_id)


def update(session_id: str, *, stage, status, prompt_status, agent=None, pane_id=None,
           error=None) -> dict:
    """Read/modify/write only this attempt, retaining known agent and pane IDs."""
    if (not isinstance(session_id, str) or not _label(stage) or not isinstance(status, str)
            or status not in _STATUSES or not isinstance(prompt_status, str)
            or prompt_status not in _PROMPT_STATUSES
            or (status == "confirmed" and prompt_status != "confirmed")
            or any(value is not None and not isinstance(value, str)
                   for value in (agent, pane_id, error))):
        raise _invalid()
    with _transaction(write=True) as conn:
        record = _get(conn, session_id)
        if record["prompt_status"] == "confirmed" and prompt_status != "confirmed":
            raise SessionError("session_invalid", "confirmed activity cannot be reverted")
        conn.execute("""UPDATE sessions SET stage = ?, status = ?, prompt_status = ?,
                     agent = ?, pane_id = ?, error = ?, updated_at = ? WHERE id = ?""",
                     (stage, status, prompt_status,
                      agent if agent is not None else record["agent"],
                      pane_id if pane_id is not None else record["pane_id"], error,
                      max(record["updated_at"], time.time_ns() // 1_000_000), session_id))
        return _get(conn, session_id)


def get(session_id: str) -> dict:
    if not isinstance(session_id, str):
        raise _invalid()
    with _transaction(write=False) as conn:
        return _get(conn, session_id)


def list_sessions(limit=20) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 2**63 - 1:
        raise _invalid()
    with _transaction(write=False) as conn:
        if conn is None:
            return []
        rows = conn.execute(f"SELECT {_COLUMNS} FROM sessions ORDER BY created_at DESC, id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [{key: value for key, value in _decode(row).items()
                 if key not in {"task", "handoff", "decision"}} for row in rows]
