"""Real temporary Git worktrees with mocked Herdr: isolation and persistence boundaries."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from router import cli, herdr, model, sessions, workspace
from router.dispatch import dispatch
from tests.test_dispatch import decision

_RUN = subprocess.run
_POPEN = subprocess.Popen


class WorktreeDispatchTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo with spaces"
        self.state = self.root / "state"
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_PLUGIN_CONFIG_DIR": str(self.root / "config"),
        }))
        self.git("init", "--quiet", str(self.repo), at=self.root)
        hooks = self.root / "empty-hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        (self.repo / "file.txt").write_text("baseline\n")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("original\n")
        self.git("add", "file.txt", "src/main.py")
        self.git("-c", "commit.gpgsign=false", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--quiet", "-m", "baseline")
        self.prepare = self.stack.enter_context(mock.patch("router.dispatch.agents.prepare"))
        for name, result in {"unique_agent_name": "worker", "current_pane_id": "source",
                             "auto_direction": "right", "split": "pane", "agent_start": {},
                             "agent_prompt": {}, "agent_wait": {}}.items():
            setattr(self, name, self.stack.enter_context(mock.patch.object(herdr, name, return_value=result)))
        self.d = decision()

    def git(self, *args, at=None, check=True):
        result = _RUN(["git", "-C", str(at or self.repo), *args], capture_output=True,
                      text=True, timeout=10)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def send(self, **kwargs):
        return dispatch(self.d, **{"cwd": str(self.repo), "target_pane": "source", **kwargs})

    def test_default_dispatch_runs_no_git_even_for_dirty_shared_directory(self):
        (self.repo / "file.txt").write_text("existing edit")
        with mock.patch.object(workspace, "_git", side_effect=AssertionError("default must not scan Git")):
            result = self.send()
        self.assertEqual(result.workspace, {"kind": "directory", "cwd": str(self.repo)})
        self.split.assert_called_once_with("source", "right", str(self.repo))

    def test_creation_is_persisted_before_git_and_ready_before_launch(self):
        observed = []

        def popen(args, **kwargs):
            if "add" in args and "worktree" in args:
                record = sessions.get(sessions.list_sessions()[0]["id"])
                self.assertEqual(record["workspace"]["state"], "creating")
                self.assertEqual(record["cwd"], str(self.repo))
                self.assertEqual(record["prompt_status"], "not_sent")
                connection = sqlite3.connect(self.state / "sessions.sqlite3", timeout=0.1)
                try:
                    connection.execute("BEGIN IMMEDIATE")  # no lock is held during Git
                finally:
                    connection.close()
                observed.append("before-add")
            return _POPEN(args, **kwargs)

        def prepare(*args):
            record = sessions.get(sessions.list_sessions()[0]["id"])
            self.assertEqual(record["workspace"]["state"], "ready")
            self.assertEqual(record["cwd"], record["workspace"]["cwd"])
            observed.append("before-launch")

        self.prepare.side_effect = prepare
        with mock.patch.object(workspace.subprocess, "Popen", side_effect=popen):
            result = self.send(isolation="worktree")
        self.assertEqual(observed, ["before-add", "before-launch"])
        stored = sessions.get(result.session_id)
        self.assertEqual(stored["workspace"], result.workspace)
        self.assertEqual(self.split.call_args.args[-1], result.workspace["cwd"])
        self.assertTrue(Path(result.workspace["path"]).is_dir())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", at=Path(stored["cwd"])).stdout.strip(),
                         result.workspace["branch"])
        self.assertEqual(stored["schema_version"], 1)

    def test_worktree_edits_leave_source_unchanged_and_subdirectory_is_preserved(self):
        def edit(*args, **kwargs):
            record = sessions.get(sessions.list_sessions()[0]["id"])
            Path(record["cwd"], "main.py").write_text("isolated edit")
            return {}
        self.agent_prompt.side_effect = edit
        result = self.send(cwd=str(self.repo / "src"), isolation="worktree")
        self.assertEqual(Path(result.workspace["cwd"]).name, "src")
        self.assertEqual(Path(result.workspace["cwd"], "main.py").read_text(), "isolated edit")
        self.assertEqual((self.repo / "src" / "main.py").read_text(), "original\n")

    def test_continuation_reuses_dirty_worktree_without_new_checkout(self):
        parent = self.send(isolation="worktree")
        cwd = parent.workspace["cwd"]
        Path(cwd, "file.txt").write_text("unfinished change")
        before = self.git("worktree", "list", "--porcelain").stdout
        with mock.patch.object(workspace, "_snapshot", side_effect=AssertionError("reuse must not scan status")):
            child = self.send(cwd=cwd, parent_id=parent.session_id,
                              handoff={"path": str(self.root / "handoff.md"), "text": "parent inspected"})
        self.assertEqual(child.workspace, parent.workspace)
        self.assertEqual(sessions.get(child.session_id)["parent_id"], parent.session_id)
        self.assertNotEqual(child.session_id, parent.session_id)
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before)
        self.assertEqual(Path(cwd, "file.txt").read_text(), "unfinished change")

    def test_start_failure_keeps_created_worktree_and_location(self):
        self.agent_start.side_effect = herdr.HerdrError("agent_not_ready", "blocked")
        with self.assertRaises(herdr.HerdrError) as caught:
            self.send(isolation="worktree")
        error = caught.exception
        self.assertEqual((error.stage, error.prompt_status), ("start", "not_sent"))
        self.assertTrue(Path(error.workspace["path"]).is_dir())
        self.assertEqual(sessions.get(error.session_id)["workspace"], error.workspace)
        recovery = cli.dispatch_recovery(error)
        self.assertIn("pane", recovery)
        self.assertIn("启动或确认问题", recovery)
        self.assertIn(error.workspace["path"], recovery)
        self.assertNotIn("检查创建结果", recovery)
        self.agent_prompt.assert_not_called()

    def test_failed_planned_checkpoint_prevents_git_mutation_and_launch(self):
        with mock.patch.object(sessions, "set_workspace", side_effect=sessions.SessionError("session_store_busy", "busy")), \
                self.assertRaises(herdr.HerdrError) as caught:
            self.send(isolation="worktree")
        error = caught.exception
        self.assertEqual((error.code, error.prompt_status), ("session_store_busy", "not_sent"))
        self.assertEqual(error.workspace["state"], "failed")
        self.assertFalse(Path(error.workspace["path"]).exists())
        self.assertEqual(self.git("show-ref", "--verify", "--quiet", "refs/heads/" + error.workspace["branch"],
                                  check=False).returncode, 1)
        self.prepare.assert_not_called()
        self.split.assert_not_called()

    def test_failed_ready_checkpoint_keeps_artifact_but_never_opens_pane(self):
        original = sessions.set_workspace
        def persist(id, metadata):
            if metadata.get("state") == "ready":
                raise sessions.SessionError("session_store_busy", "busy")
            return original(id, metadata)
        with mock.patch.object(sessions, "set_workspace", side_effect=persist), \
                self.assertRaises(herdr.HerdrError) as caught:
            self.send(isolation="worktree")
        error = caught.exception
        self.assertEqual(error.prompt_status, "not_sent")
        self.assertTrue(Path(error.workspace["path"]).is_dir())
        self.assertEqual(sessions.get(error.session_id)["workspace"]["state"], "creating")
        self.assertIn("session_store_error", error.session_warning)
        self.split.assert_not_called()

    def test_timeout_preserves_unknown_workspace_in_error_session_and_log(self):
        def fail(plan, id, budget, *, before_create):
            root = str(self.state / "worktrees" / "repo" / id)
            meta = {"kind": "worktree", "cwd": str(self.repo), "path": root, "branch": "router/" + id,
                    "common_dir": str(self.repo / ".git"), "source_root": str(self.repo),
                    "source_cwd": str(self.repo), "relative_cwd": ".", "base_head": plan["snapshot"]["head"],
                    "state": "creating", "creation_ms": None}
            before_create(meta)
            raise workspace.WorkspaceError("worktree_timeout", "timed out",
                                            workspace={**meta, "state": "unknown", "creation_ms": 50})
        with mock.patch.object(workspace, "materialize", side_effect=fail), self.assertRaises(herdr.HerdrError) as caught:
            self.send(isolation="worktree")
        error = caught.exception
        stored = sessions.get(error.session_id)
        self.assertEqual(stored["workspace"], error.workspace)
        self.assertEqual((stored["stage"], stored["prompt_status"], stored["workspace"]["state"]),
                         ("workspace", "not_sent", "unknown"))
        log = json.loads((self.state / "decisions.jsonl").read_text().splitlines()[0])
        self.assertEqual(log["workspace"], error.workspace)
        self.split.assert_not_called()

    def test_routing_race_fails_before_checkout_with_structured_json(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        def route(*args, **kwargs):
            (self.repo / "file.txt").write_text("edit during assessment")
            return self.d
        with mock.patch.object(cli, "route_task", side_effect=route), \
                mock.patch.object(herdr, "inside_herdr", return_value=True), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["dispatch", "task", "--json", "--cwd", str(self.repo), "--isolation", "worktree"])
        self.assertEqual((code, stdout.getvalue()), (1, ""))
        result = json.loads(stderr.getvalue())
        self.assertEqual(result["error"]["code"], "workspace_source_changed")
        self.assertEqual((result["stage"], result["prompt_status"]), ("workspace", "not_sent"))
        self.assertEqual(len(sessions.list_sessions()), 1)
        self.split.assert_not_called()

    def test_worktree_dry_run_is_read_only_and_reports_budget(self):
        stdout = io.StringIO()
        with mock.patch.object(cli, "route_task", return_value=self.d), redirect_stdout(stdout):
            code = cli.main(["dispatch", "task", "--dry-run", "--json", "--cwd", str(self.repo),
                             "--isolation", "worktree", "--worktree-timeout", "12345"])
        self.assertEqual(code, 0)
        plan = json.loads(stdout.getvalue())["workspace_plan"]
        self.assertEqual((plan["mode"], plan["timeout_ms"]), ("create", 12345))
        self.assertFalse(self.state.exists())
        self.split.assert_not_called()

    def test_session_workspace_cannot_be_changed_after_submission_or_escape_identity(self):
        result = self.send(isolation="worktree")
        before = sessions.get(result.session_id)
        with self.assertRaises(sessions.SessionError):
            sessions.set_workspace(result.session_id, {"kind": "directory", "cwd": str(self.repo)})
        self.assertEqual(sessions.get(result.session_id), before)


if __name__ == "__main__":
    unittest.main()
