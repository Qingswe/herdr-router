import copy
import glob
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router import agents, model
from router.classify import (Assessment, Overrides, assess, bump, heuristic, parse_overrides,
                             parse_systemone, point, systemone_request)
from router.score import NoAgentAvailable, decide, penalty, rank

POLICY = model.load_default()
FIXTURES = Path(__file__).parent / "fixtures"
ALL = lambda b: f"/usr/bin/{b}"  # noqa: E731  every binary "installed"


def fixture(slug: str) -> dict:
    return json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))


def mk(**dists) -> Assessment:
    return Assessment(dists=dists, source="test", confidence=0.9)


class RegressionTest(unittest.TestCase):
    """Recorded jev-1.13-free responses must route the way v1 (2x2 quadrants) did."""

    EXPECT = {
        "rename": "haiku-low", "hud_ping": "haiku-low", "bulk_replace": "codex-medium",
        "review": "codex-review", "explain_relay": "sonnet-research",
        "architecture": "opus-xhigh", "crash": "opus-xhigh", "canvas_all": "opus-xhigh",
    }

    def test_all_fixtures_covered(self):
        slugs = {Path(f).stem for f in glob.glob(str(FIXTURES / "*.json"))}
        self.assertEqual(slugs, set(self.EXPECT))

    def test_routes(self):
        for slug, want in self.EXPECT.items():
            fx = fixture(slug)
            a = parse_systemone(fx["response"], POLICY)
            d = decide(fx["task"], a, POLICY, which=ALL)
            self.assertEqual(d.profile_id, want, slug)


class PenaltyTest(unittest.TestCase):
    def prof(self, **ranges):
        return {"id": "p", "kind": "claude", "cost": 0, "ranges": ranges}

    def test_inside_range_is_free(self):
        _, br = penalty(self.prof(difficulty=[1, 3]), mk(difficulty={1: 0.5, 3: 0.5}), POLICY)
        self.assertEqual(br["difficulty"], 0)

    def test_asymmetric(self):
        # 40% above the range costs under_penalty (3); 40% below costs over_penalty (1).
        _, above = penalty(self.prof(difficulty=[0, 1]), mk(difficulty={1: 0.6, 2: 0.4}), POLICY)
        _, below = penalty(self.prof(difficulty=[2, 4]), mk(difficulty={1: 0.4, 2: 0.6}), POLICY)
        self.assertAlmostEqual(above["difficulty"], 1.2)
        self.assertAlmostEqual(below["difficulty"], 0.4)

    def test_dimension_weight(self):
        _, br = penalty(self.prof(scope=[0, 1]), mk(scope={3: 1.0}), POLICY)  # scope weight 0.5
        self.assertAlmostEqual(br["scope"], 1.5)

    def test_unknown_dimension_has_no_opinion(self):
        _, br = penalty(self.prof(scope=[0, 0]), mk(difficulty={0: 1.0}), POLICY)
        self.assertNotIn("scope", br)

    def test_choice_affinity_with_default(self):
        prof = {"id": "p", "kind": "codex", "cost": 0,
                "affinity": {"task_type": {"*": 0, "review": 1}}}
        _, br = penalty(prof, mk(task_type={"review": 0.75, "bugfix": 0.25}), POLICY)
        self.assertAlmostEqual(br["task_type"], 2.0 * 0.25)  # task_type weight 2

    def test_cost_term(self):
        _, br = penalty({"id": "p", "kind": "claude", "cost": 4}, mk(), POLICY)
        self.assertAlmostEqual(br["cost"], 0.2)


class RankTest(unittest.TestCase):
    def test_unavailable_skipped_but_listed(self):
        which = lambda b: None if b == "claude" else ALL(b)  # noqa: E731
        d = decide("t", mk(difficulty={0: 1.0}, duration={0: 1.0}), POLICY, which=which)
        self.assertEqual(d.profile_id, "kimi-low")
        self.assertIn("haiku-low", d.reason)
        self.assertFalse(next(r for r in d.ranking if r["profile_id"] == "haiku-low")["available"])

    def test_cost_breaks_ties(self):
        r = rank(mk(difficulty={0: 1.0}, duration={0: 1.0}), POLICY, ALL)
        self.assertEqual([x.profile_id for x in r[:3]], ["haiku-low", "kimi-low", "opencode-default"])

    def test_disabled_excluded(self):
        pol = copy.deepcopy(POLICY)
        model.profile(pol, "haiku-low")["enabled"] = False
        d = decide("t", mk(difficulty={0: 1.0}, duration={0: 1.0}), pol, which=ALL)
        self.assertEqual(d.profile_id, "kimi-low")

    def test_nothing_available(self):
        with self.assertRaises(NoAgentAvailable):
            decide("t", mk(), POLICY, which=lambda b: None)

    def test_kind_filter_and_patches(self):
        d = decide("t", mk(difficulty={4: 1.0}, duration={3: 1.0}), POLICY,
                   kind="codex", effort="max", which=ALL)
        self.assertEqual((d.kind, d.effort), ("codex", "max"))
        self.assertIn("model_reasoning_effort=xhigh", d.args[-1])

    def test_pinned_profile_even_if_disabled(self):
        pol = copy.deepcopy(POLICY)
        model.profile(pol, "cursor-ui")["enabled"] = False
        d = decide("t", mk(difficulty={4: 1.0}), pol, pinned_profile="cursor-ui", which=ALL)
        self.assertEqual((d.profile_id, d.reason), ("cursor-ui", "explicitly pinned"))

    def test_prompt_prefix(self):
        d = decide("do it", mk(difficulty={4: 1.0}, duration={3: 1.0}), POLICY, which=ALL)
        self.assertEqual(d.profile_id, "opus-xhigh")
        self.assertTrue(d.prompt.endswith("do it") and len(d.prompt) > 5)


