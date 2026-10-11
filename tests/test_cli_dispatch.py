"""Dispatch outcomes must survive the CLI and popup without encouraging duplicate work."""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from router import cli, herdr, model
from router.classify import Assessment
from router.score import Decision


class DispatchCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": self.tmp.name + "/cfg",
            "HERDR_PLUGIN_STATE_DIR": self.tmp.name + "/state",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.policy = model.load_default()
        self.decision = Decision("p", "Profile", "codex", "model", "high", [], "task", "test",
                                 Assessment({}, "test", 1).to_dict(self.policy))
        self.outcome = SimpleNamespace(agent="worker", pane_id="w1:p2", dispatch_id="attempt-1",
                                       stage="complete", prompt_status="confirmed")

    def invoke(self, *, error=None, policy=None, flags=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(model, "load", return_value=policy or self.policy), \
                mock.patch.object(cli, "route_task", return_value=self.decision) as route, \
                mock.patch.object(herdr, "inside_herdr", return_value=True), \
                mock.patch.object(cli, "dispatch", side_effect=error, return_value=self.outcome) as send, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["dispatch", "task", "--json", *flags])
        return code, stdout.getvalue(), stderr.getvalue(), send, route

    def failure(self, prompt_status, stage):
        e = herdr.HerdrError("timeout", "RPC timed out")
        e.pane_id, e.agent, e.dispatch_id = "w1:p2", "worker", "attempt-1"
        e.stage, e.prompt_status = stage, prompt_status
        return e

    def test_success_stdout_is_one_json_object_and_reports_confirmation(self):
        code, stdout, stderr, send, _ = self.invoke()
        result = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(result["prompt_status"], "confirmed")
        self.assertEqual(result["dispatch_id"], "attempt-1")
        self.assertEqual((result["agent"], result["pane_id"]), ("worker", "w1:p2"))
        send.assert_called_once()

    def test_failure_json_preserves_location_and_delivery_state(self):
        for state, stage in (("not_sent", "start"), ("unknown", "prompt"), ("confirmed", "wait")):
            with self.subTest(state=state):
                code, stdout, stderr, send, _ = self.invoke(error=self.failure(state, stage))
                result = json.loads(stderr)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(result["prompt_status"], state)
                self.assertEqual(result["stage"], stage)
                self.assertEqual(result["dispatch_id"], "attempt-1")
                self.assertEqual((result["agent"], result["pane_id"]), ("worker", "w1:p2"))
                if state != "not_sent":
                    self.assertIn("不要", result["recovery"])
                    self.assertNotIn("任务未发送", result["recovery"])
                send.assert_called_once()

    def test_wait_and_timeout_are_forwarded_without_changing_json_contract(self):
        code, stdout, _, send, _ = self.invoke(flags=["--wait", "--timeout", "10000"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["prompt_status"], "confirmed")
        self.assertTrue(send.call_args.kwargs["wait"])
        self.assertEqual(send.call_args.kwargs["wait_timeout_ms"], 10000)

    def test_unexpected_failure_preserves_context_as_json_without_exception_text(self):
        error = ValueError("private task body should not be echoed")
        error.pane_id, error.agent, error.dispatch_id = "w1:p2", "worker", "attempt-1"
        error.stage, error.prompt_status, error.session_id = "prompt", "unknown", "attempt-1"
        code, stdout, stderr, _, _ = self.invoke(error=error)
        self.assertEqual((code, stdout), (1, ""))
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "unexpected_error")
        self.assertEqual((payload["prompt_status"], payload["pane_id"], payload["session_id"]),
                         ("unknown", "w1:p2", "attempt-1"))
        self.assertNotIn("private task body", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_invalid_saved_dispatch_settings_fail_before_assessment_or_pane_creation(self):
        for bad in ("60s", 900000, True):
            with self.subTest(timeout=bad):
                policy = copy.deepcopy(self.policy)
                policy["dispatch"]["agent_start_timeout_ms"] = bad
                code, stdout, stderr, send, route = self.invoke(policy=policy)
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_dispatch_config")
                self.assertEqual(json.loads(stderr)["prompt_status"], "not_sent")
                route.assert_not_called()
                send.assert_not_called()

    def test_dry_run_never_dispatches(self):
        code, stdout, stderr, send, _ = self.invoke(flags=["--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["profile_id"], "p")
        send.assert_not_called()

    def test_popup_unknown_delivery_returns_failure_and_does_not_claim_unsent(self):
        stdout = io.StringIO()
        with mock.patch.object(cli, "_ask", side_effect=["task", "", ""]), \
                mock.patch.object(cli, "route_task", return_value=self.decision), \
                mock.patch.object(cli, "dispatch", side_effect=self.failure("unknown", "prompt")) as send, \
                mock.patch.object(herdr, "pane_screen", return_value="agent is working"), \
                mock.patch.object(cli.signal, "signal"), contextlib.redirect_stdout(stdout):
            code = cli.run_popup(self.policy)
        self.assertEqual(code, 1)
        self.assertIn("w1:p2", stdout.getvalue())
        self.assertIn("任务可能已送达", stdout.getvalue())
        self.assertNotIn("任务没有发出", stdout.getvalue())
        self.assertNotIn("任务未发送", stdout.getvalue())
        send.assert_called_once()


class DispatchPolicyTest(unittest.TestCase):
    def test_invalid_launch_settings_cannot_be_saved(self):
        for bad in ({"direction": "sideways"}, {"agent_start_timeout_ms": "60s"},
                    {"agent_start_timeout_ms": 900000}, {"agent_start_timeout_ms": True}, []):
            with self.subTest(dispatch=bad), tempfile.TemporaryDirectory() as tmp:
                policy = model.load_default()
                policy["dispatch"] = bad
                with self.assertRaises(model.InvalidPolicy):
                    model.save(policy, path=Path(tmp) / "policy.json")


if __name__ == "__main__":
    unittest.main()
