"""Terminal copy stays centralized without changing routing or policy labels."""

import contextlib
import copy
import io
import os
import tempfile
import unittest
from functools import partial
from types import SimpleNamespace
from unittest import mock

from router import cli, herdr, model
from router.score import decide


class RenderLabelsTest(unittest.TestCase):
    def test_render_uses_constants_and_preserves_dimension_labels(self):
        policy = model.load_default()
        with mock.patch.object(cli, "decide", partial(decide, which=lambda binary: f"/test/bin/{binary}")):
            decision = cli.route_task("rename a variable", policy, use_backend=False)
        decision.assessment["bumped"] = True
        decision.assessment.setdefault("details", {})["systemone_error"] = "offline"
        before = cli.render(decision, policy)
        expected = {
            "source": "判断来源", "selection": "选择", "reason": "原因",
            "decision_model": "决策模型", "launch_args": "启动参数",
            "low_confidence": "低置信度→难度上调",
            "keyword_fallback": "不可用，已用关键词规则",
        }
        self.assertEqual(cli.RENDER_LABELS, expected)
        for value in expected.values():
            self.assertIn(value, before)
        replacement = {key: f"label-{key}" for key in expected}
        with mock.patch.dict(cli.RENDER_LABELS, replacement):
            after = cli.render(decision, policy)
        for key, value in expected.items():
            self.assertIn(replacement[key], after)
        for dimension in model.dimensions(policy):
            self.assertIn(dimension.get("label", dimension["id"]), after)
        self.assertEqual(cli.render(decision, policy), before)


class TerminalLanguageTest(unittest.TestCase):
    """terminal_language only changes terminal copy; policy data and routing stay as they are."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_ROUTER_LANGUAGE", None)
        self.policy = model.load_default()
        with mock.patch.object(cli, "decide", partial(decide, which=lambda binary: f"/test/bin/{binary}")):
            self.decision = cli.route_task("rename a variable", self.policy, use_backend=False)
        self.outcome = SimpleNamespace(agent="worker", pane_id="w1:p2", dispatch_id="attempt-1",
                                       stage="complete", prompt_status="confirmed")

    def test_default_is_chinese_and_env_overrides_policy(self):
        self.assertEqual(cli.language(self.policy), "zh")
        en = dict(self.policy, terminal_language="en")
        self.assertEqual(cli.language(en), "en")
        for value, expected in (("zh", "zh"), ("en_US.UTF-8", "en"), ("fr", "en")):
            with self.subTest(env=value), mock.patch.dict(os.environ, {"HERDR_ROUTER_LANGUAGE": value}):
                self.assertEqual(cli.language(en), expected)

    def test_english_render_translates_builtin_labels_but_keeps_custom_ones(self):
        policy = copy.deepcopy(self.policy)
        policy["dimensions"][2]["label"] = "影响面"  # user-edited scope label
        before = copy.deepcopy(policy)
        out = cli.render(self.decision, policy, lang="en")
        for label in ("Source", "Difficulty", "Duration", "Type", "Selected", "Reason", "Launch"):
            self.assertIn(label, out)
        self.assertIn("影响面", out)
        self.assertNotIn("难度", out)
        self.assertEqual(policy, before)
        self.assertIn("难度", cli.render(self.decision, policy))

    def run_popup(self, lang, answers):
        stdout = io.StringIO()
        with mock.patch.object(cli, "_ask", side_effect=answers) as ask, \
                mock.patch.object(cli, "route_task", return_value=self.decision) as route, \
                mock.patch.object(cli, "dispatch", return_value=self.outcome) as send, \
                mock.patch.object(cli.signal, "signal"), mock.patch.object(cli.time, "sleep"), \
                contextlib.redirect_stdout(stdout):
            code = cli.run_popup(self.policy, lang)
        return code, stdout.getvalue(), ask, route, send

    def test_english_popup_flow(self):
        code, out, ask, route, send = self.run_popup("en", ["rename a variable", ""])
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_args_list[0].args[0], "Task> ")
        self.assertIn("Task  rename a variable", out)
        self.assertIn("Selected", out)
        self.assertIn("Enter dispatch · k profile · m model · e effort · t edit task · q cancel", out)
        self.assertIn("Starting codex; waiting up to 60 s", out)
        self.assertIn("✓ Task delivered to worker (pane w1:p2)", out)
        self.assertNotRegex(out, "[一-鿿]")
        send.assert_called_once()
        self.assertEqual(route.call_args.args[0], "rename a variable")

    def test_chinese_popup_is_unchanged_by_default(self):
        code, out, ask, _, _ = self.run_popup("zh", ["rename a variable", ""])
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_args_list[0].args[0], "任务> ")
        self.assertIn("Enter 派发 · k 换档位", out)
        self.assertIn("✓ 任务已确认送达 worker（pane w1:p2）", out)

    def test_shortcut_action_passes_policy_language_to_popup(self):
        policy = dict(self.policy, terminal_language="en")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp, "HERDR_PLUGIN_STATE_DIR": tmp}), \
                mock.patch.object(model, "load", return_value=policy), \
                mock.patch.object(cli, "live_cwd", return_value=tmp), \
                mock.patch.object(herdr, "plugin_pane_open") as open_pane:
            self.assertEqual(cli.main(["action", "route"]), 0)
        self.assertEqual(open_pane.call_args.kwargs["env"]["HERDR_ROUTER_LANGUAGE"], "en")

    def test_terminal_language_is_validated(self):
        def paths(value):
            return [e["path"] for e in model.validate(dict(self.policy, terminal_language=value))]
        self.assertNotIn("terminal_language", paths("en"))
        for bad in ("fr", ["en"], {"en": 1}, 1, None):
            with self.subTest(value=bad):
                self.assertIn("terminal_language", paths(bad))
                # An unsaved or hand-edited bad value falls back to Chinese instead of crashing.
                self.assertEqual(cli.language(dict(self.policy, terminal_language=bad)), "zh")