class ExtensibilityTest(unittest.TestCase):
    """A new dimension plus a profile that covers it: no code changes."""

    def setUp(self):
        self.pol = copy.deepcopy(POLICY)
        self.pol["dimensions"].append({
            "id": "security", "label": "安全", "type": "score", "weight": 1.5,
            "instructions": "How security-sensitive is this task?",
            "levels": ["Unrelated", "Touches user data", "Auth or permissions", "Secrets, crypto, or tokens"]})
        for p in self.pol["profiles"]:
            p.setdefault("ranges", {})["security"] = [0, 1]
        self.pol["profiles"].append({
            "id": "opus-security", "label": "Opus 安全", "kind": "claude", "model": "opus",
            "effort": "max", "cost": 6,
            "ranges": {"difficulty": [1, 4], "duration": [0, 3], "scope": [0, 3], "security": [2, 3]}})

    def test_valid(self):
        self.assertEqual(model.validate(self.pol), [])

    def test_question_generated(self):
        q = systemone_request("x", self.pol)["questions"]["security"]
        self.assertEqual((q["type"], len(q["criteria"])), ("score", 4))

    def test_routes_on_new_dimension(self):
        a = mk(difficulty={2: 1.0}, duration={1: 1.0}, security={3: 0.8, 2: 0.2})
        self.assertEqual(decide("t", a, self.pol, which=ALL).profile_id, "opus-security")
        a = mk(difficulty={2: 1.0}, duration={1: 1.0}, security={0: 1.0})
        self.assertEqual(decide("t", a, self.pol, which=ALL).profile_id, "opus-high")


class ValidateTest(unittest.TestCase):
    def errors(self, mutate) -> list[str]:
        pol = copy.deepcopy(POLICY)
        mutate(pol)
        return [e["path"] for e in model.validate(pol)]

    def test_default_is_valid(self):
        self.assertEqual(model.validate(POLICY), [])

    def test_rejections(self):
        cases = {
            "profiles[0].ranges.difficulty": lambda p: p["profiles"][0]["ranges"].update(difficulty=[3, 1]),
            "profiles[1].ranges.duration": lambda p: p["profiles"][1]["ranges"].update(duration=[0, 9]),
            "profiles[0].kind": lambda p: p["profiles"][0].update(kind="nope"),
            "profiles[0].effort": lambda p: p["profiles"][0].update(effort="ultra"),
            "profiles[0].model": lambda p: p["profiles"][0].update(model="opus; rm -rf ~"),
            "profiles[1].id": lambda p: p["profiles"][1].update(id=p["profiles"][0]["id"]),
            "dimensions[1].id": lambda p: p["dimensions"][1].update(id="difficulty"),
            "dimensions[0].levels": lambda p: p["dimensions"][0].update(levels=["only one"]),
            "aliases.@opus": lambda p: p["aliases"].update({"@opus": "missing"}),
            "aliases.!hard.difficulty": lambda p: p["aliases"].update({"!hard": {"difficulty": 9}}),
            "profiles[8].affinity.task_type.nope": lambda p: p["profiles"][8]["affinity"]["task_type"].update(nope=1),
            "profiles": lambda p: [q.update(enabled=False) for q in p["profiles"]],
        }
        for path, mutate in cases.items():
            self.assertIn(path, self.errors(mutate), path)


