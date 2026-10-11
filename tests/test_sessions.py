"""SQLite session persistence, exercised only in isolated temporary state."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from router import sessions


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.tmp / "missing" / "state"
        self.db = self.state / "sessions.sqlite3"
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_PLUGIN_CONFIG_DIR": str(self.tmp / "config"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }))
        self.decision = {"prompt": "任务 '); DROP TABLE sessions; --\n$(no-shell)",
                         "assessment": {"source": "test"}}

    def create(self, session_id=None, **kwargs):
        return sessions.create(session_id or str(uuid.uuid4()), **{
            "decision": self.decision, "cwd": str(self.tmp), **kwargs})

    def update(self, session_id, **kwargs):
        return sessions.update(session_id, **{
            "stage": "prompt", "status": "in_progress", "prompt_status": "unknown", **kwargs})

    def assert_error(self, code, call, *args, **kwargs):
        with self.assertRaises(sessions.SessionError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(self.decision["prompt"], str(caught.exception))
        return caught.exception

    def sql(self, query, args=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(query, args)
            conn.commit()
        finally:
            conn.close()

    def test_missing_read_only_store_and_update_leave_no_files(self):
        missing = str(uuid.uuid4())
        self.assertEqual(sessions.list_sessions(), [])
        self.assert_error("session_not_found", sessions.get, missing)
        self.assert_error("session_not_found", sessions.get, "missing-id")
        self.assert_error("session_not_found", self.update, missing)
        self.assertFalse(self.state.parent.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_create_round_trip_snapshots_and_permissions(self):
        snapshot = {"path": str(self.tmp / "handoff.md"), "text": "原始交接摘要"}
        created = self.create(task="original task", phase="review", handoff=snapshot)
        self.decision["assessment"]["source"] = "modified"
        snapshot["text"] = "modified"
        stored = sessions.get(created["id"])
        self.assertEqual(stored, created)
        self.assertEqual(stored["dispatch_id"], stored["id"])
        self.assertEqual(stored["schema_version"], sessions.SCHEMA_VERSION)
        self.assertEqual(stored["workspace"], {"kind": "directory", "cwd": str(self.tmp.resolve())})
        self.assertEqual((stored["stage"], stored["status"], stored["prompt_status"]),
                         ("prepare", "in_progress", "not_sent"))
        self.assertEqual(stored["owner_pid"], os.getpid())
        self.assertLessEqual(abs(stored["created_at"] - int(time.time() * 1000)), 2000)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.state.iterdir()), [self.db])

    def test_task_defaults_to_decision_prompt(self):
        self.assertEqual(self.create()["task"], self.decision["prompt"])

    def test_duplicate_id_and_missing_update_do_not_overwrite(self):
        record = self.create()
        self.assert_error("session_exists", self.create, record["id"], task="replacement")
        self.assert_error("session_not_found", self.update, str(uuid.uuid4()))
        self.assertEqual(sessions.get(record["id"]), record)
        self.assertEqual(len(sessions.list_sessions()), 1)

    def test_list_is_bounded_deterministic_and_excludes_snapshots(self):
        records = []
        for index in range(23):
            with mock.patch.object(sessions.time, "time_ns", return_value=1000_000_000 + index // 2 * 1_000_000):
                records.append(self.create())
        expected = sorted(records, key=lambda record: (record["created_at"], record["id"]), reverse=True)
        summaries = sessions.list_sessions()
        self.assertEqual([record["id"] for record in summaries], [r["id"] for r in expected[:20]])
        self.assertEqual(sessions.list_sessions(limit=3), summaries[:3])
        self.assert_error("session_invalid", sessions.list_sessions, limit=2**100)
        for summary in summaries:
            self.assertTrue({"task", "handoff", "decision"}.isdisjoint(summary))
            self.assertEqual(summary["status"], "in_progress")

    def test_reads_do_not_rewrite_valid_store_or_infer_dead_owner_completion(self):
        record = self.create()
        self.sql("UPDATE sessions SET owner_pid = ? WHERE id = ?", (2147483647, record["id"]))
        before = self.db.read_bytes(), self.db.stat().st_mtime_ns, sorted(self.state.iterdir())
        self.assertEqual(sessions.get(record["id"])["status"], "in_progress")
        self.assertEqual(sessions.list_sessions()[0]["status"], "in_progress")
        self.assertEqual((self.db.read_bytes(), self.db.stat().st_mtime_ns, sorted(self.state.iterdir())), before)

    def test_parent_requires_confirmation_directory_and_handoff_without_updating_parent(self):
        parent = self.create()
        handoff = {"path": str(self.tmp / "handoff.md"), "text": "summary"}
        self.assert_error("session_not_found", self.create, parent_id=str(uuid.uuid4()), handoff=handoff)
        for state in ("not_sent", "unknown"):
            parent = self.update(parent["id"], prompt_status=state)
            self.assert_error("session_parent_unconfirmed", self.create, parent_id=parent["id"], handoff=handoff)
        # Failed completion wait is still a confirmed parent activity observation.
        parent = self.update(parent["id"], stage="wait", status="failed", prompt_status="confirmed", error="timeout")
        self.assert_error("handoff_required", self.create, parent_id=parent["id"])
        other = self.tmp / "other"
        other.mkdir()
        self.assert_error("session_cwd_mismatch", self.create, parent_id=parent["id"], cwd=str(other), handoff=handoff)
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp, target_is_directory=True)
        child = self.create(parent_id=parent["id"], cwd=str(alias), phase="review", handoff=handoff)
        self.assertNotEqual(child["id"], parent["id"])
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["cwd"], parent["cwd"])
        self.assertEqual(sessions.get(parent["id"]), parent)
        self.assertEqual(len(sessions.list_sessions()), 2)

    def test_handoff_is_snapshot_and_survives_source_change_and_removal(self):
        source = self.tmp / "handoff.md"
        source.write_text("summary before change", encoding="utf-8")
        parent = self.create()
        self.update(parent["id"], prompt_status="confirmed")
        child = self.create(parent_id=parent["id"], handoff={"path": str(source), "text": source.read_text()})
        source.write_text("new summary", encoding="utf-8")
        self.assertEqual(sessions.get(child["id"])["handoff"]["text"], "summary before change")
        source.unlink()
        self.assertEqual(sessions.get(child["id"])["handoff"], child["handoff"])

    def test_cwd_phase_and_handoff_validation(self):
        ordinary_file = self.tmp / "file"
        ordinary_file.touch()
        for cwd in (str(self.tmp / "absent"), str(ordinary_file), str(ordinary_file / "child"), "", "\0"):
            with self.subTest(cwd=cwd):
                self.assert_error("invalid_cwd", self.create, cwd=cwd)
        for phase in ("", " ", "x" * 65, "review\nnext", "review\0", 9, None):
            with self.subTest(phase=phase):
                self.assert_error("session_invalid", self.create, phase=phase)
        for handoff in ({}, {"path": "relative", "text": "summary"},
                        {"path": str(self.tmp), "text": ""},
                        {"path": str(self.tmp), "text": "x" * (48 * 1024 + 1)},
                        {"path": str(self.tmp), "text": "\ud800"}):
            with self.subTest(handoff=handoff):
                self.assert_error("session_invalid", self.create, handoff=handoff)
        self.assertEqual(self.create(phase="x" * 64)["phase"], "x" * 64)

    def test_invalid_json_input_is_safe(self):
        for value in (object(), float("nan"), {"nested": object()}):
            self.assert_error("session_invalid", self.create, decision={"prompt": "secret", "data": value})
        self.assertFalse(self.state.exists())

    def test_confirmed_cannot_revert_and_status_cannot_manufacture_confirmation(self):
        record = self.create()
        self.assert_error("session_invalid", self.update, record["id"], status="confirmed")
        self.update(record["id"], prompt_status="unknown")
        self.update(record["id"], prompt_status="not_sent")
        confirmed = self.update(record["id"], prompt_status="confirmed")
        for state in ("not_sent", "unknown"):
            self.assert_error("session_invalid", self.update, record["id"], prompt_status=state)
            self.assertEqual(sessions.get(record["id"]), confirmed)

    def test_invalid_existing_stores_are_not_initialized_or_rebuilt(self):
        self.state.mkdir(parents=True)
        for content in (b"", b"not a database\nsecret task SQL"):
            with self.subTest(content=content):
                self.db.write_bytes(content)
                before = self.db.stat().st_mtime_ns
                self.assert_error("session_store_error", sessions.list_sessions)
                self.assert_error("session_store_error", sessions.get, str(uuid.uuid4()))
                self.assert_error("session_store_error", self.create)
                self.assertEqual(self.db.read_bytes(), content)
                self.assertEqual(self.db.stat().st_mtime_ns, before)
                self.assertEqual(list(self.state.iterdir()), [self.db])

    def test_schema_version_gate_on_reads_and_writes(self):
        record = self.create()
        for version in (0, sessions.SCHEMA_VERSION + 1):
            self.sql(f"PRAGMA user_version = {version}")
            before = self.db.read_bytes(), self.db.stat().st_mtime_ns
            self.assert_error("session_store_error", sessions.get, record["id"])
            self.assert_error("session_store_error", sessions.list_sessions)
            self.assert_error("session_store_error", self.create)
            self.assert_error("session_store_error", self.update, record["id"])
            self.assertEqual((self.db.read_bytes(), self.db.stat().st_mtime_ns), before)

    def test_invalid_stored_records_fail_closed_on_get_list_and_update(self):
        record = self.create()
        for column, value in (("decision", "{invalid"),
                              ("decision", '{"prompt":"\\ud800"}'), ("workspace", "[]"),
                              ("handoff", '{"path":"/absolute","text":8}'),
                              ("phase", "review\nnext"), ("schema_version", 999),
                              ("dispatch_id", str(uuid.uuid4())), ("status", "confirmed")):
            with self.subTest(column=column):
                self.sql(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, record["id"]))
                self.assert_error("session_store_error", sessions.get, record["id"])
                self.assert_error("session_store_error", sessions.list_sessions)
                self.assert_error("session_store_error", self.update, record["id"])
                original = record[column]
                if column in {"decision", "workspace", "handoff"} and original is not None:
                    original = json.dumps(original)
                self.sql(f"UPDATE sessions SET {column} = ? WHERE id = ?", (original, record["id"]))

    def test_os_and_sqlite_errors_are_wrapped_without_diagnostics(self):
        for exception in (OSError("private task"), sqlite3.DatabaseError("secret SQL")):
            with mock.patch.object(sessions.sqlite3, "connect", side_effect=exception):
                error = self.assert_error("session_store_error", self.create)
                self.assertNotIn("private task", str(error))
                self.assertNotIn("secret SQL", str(error))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_busy_timeout_is_bounded_and_failed_transaction_does_not_modify_record(self):
        record = self.create()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            with mock.patch.object(sessions, "BUSY_TIMEOUT_MS", 50):
                started = time.monotonic()
                self.assert_error("session_store_busy", self.update, record["id"])
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.03)
                self.assertLess(elapsed, 2)
                self.assertTrue(conn.in_transaction)
        finally:
            conn.close()
        self.assertEqual(sessions.get(record["id"]), record)

    def test_extended_busy_and_locked_codes_use_safe_busy_error(self):
        for code in (sqlite3.SQLITE_BUSY | (3 << 8), sqlite3.SQLITE_LOCKED | (1 << 8)):
            with self.subTest(code=code):
                failure = sqlite3.OperationalError("secret task and SQL")
                failure.sqlite_errorcode = code
                with mock.patch.object(sessions.sqlite3, "connect", side_effect=failure):
                    error = self.assert_error("session_store_busy", self.create)
                self.assertEqual(error.message, "session store is busy")
                self.assertNotIn("secret", str(error))

    def test_concurrent_first_creates_and_independent_updates_keep_every_session(self):
        barrier = threading.Barrier(12)

        def worker(index):
            barrier.wait(timeout=10)
            record = self.create(task=f"task-{index}")
            for step in range(5):
                self.update(record["id"], stage="start", agent=f"worker-{index}", pane_id=f"pane-{index}")
            return self.update(record["id"], stage="complete", status="confirmed", prompt_status="confirmed")

        with ThreadPoolExecutor(max_workers=12) as pool:
            records = list(pool.map(worker, range(12)))
        self.assertEqual(len(sessions.list_sessions()), 12)
        for index, record in enumerate(records):
            self.assertEqual(sessions.get(record["id"]), record)
            self.assertEqual((record["agent"], record["pane_id"], record["task"]),
                             (f"worker-{index}", f"pane-{index}", f"task-{index}"))
        self.assertEqual(list(self.state.iterdir()), [self.db])

    def test_concurrent_read_modify_write_preserves_fields_of_same_attempt(self):
        record = self.create()
        barrier = threading.Barrier(2)

        def worker(**kwargs):
            barrier.wait(timeout=10)
            return self.update(record["id"], **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, agent="worker"), pool.submit(worker, pane_id="pane")]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual((sessions.get(record["id"])["agent"], sessions.get(record["id"])["pane_id"]),
                         ("worker", "pane"))

    def test_independent_process_writers_keep_all_records(self):
        script = """
import sys, uuid
from router import sessions
r = sessions.create(str(uuid.uuid4()), decision={'prompt': sys.argv[2]}, cwd=sys.argv[1])
sessions.update(r['id'], stage='complete', status='confirmed', prompt_status='confirmed')
print(r['id'])
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.tmp), f"task-{i}"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for i in range(5)]
        for index, process in enumerate(processes):
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            record = sessions.get(stdout.strip())
            self.assertEqual((record["task"], record["prompt_status"]), (f"task-{index}", "confirmed"))
        self.assertEqual(len(sessions.list_sessions()), 5)


if __name__ == "__main__":
    unittest.main()
