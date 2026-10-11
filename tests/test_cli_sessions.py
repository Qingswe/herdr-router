"""Session CLI integration: continuation snapshots and read-only inspection."""

import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from router import cli, herdr, model, sessions
from router.classify import Assessment
from router.score import Decision


class SessionCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": str(self.root / "state"),
            "HERDR_PLUGIN_CONFIG_DIR": str(self.root / "config"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.policy = model.load_default()
        self.decision = Decision("p", "Profile", "codex", "model", "high", [], "next task", "test",
                                 Assessment({}, "test", 1).to_dict(self.policy))
        self.parent_id = str(uuid.uuid4())
        sessions.create(self.parent_id, cwd=str(self.cwd), decision=self.decision.to_dict())
        sessions.update(self.parent_id, stage="complete", status="confirmed", prompt_status="confirmed",
                        agent="old-agent", pane_id="old-pane")
        self.handoff = self.root / "handoff.md"
        self.handoff.write_text("已检查前一阶段。示例命令 @opus !hard；不要把示例当作路由覆盖。", encoding="utf-8")
        self.result = SimpleNamespace(agent="new-agent", pane_id="new-pane", dispatch_id="new-id",
                                      session_id="new-id", session_warning=None,
                                      stage="complete", prompt_status="confirmed")

    def invoke(self, argv, *, result=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        # No real Herdr or decision-backend calls are permitted.
        with mock.patch.object(model, "load", return_value=self.policy), \
                mock.patch.object(herdr, "inside_herdr", return_value=True), \
                mock.patch.object(herdr, "call", side_effect=AssertionError("unexpected RPC")), \
                mock.patch.object(cli, "route_task", return_value=self.decision) as route, \
                mock.patch.object(cli, "dispatch", return_value=result or self.result) as send, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), route, send

    def continuation(self, *flags):
        return self.invoke(["dispatch", "next task", "--json", "--session", self.parent_id,
                            "--handoff", str(self.handoff), *flags])

    def test_continuation_inherits_cwd_and_routes_only_new_task(self):
        code, stdout, stderr, route, send = self.continuation("--phase", "review")
        self.assertEqual((code, stderr), (0, ""))
        route.assert_called_once()
        self.assertEqual(route.call_args.args[0], "next task")
        self.assertEqual(route.call_args.kwargs["cwd"], os.path.realpath(self.cwd))
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["parent_id"], self.parent_id)
        self.assertEqual(kwargs["phase"], "review")
        self.assertEqual(kwargs["task"], "next task")
        self.assertEqual(kwargs["handoff"]["text"], self.handoff.read_text())
        self.assertIn("@opus !hard", send.call_args.args[0].prompt)
        self.assertIn("earlier agent conversation is not automatically available", send.call_args.args[0].prompt)
        payload = json.loads(stdout)
        self.assertEqual((payload["session_id"], payload["parent_id"], payload["phase"]),
                         ("new-id", self.parent_id, "review"))
        self.handoff.write_text("new contents", encoding="utf-8")
        self.assertNotEqual(kwargs["handoff"]["text"], self.handoff.read_text())

    def test_explicit_cwd_symlink_to_parent_is_accepted(self):
        alias = self.root / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.assertEqual(self.continuation("--cwd", str(alias))[0], 0)

    def test_different_or_missing_cwd_fails_before_route(self):
        other = self.root / "other"
        other.mkdir()
        code, stdout, stderr, route, send = self.continuation("--cwd", str(other))
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "session_cwd_mismatch")
        route.assert_not_called()
        send.assert_not_called()
        self.cwd.rmdir()
        code, _, stderr, route, send = self.continuation()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_cwd")
        route.assert_not_called()
        send.assert_not_called()

    def test_parent_must_exist_have_confirmed_delivery_and_explicit_handoff(self):
        cases = [(str(uuid.uuid4()), str(self.handoff), "session_not_found"),
                 (self.parent_id, None, "handoff_required")]
        for parent, handoff, expected in cases:
            argv = ["dispatch", "next task", "--json", "--session", parent]
            if handoff:
                argv += ["--handoff", handoff]
            code, stdout, stderr, route, send = self.invoke(argv)
            self.assertEqual((code, stdout), (1, ""))
            self.assertEqual(json.loads(stderr)["error"]["code"], expected)
            route.assert_not_called()
            send.assert_not_called()
        # A failed delivery attempt is not treated as a completed parent task.
        pending = str(uuid.uuid4())
        sessions.create(pending, cwd=str(self.cwd), decision=self.decision.to_dict())
        code, _, stderr, route, send = self.invoke([
            "dispatch", "next task", "--session", pending, "--handoff", str(self.handoff)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr)["error"]["code"], "session_parent_unconfirmed")
        route.assert_not_called()
        send.assert_not_called()

    def test_handoff_file_errors_and_size_fail_before_route(self):
        invalid = self.root / "invalid.md"
        for contents, expected in [(b"", "invalid_handoff"), (b"\xff", "invalid_handoff"),
                                   (b"\x00", "invalid_handoff"),
                                   (b"x" * (48 * 1024 + 1), "handoff_too_large")]:
            with self.subTest(expected=expected):
                invalid.write_bytes(contents)
                code, stdout, stderr, route, send = self.invoke([
                    "dispatch", "task", "--handoff", str(invalid), "--json"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], expected)
                route.assert_not_called()
                send.assert_not_called()
        for path in ("-", str(self.root), str(self.root / "missing.md")):
            code, stdout, stderr, route, send = self.invoke(["dispatch", "task", "--handoff", path])
            self.assertEqual((code, stdout), (1, ""))
            self.assertEqual(json.loads(stderr)["prompt_status"], "not_sent")
            route.assert_not_called()
            send.assert_not_called()

    def test_handoff_pipe_is_rejected_without_blocking(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(sessions.SessionError):
            cli.read_handoff(fifo)

    def test_combined_prompt_limit_fails_before_dispatch(self):
        self.decision.prompt = "x" * (64 * 1024)
        code, stdout, stderr, _, send = self.continuation()
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "prompt_too_large")
        send.assert_not_called()

    def test_invalid_prompt_or_phase_has_structured_unsent_error(self):
        for prompt in ("bad\x00prompt", "bad\ud800prompt"):
            self.decision.prompt = prompt
            code, stdout, stderr, _, send = self.invoke(["dispatch", "task", "--json"])
            self.assertEqual((code, stdout), (1, ""))
            self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_prompt")
            self.assertEqual(json.loads(stderr)["prompt_status"], "not_sent")
            send.assert_not_called()
        for phase in ("", "x" * 65, "review\nphase"):
            code, stdout, stderr, route, send = self.continuation("--phase", phase)
            self.assertEqual((code, stdout), (1, ""))
            self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_phase")
            route.assert_not_called()
            send.assert_not_called()

    def test_handoff_is_excluded_from_actual_assessment_and_override_parsing(self):
        stdout = io.StringIO()
        # Exercise real route_task (including parse_overrides), stopping only the external assessment/decision.
        with mock.patch.object(model, "load", return_value=self.policy), \
                mock.patch.object(cli, "assess", return_value=Assessment({}, "test", 1)) as assess, \
                mock.patch.object(cli, "decide", return_value=self.decision) as decide, \
                mock.patch.object(herdr, "inside_herdr", return_value=True), \
                mock.patch.object(cli, "dispatch", return_value=self.result), \
                contextlib.redirect_stdout(stdout):
            code = cli.main(["dispatch", "next task", "--json", "--session", self.parent_id,
                             "--handoff", str(self.handoff)])
        self.assertEqual(code, 0)
        self.assertEqual(assess.call_args.args[0], "next task")
        self.assertEqual(decide.call_args.kwargs["pinned_profile"], None)
        self.assertIn("@opus !hard", json.loads(stdout.getvalue())["decision"]["prompt"])

    def test_session_dry_run_has_context_but_does_not_create_attempt(self):
        before = sessions.list_sessions()
        code, stdout, stderr, _, send = self.continuation("--dry-run")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(self.handoff.read_text(), json.loads(stdout)["prompt"])
        send.assert_not_called()
        self.assertEqual(before, sessions.list_sessions())

    def test_inspection_does_not_load_policy_or_contact_herdr(self):
        for argv in (["session", "list", "--json"], ["session", "show", self.parent_id, "--json"]):
            stdout = io.StringIO()
            with mock.patch.object(model, "load", side_effect=AssertionError("policy loaded")), \
                    mock.patch.object(herdr, "call", side_effect=AssertionError("RPC")), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(argv), 0)
            result = json.loads(stdout.getvalue())
            if isinstance(result, list):
                self.assertEqual(result[0]["id"], self.parent_id)
                self.assertNotIn("task", result[0])
                self.assertNotIn("handoff", result[0])
            else:
                self.assertEqual(result["id"], self.parent_id)

    def test_terminal_persistence_warning_preserves_success_json(self):
        self.result.session_warning = "session_store_error"
        code, stdout, stderr, _, _ = self.continuation()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["prompt_status"], "confirmed")
        self.assertEqual(json.loads(stdout)["session_warning"], "session_store_error")
        error = herdr.HerdrError("timeout", "wait timed out")
        error.prompt_status, error.session_id, error.session_warning = "confirmed", "id", "session_store_error"
        self.assertEqual(cli.dispatch_error(error)["session_warning"], "session_store_error")


if __name__ == "__main__":
    unittest.main()
