"""Offline dispatch behavior: every CLI process is mocked and logs use temp dirs."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from router import herdr
from router.dispatch import dispatch
from router.score import Decision


def decision():
    return Decision(profile_id="test", profile_label="Test", kind="codex",
                    model="gpt-test", effort="high", args=["-m", "gpt-test"],
                    prompt="实现任务\n保留 $(shell); `text` & --flags", reason="test",
                    assessment={"source": "test"})


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": self.tmp, "HERDR_PLUGIN_STATE_DIR": self.tmp,
        }))
        self.stack.enter_context(mock.patch.object(herdr, "herdr_bin", return_value="/mock/herdr"))
        self.run = self.stack.enter_context(mock.patch.object(
            herdr.subprocess, "run", side_effect=AssertionError("unexpected CLI call")))
        self.d = decision()
        self.calls = mock.Mock()
        self.prepare = self.stack.enter_context(mock.patch("router.dispatch.agents.prepare"))
        self.calls.attach_mock(self.prepare, "prepare")
        self.original = {}
        for method, result in {
            "unique_agent_name": "r-test-1", "current_pane_id": "source",
            "auto_direction": "right", "split": "new-pane", "agent_start": {},
            "agent_prompt": {"result": {"state": "working"}},
            "agent_wait": {"result": {"state": "done"}},
        }.items():
            self.original[method] = getattr(herdr, method)
            patched = self.stack.enter_context(mock.patch.object(herdr, method, return_value=result))
            setattr(self, method, patched)
            self.calls.attach_mock(patched, method)

    def invoke(self, **kwargs):
        return dispatch(self.d, **{"target_pane": "source", "cwd": self.tmp, **kwargs})

    def records(self):
        path = Path(self.tmp) / "decisions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_record(self, record, stage, prompt_status, status, error=None,
                      pane_id="new-pane", agent="r-test-1"):
        self.assertTrue({"ts", "event", "cwd", "decision", "agent", "pane_id",
                         "dispatch_id", "stage", "prompt_status", "status"} <= record.keys())
        self.assertEqual(record["event"], "dispatch")
        self.assertEqual(record["cwd"], self.tmp)
        self.assertEqual(record["decision"], self.d.to_dict())
        self.assertEqual((record["stage"], record["prompt_status"], record["status"]),
                         (stage, prompt_status, status))
        self.assertEqual((record["pane_id"], record["agent"]), (pane_id, agent))
        self.assertEqual(uuid.UUID(record["dispatch_id"]).version, 4)
        if error is None:
            self.assertNotIn("error", record)
        else:
            self.assertEqual(record["error"], error)

    def failure(self, code, stage, prompt_status="not_sent", pane_id="new-pane",
                agent="r-test-1", **kwargs):
        count = len(self.records())
        with self.assertRaises(herdr.HerdrError) as caught:
            self.invoke(**kwargs)
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertEqual((error.stage, error.prompt_status, error.pane_id, error.agent),
                         (stage, prompt_status, pane_id, agent))
        self.assertEqual(len(self.records()), count + 1)
        record = self.records()[-1]
        self.assert_record(record, stage, prompt_status, "failed", code, pane_id, agent)
        self.assertEqual(error.dispatch_id, record["dispatch_id"])
        return error

    def test_default_confirms_activity_without_waiting_for_completion(self):
        result = self.invoke()
        self.agent_prompt.assert_called_once_with(
            "r-test-1", self.d.prompt, wait=True, until=("working", "blocked"), timeout_ms=15000)
        self.agent_wait.assert_not_called()
        self.agent_start.assert_called_once_with("r-test-1", "codex", "new-pane", self.d.args, 60000)
        self.assertEqual([call[0] for call in self.calls.mock_calls],
                         ["prepare", "unique_agent_name", "auto_direction", "split",
                          "agent_start", "agent_prompt"])
        self.assertEqual((result.stage, result.prompt_status), ("complete", "confirmed"))
        self.assertEqual(result.prompt_result, self.agent_prompt.return_value)
        self.assertEqual(len(self.records()), 1)
        self.assert_record(self.records()[0], "complete", "confirmed", "confirmed")
        self.assertEqual(result.dispatch_id, self.records()[0]["dispatch_id"])
        self.assertIn(result.dispatch_id[:8], self.unique_agent_name.call_args.args[0])

    def test_dispatch_ids_are_unique(self):
        first, second = self.invoke(), self.invoke()
        self.assertNotEqual(first.dispatch_id, second.dispatch_id)
        self.assertEqual(len(self.records()), 2)

    def test_resolves_current_pane_and_direction_before_split(self):
        self.invoke(target_pane=None)
        self.current_pane_id.assert_called_once_with()
        self.auto_direction.assert_called_once_with("source")
        self.split.assert_called_once_with("source", "right", self.tmp)

    def test_wait_confirms_first_then_uses_native_default_completion_states(self):
        for timeout in (None, 32000):
            with self.subTest(timeout=timeout):
                result = self.invoke(wait=True, wait_timeout_ms=timeout)
                self.assertEqual(self.calls.mock_calls[-2:], [
                    mock.call.agent_prompt("r-test-1", self.d.prompt, wait=True,
                                           until=("working", "blocked"), timeout_ms=15000),
                    mock.call.agent_wait("r-test-1", timeout_ms=timeout),
                ])
                self.assertEqual(result.prompt_result, self.agent_wait.return_value)
                self.assert_record(self.records()[-1], "complete", "confirmed", "confirmed")
        self.assertEqual(len(self.records()), 2)

    def test_completion_timeout_keeps_confirmed_delivery(self):
        self.agent_wait.side_effect = herdr.HerdrError("timeout", "completion deadline")
        self.failure("timeout", "wait", "confirmed", wait=True, wait_timeout_ms=25000)
        self.agent_prompt.assert_called_once()
        self.agent_wait.assert_called_once_with("r-test-1", timeout_ms=25000)

    def test_prompt_failures_send_once_and_keep_pane_and_unknown_delivery(self):
        for index, code in enumerate(("timeout", "unexpected_prompt_error", "exit_1", "rpc_error",
                                      "agent_not_found", "agent_not_started", "agent_not_running"), 1):
            with self.subTest(code=code):
                original = herdr.HerdrError(code, "prompt failed")
                self.agent_prompt.side_effect = original
                self.assertIs(self.failure(code, "prompt", "unknown"), original)
                self.assertEqual(self.agent_prompt.call_count, index)
                self.assertEqual(self.split.call_count, index)
                self.agent_wait.assert_not_called()

    def test_known_pre_write_prompt_errors_are_not_sent(self):
        for code in ("agent_blocked",):
            with self.subTest(code=code):
                self.agent_prompt.side_effect = herdr.HerdrError(code, "rejected before write")
                self.failure(code, "prompt")
                self.agent_wait.assert_not_called()

    def test_local_process_start_failure_is_not_sent_without_trusting_remote_error_codes(self):
        self.agent_prompt.side_effect = herdr.HerdrError("rpc_error", "exec failed", before_submission=True)
        self.failure("rpc_error", "prompt")
        self.agent_prompt.assert_called_once()
        self.agent_wait.assert_not_called()

    def test_stalled_observes_activity_once_without_resending(self):
        self.agent_prompt.side_effect = herdr.HerdrError("agent_prompt_stalled", "stalled")
        result = self.invoke()
        self.agent_prompt.assert_called_once()
        self.agent_wait.assert_called_once_with(
            "r-test-1", until=("working", "blocked"), timeout_ms=15000)
        self.assertEqual(result.prompt_status, "confirmed")
        self.assertEqual(len(self.records()), 1)
        self.assert_record(self.records()[0], "complete", "confirmed", "confirmed")

    def test_failed_stalled_observation_remains_unknown_even_for_blocked_error(self):
        for index, code in enumerate(("timeout", "agent_blocked", "agent_prompt_stalled"), 1):
            with self.subTest(code=code):
                self.agent_prompt.side_effect = herdr.HerdrError("agent_prompt_stalled", "stalled")
                self.agent_wait.side_effect = herdr.HerdrError(code, "observation failed")
                self.failure(code, "prompt", "unknown")
                self.assertEqual(self.agent_prompt.call_count, index)
                self.assertEqual(self.agent_wait.call_count, index)
                self.assertEqual(self.agent_wait.call_args,
                                 mock.call("r-test-1", until=("working", "blocked"), timeout_ms=15000))

    def test_stalled_observation_and_completion_are_separate_waits(self):
        self.agent_prompt.side_effect = herdr.HerdrError("agent_prompt_stalled", "stalled")
        self.agent_wait.side_effect = [{}, herdr.HerdrError("timeout", "completion deadline")]
        self.failure("timeout", "wait", "confirmed", wait=True)
        self.agent_prompt.assert_called_once()
        self.assertEqual(self.agent_wait.call_args_list, [
            mock.call("r-test-1", until=("working", "blocked"), timeout_ms=15000),
            mock.call("r-test-1", timeout_ms=None),
        ])

    def test_start_failure_does_not_prompt_or_wait(self):
        self.agent_start.side_effect = herdr.HerdrError("agent_not_ready", "startup blocked")
        self.failure("agent_not_ready", "start")
        self.agent_prompt.assert_not_called()
        self.agent_wait.assert_not_called()

    def test_prepare_os_error_logs_known_explicit_name_before_split(self):
        self.prepare.side_effect = PermissionError("cannot create settings")
        self.failure("prepare_failed", "prepare", pane_id=None, agent="chosen", name="chosen")
        self.unique_agent_name.assert_not_called()
        self.split.assert_not_called()
        self.agent_start.assert_not_called()

    def test_prepare_herdr_error_preserves_error_context(self):
        self.prepare.side_effect = herdr.HerdrError("prepare_error", "failed", agent="known")
        self.failure("prepare_error", "prepare", pane_id=None, agent="known")
        self.split.assert_not_called()

    def test_name_lookup_failure_is_logged_before_split(self):
        self.unique_agent_name.side_effect = herdr.HerdrError("rpc_error", "list failed")
        self.failure("rpc_error", "resolve", pane_id=None, agent=None)
        self.split.assert_not_called()

    def test_explicit_name_validation_happens_before_split(self):
        for name in ("", "-name", "9name", "UPPER", "a b", "a" * 33):
            with self.subTest(name=name):
                self.failure("invalid_agent_name", "resolve", pane_id=None, agent=name, name=name)
        self.unique_agent_name.assert_not_called()
        self.split.assert_not_called()

    def test_generated_name_is_also_validated_before_split(self):
        self.unique_agent_name.return_value = "Bad name"
        self.failure("invalid_agent_name", "resolve", pane_id=None, agent="Bad name")
        self.split.assert_not_called()

    def test_default_names_include_uuid_suffix_with_long_model_names(self):
        self.d.model = "provider/" + "long-model" * 20
        self.unique_agent_name.side_effect = self.original["unique_agent_name"]
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, '{"result":{"agents":[]}}', "")
        first, second = self.invoke(), self.invoke()
        for result in (first, second):
            self.assertRegex(result.agent, r"^[a-z][a-z0-9_-]{0,31}$")
            self.assertIn(result.dispatch_id[:8], result.agent)
        self.assertNotEqual(first.agent, second.agent)

    def test_current_pane_lookup_failure_logs_known_name(self):
        self.current_pane_id.side_effect = herdr.HerdrError("invalid_response", "bad pane reply")
        self.failure("invalid_response", "resolve", pane_id=None, target_pane=None)
        self.split.assert_not_called()

    def test_direction_lookup_failure_is_logged_before_split(self):
        self.auto_direction.side_effect = herdr.HerdrError("timeout", "layout timeout")
        self.failure("timeout", "resolve", pane_id=None)
        self.split.assert_not_called()

    def test_split_failure_preserves_pane_reported_by_cli(self):
        self.split.side_effect = herdr.HerdrError("split_failed", "partial failure", pane_id="partial-pane")
        self.failure("split_failed", "split", pane_id="partial-pane")
        self.agent_start.assert_not_called()
        self.agent_prompt.assert_not_called()

    def test_split_reply_shape_error_is_a_logged_herdr_error(self):
        self.split.side_effect = self.original["split"]
        with mock.patch.object(herdr, "call", return_value={"result": {"pane": {}}}):
            self.failure("invalid_response", "split", pane_id=None)
        self.agent_start.assert_not_called()

    def test_native_plain_text_prompt_success_confirms_dispatch_and_keeps_argv(self):
        self.d.prompt = "--leading\n$(never-run); `also-never-run` & | > 'quotes'"
        self.agent_prompt.side_effect = self.original["agent_prompt"]
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, "activity observed", "")
        result = self.invoke(direction="down")
        self.assertEqual(self.run.call_args.args[0], [
            "/mock/herdr", "agent", "prompt", "r-test-1", self.d.prompt,
            "--wait", "--until", "working", "--until", "blocked", "--timeout", "15000",
        ])
        self.assertFalse(self.run.call_args.kwargs.get("shell", False))
        self.assertEqual(result.prompt_status, "confirmed")
        self.assertEqual(result.prompt_result, {})

    def test_interruptions_and_system_exit_log_delivery_state_and_reraise(self):
        for operation, stage, delivery in (("prepare", "prepare", "not_sent"),
                                           ("agent_start", "start", "not_sent"),
                                           ("agent_prompt", "prompt", "unknown"),
                                           ("agent_wait", "wait", "confirmed")):
            for exception in (KeyboardInterrupt(), SystemExit(7)):
                with self.subTest(operation=operation, exception=type(exception).__name__):
                    operation_mock = getattr(self, operation)
                    operation_mock.side_effect = exception
                    count = len(self.records())
                    with self.assertRaises(type(exception)) as caught:
                        self.invoke(name="chosen", wait=stage == "wait")
                    self.assertIs(caught.exception, exception)
                    self.assertEqual(len(self.records()), count + 1)
                    self.assert_record(self.records()[-1], stage, delivery, "failed", "interrupted",
                                       None if stage == "prepare" else "new-pane", "chosen")
                    operation_mock.side_effect = None

    def test_programming_errors_are_logged_and_propagate_unchanged(self):
        error = ValueError("programming error")
        self.agent_prompt.side_effect = error
        with self.assertRaises(ValueError) as caught:
            self.invoke()
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.records()), 1)
        self.assert_record(self.records()[0], "prompt", "unknown", "failed", "unexpected_error")

    def test_log_os_error_does_not_change_success_or_failure(self):
        with mock.patch("router.dispatch.log.append", side_effect=OSError("log unavailable")) as append:
            self.assertEqual(self.invoke().prompt_status, "confirmed")
            failure = herdr.HerdrError("timeout", "prompt deadline")
            self.agent_prompt.side_effect = failure
            with self.assertRaises(herdr.HerdrError) as caught:
                self.invoke()
            self.assertIs(caught.exception, failure)
            self.assertEqual(failure.prompt_status, "unknown")
            self.assertEqual(append.call_count, 2)


class HerdrCallTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": tmp, "HERDR_PLUGIN_STATE_DIR": tmp,
        }))
        self.stack.enter_context(mock.patch.object(herdr, "herdr_bin", return_value="/mock/herdr"))
        self.run = self.stack.enter_context(mock.patch.object(herdr.subprocess, "run"))
        self.response("{}")

    def response(self, stdout, stderr="", returncode=0):
        self.run.return_value = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_generic_rpc_is_bounded_and_returns_json_object(self):
        self.response('{"result":{"value":1}}')
        self.assertEqual(herdr.call("agent", "get", "target"), {"result": {"value": 1}})
        self.assertEqual(self.run.call_args.args[0], ["/mock/herdr", "agent", "get", "target"])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 15.0)
        self.assertFalse(self.run.call_args.kwargs.get("shell", False))

    def test_generic_invalid_empty_and_non_object_json_are_herdr_errors(self):
        for output in ("", "not JSON" * 1000, "null", "[]", '"text"', "42"):
            with self.subTest(output=output[:20]):
                self.response(output)
                with self.assertRaises(herdr.HerdrError) as caught:
                    herdr.call("agent", "get", "target")
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertLess(len(caught.exception.message), 200)

    def test_subprocess_os_error_and_timeout_are_safe_herdr_errors(self):
        task = "private task\n$(never-run)"
        for code, exception in (
            ("rpc_error", FileNotFoundError(2, task)),
            ("timeout", subprocess.TimeoutExpired(["herdr", "agent", "prompt", "target", task],
                                                 20, output=task * 1000, stderr=task)),
        ):
            with self.subTest(code=code):
                self.run.side_effect = exception
                with self.assertRaises(herdr.HerdrError) as caught:
                    herdr.agent_prompt("target", task, wait=True, timeout_ms=15000)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.before_submission, code == "rpc_error")
                self.assertNotIn(task, str(caught.exception))
                self.assertLess(len(str(caught.exception)), 200)

    def test_nonzero_json_errors_are_parsed_from_either_stream(self):
        payload = {"error": {"code": "agent_blocked", "message": "blocked before write",
                              "pane_id": "p", "agent": "a"}}
        for stream in ("stdout", "stderr"):
            for output in (json.dumps(payload), "diagnostic\n" + json.dumps(payload),
                           json.dumps(payload, indent=2)):
                with self.subTest(stream=stream, output=output[:20]):
                    self.response(output if stream == "stdout" else "noise",
                                  output if stream == "stderr" else "noise", returncode=1)
                    with self.assertRaises(herdr.HerdrError) as caught:
                        herdr.agent_prompt("a", "task", wait=True, timeout_ms=15000)
                    self.assertEqual(caught.exception.code, "agent_blocked")
                    self.assertEqual((caught.exception.pane_id, caught.exception.agent), ("p", "a"))

    def test_nonzero_unstructured_errors_do_not_expose_task_or_large_output(self):
        task = "do something secret"
        for stderr in ("usage " + task * 1000, '{"error":null}', "[]", ""):
            with self.subTest(stderr=stderr[:20]):
                self.response(task * 1000, stderr, returncode=2)
                with self.assertRaises(herdr.HerdrError) as caught:
                    herdr.agent_prompt("target", task)
                self.assertEqual(caught.exception.code, "exit_2")
                self.assertNotIn(task, str(caught.exception))
                self.assertLess(len(str(caught.exception)), 200)

    def test_structured_error_messages_redact_raw_or_json_escaped_task(self):
        task = '私密任务\n"quoted"'
        for echoed in (task, json.dumps(task)[1:-1], json.dumps(task, ensure_ascii=False)[1:-1]):
            with self.subTest(echoed=echoed):
                self.response("", json.dumps({"error": {
                    "code": "agent_prompt_stalled", "message": "echo " + echoed + "x" * 1000,
                }}), returncode=1)
                with self.assertRaises(herdr.HerdrError) as caught:
                    herdr.agent_prompt("target", task)
                self.assertNotIn(echoed, caught.exception.message)
                self.assertLessEqual(len(caught.exception.message), 400)

    def test_prompt_target_text_flags_order_and_single_argv(self):
        for task in ("line one\nline two", "$(command); `command` & | > 'quotes'", "--leading-dash"):
            with self.subTest(task=task):
                herdr.agent_prompt("target", task, wait=True, until=("working", "blocked"),
                                   timeout_ms=15000)
                self.assertEqual(self.run.call_args.args[0], [
                    "/mock/herdr", "agent", "prompt", "target", task,
                    "--wait", "--until", "working", "--until", "blocked", "--timeout", "15000",
                ])
                self.assertEqual(self.run.call_args.kwargs["timeout"], 20.0)
                self.assertFalse(self.run.call_args.kwargs.get("shell", False))

    def test_native_prompt_and_wait_accept_non_json_only_on_success(self):
        for output in ("", "native activity report", "null"):
            with self.subTest(output=output):
                self.response(output)
                self.assertEqual(herdr.agent_prompt("target", "task", wait=True,
                                                   until=("working", "blocked"), timeout_ms=15000), {})
                self.assertEqual(herdr.agent_wait("target", until=("working", "blocked"),
                                                 timeout_ms=15000), {})
                with self.assertRaises(herdr.HerdrError):
                    herdr.call("agent", "get", "target")
        self.response("native failure", returncode=1)
        with self.assertRaises(herdr.HerdrError):
            herdr.agent_prompt("target", "task", wait=True)
        with self.assertRaises(herdr.HerdrError):
            herdr.agent_wait("target")

    def test_start_keeps_native_separator_and_allows_cli_timeout_grace(self):
        herdr.agent_start("target", "codex", "pane", ["-m", "model", "--other"], 60000)
        self.assertEqual(self.run.call_args.args[0], [
            "/mock/herdr", "agent", "start", "target", "--kind", "codex", "--pane", "pane",
            "--timeout", "60000", "--", "-m", "model", "--other",
        ])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 65.0)

    def test_wait_activity_gate_has_explicit_supported_states(self):
        herdr.agent_wait("target", until=("working", "blocked"), timeout_ms=15000)
        self.assertEqual(self.run.call_args.args[0], [
            "/mock/herdr", "agent", "wait", "target", "--until", "working",
            "--until", "blocked", "--timeout", "15000",
        ])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 20.0)

    def test_completion_wait_uses_native_defaults_and_optional_deadline(self):
        herdr.agent_wait("target")
        self.assertEqual(self.run.call_args.args[0], ["/mock/herdr", "agent", "wait", "target"])
        self.assertIsNone(self.run.call_args.kwargs["timeout"])
        herdr.agent_wait("target", timeout_ms=30000)
        self.assertEqual(self.run.call_args.args[0], [
            "/mock/herdr", "agent", "wait", "target", "--timeout", "30000",
        ])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 35.0)

    def test_pane_get_keeps_existing_parser_order_without_separator(self):
        self.response('{"result":{"pane":{"pane_id":"p"}}}')
        self.assertEqual(herdr.pane_get("p"), {"pane_id": "p"})
        self.assertEqual(self.run.call_args.args[0], ["/mock/herdr", "pane", "get", "p"])

    def test_reply_shape_errors_are_normalized_at_cli_boundaries(self):
        cases = [
            ({}, lambda: herdr.current_pane_id()),
            ({"result": {"pane": []}}, lambda: herdr.split("p", "right", "/tmp")),
            ({"result": {"pane": {"pane_id": 1}}}, lambda: herdr.pane_get("p")),
            ({"result": []}, lambda: herdr.unique_agent_name("r-test")),
            ({"result": {"agents": [1]}}, lambda: herdr.unique_agent_name("r-test")),
            ({"result": {"agents": [{"name": []}]}}, lambda: herdr.unique_agent_name("r-test")),
            ({"result": {"layout": None}}, lambda: herdr.auto_direction("p")),
            ({"result": {"layout": {"panes": [1]}}}, lambda: herdr.auto_direction("p")),
            ({"result": {"layout": {"panes": [{"pane_id": "p", "rect": {}}]}}},
             lambda: herdr.auto_direction("p")),
        ]
        for reply, invoke in cases:
            with self.subTest(reply=reply):
                self.response(json.dumps(reply))
                with self.assertRaises(herdr.HerdrError) as caught:
                    invoke()
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_unique_agent_name_keeps_existing_interface(self):
        self.response('{"result":{"agents":[{"name":"r-test-1"},{"name":"r-test-2"}]}}')
        self.assertEqual(herdr.unique_agent_name("r-test"), "r-test-3")

    def test_call_does_not_convert_programming_errors_or_interruptions(self):
        for exception in (ValueError("bug"), KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(exception=type(exception).__name__):
                self.run.side_effect = exception
                with self.assertRaises(type(exception)) as caught:
                    herdr.call("agent", "list")
                self.assertIs(caught.exception, exception)


class RpcEncodingTest(unittest.TestCase):
    def test_invalid_utf8_output_remains_a_parseable_reply_instead_of_a_traceback(self):
        script = "import sys; sys.stdout.buffer.write(bytes.fromhex('7b22726573756c74223a7b2276616c7565223a22ff227d7d'))"
        with mock.patch.object(herdr, "herdr_bin", return_value=sys.executable):
            self.assertEqual(herdr.call("-c", script), {"result": {"value": "\ufffd"}})


if __name__ == "__main__":
    unittest.main()