class SaveTest(unittest.TestCase):
    def test_atomic_save_with_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            for i in range(model.BACKUPS_KEPT + 3):
                pol = copy.deepcopy(POLICY)
                pol["scoring"]["cost_weight"] = i / 100
                model.save(pol, path=path)
            self.assertEqual(json.loads(path.read_text())["scoring"]["cost_weight"], 0.12)
            self.assertEqual(len(list((Path(tmp) / "policy.backups").glob("*.json"))), model.BACKUPS_KEPT)
            self.assertEqual([p.name for p in Path(tmp).glob(".policy-*")], [])

    def test_invalid_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            bad = copy.deepcopy(POLICY)
            bad["profiles"][0]["kind"] = "nope"
            with self.assertRaises(model.InvalidPolicy):
                model.save(bad, path=path)
            self.assertFalse(path.exists())

    def test_user_policy_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp}):
            self.assertEqual(model.load()["version"], POLICY["version"])
            pol = copy.deepcopy(POLICY)
            pol["scoring"]["cost_weight"] = 0.5
            model.save(pol)
            self.assertEqual(model.load()["scoring"]["cost_weight"], 0.5)


class OverrideTest(unittest.TestCase):
    def test_alias_and_profile_id(self):
        text, ov = parse_overrides("@codex fix it", POLICY)
        self.assertEqual((text, ov.profile), ("fix it", "codex-medium"))
        _, ov = parse_overrides("do @opus-xhigh this", POLICY)
        self.assertEqual(ov.profile, "opus-xhigh")

    def test_dimension_flags(self):
        text, ov = parse_overrides("go !long !difficulty=3", POLICY)
        self.assertEqual((text, ov.dims), ("go", {"duration": 3, "difficulty": 3.0}))

    def test_unknown_and_path_mentions_kept(self):
        text, ov = parse_overrides("look at @src/foo.cs and @someone !nah !difficulty=9", POLICY)
        self.assertEqual(text, "look at @src/foo.cs and @someone !nah !difficulty=9")
        self.assertIsNone(ov.profile)

    def test_override_replaces_distribution(self):
        a = assess("rename x", POLICY, Overrides(dims={"difficulty": 4}), use_backend=False)
        self.assertEqual(a.dists["difficulty"], {4: 1.0})
        self.assertFalse(a.bumped)


class HeuristicTest(unittest.TestCase):
    def test_rename_is_easy_short(self):
        a = heuristic("把这个变量重命名", POLICY)
        self.assertEqual((a.dists["difficulty"], a.dists["duration"], a.dists["task_type"]),
                         ({0: 1.0}, {0: 1.0}, {"refactor": 1.0}))
        self.assertNotIn("scope", a.dists)

    def test_ascii_word_boundaries(self):
        self.assertNotIn("ui", heuristic("rebuild the guide", POLICY).dists["task_type"])

    def test_low_confidence_bumps(self):
        a = assess("do the thing", POLICY, use_backend=False)
        self.assertTrue(a.bumped)
        self.assertEqual(a.dists["difficulty"], {2: 1.0})

    def test_point_and_bump(self):
        self.assertEqual(point(1.25), {1: 0.75, 2: 0.25})
        self.assertEqual((bump(1.3, 4), bump(1, 4), bump(4, 4)), (2, 2, 4))


class SystemOneTest(unittest.TestCase):
    def test_request_from_dimensions(self):
        req = systemone_request("state", POLICY)
        self.assertEqual(set(req["questions"]), {d["id"] for d in POLICY["dimensions"]})
        self.assertEqual(req["questions"]["task_type"]["type"], "choice")
        json.dumps(req)

    def test_parse_normalises_and_skips_missing(self):
        resp = copy.deepcopy(fixture("rename")["response"])
        del resp["answers"]["scope"]
        a = parse_systemone(resp, POLICY)
        self.assertNotIn("scope", a.dists)
        self.assertAlmostEqual(sum(a.dists["difficulty"].values()), 1.0, places=3)

    def test_unreachable_falls_back(self):
        pol = copy.deepcopy(POLICY)
        pol["backend"].update(base_url="http://127.0.0.1:9", timeout_s=0.5)
        a = assess("rename x", pol)
        self.assertEqual(a.source, "heuristic")
        self.assertIn("systemone_error", a.details)


class AgentArgsTest(unittest.TestCase):
    def test_per_kind(self):
        self.assertEqual(agents.build_args("claude", "opus", "high"), ["--model", "opus", "--effort", "high"])
        self.assertEqual(agents.build_args("codex", None, "max"), ["-c", "model_reasoning_effort=xhigh"])
        self.assertEqual(agents.build_args("kimi", None, "low"), ["--no-thinking"])
        self.assertEqual(agents.build_args("cursor", "gpt-5", "high"), ["--model", "gpt-5[effort=high]"])
        self.assertEqual(agents.build_args("droid", "x", "high"), [])

    def test_bad_effort(self):
        with self.assertRaises(ValueError):
            agents.build_args("claude", None, "ultra")


if __name__ == "__main__":
    unittest.main()
