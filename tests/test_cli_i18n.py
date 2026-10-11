"""Terminal copy stays centralized without changing routing or policy labels."""

import unittest
from unittest import mock

from router import cli, model


class RenderLabelsTest(unittest.TestCase):
    def test_render_uses_constants_and_preserves_dimension_labels(self):
        policy = model.load_default()
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
