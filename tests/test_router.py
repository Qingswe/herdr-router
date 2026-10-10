import copy
import glob
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router import agents, catalog, model
from router.classify import (Assessment, Overrides, assess, bump, heuristic, parse_overrides,
                             parse_systemone, point, systemone_request)
from router.score import NoAgentAvailable, decide, penalty, rank, rank_at, territory

POLICY = model.load_default()
FIXTURES = Path(__file__).parent / "fixtures"
ALL = lambda b: f"/usr/bin/{b}"  # noqa: E731  every binary "installed"


def fixture(slug: str) -> dict:
    return json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))


def mk(**dists) -> Assessment:
    return Assessment(dists=dists, source="test", confidence=0.9)


class RegressionTest(unittest.TestCase):
    """Recorded jev-1.13-free responses under the default built from Artificial Analysis
    data, tuned with the user: easy work goes to cheap open models, Sol takes most real work,
    Opus xhigh expert-level design. Close calls, by penalty: rename deepseek-flash 0.695 vs
    mimo-flash 0.703; bulk_replace glm-flash 1.42 vs sol-high 1.46; architecture opus-xhigh
    3.04 vs sol-xhigh 3.18."""

    EXPECT = {
        "rename": "deepseek-flash", "hud_ping": "glm-flash", "bulk_replace": "glm-flash",
        "review": "codex-review", "explain_relay": "sonnet-research",
        "architecture": "opus-xhigh", "crash": "sol-xhigh", "canvas_all": "sol-high",
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
    def prof(self, **point):
        return {"id": "p", "kind": "claude", "cost": 0, "point": point}

    def test_within_tolerance_is_free(self):
        _, br = penalty(self.prof(difficulty=2.2), mk(difficulty={2: 1.0}), POLICY)
        self.assertEqual(br["difficulty"], 0)

    def test_asymmetric(self):
        # 2 levels above the point, minus 0.5 tolerance, at under_penalty; below at over_penalty.
        sc = POLICY["scoring"]
        _, above = penalty(self.prof(difficulty=1), mk(difficulty={3: 1.0}), POLICY)
        _, below = penalty(self.prof(difficulty=3), mk(difficulty={1: 1.0}), POLICY)
        self.assertAlmostEqual(above["difficulty"], 1.5 * sc["under_penalty"])
        self.assertAlmostEqual(below["difficulty"], 1.5 * sc["over_penalty"])
        self.assertGreater(sc["under_penalty"], sc["over_penalty"])

    def test_expected_over_distribution(self):
        _, br = penalty(self.prof(difficulty=1), mk(difficulty={1: 0.6, 3: 0.4}), POLICY)
        self.assertAlmostEqual(br["difficulty"], 0.4 * 1.5 * POLICY["scoring"]["under_penalty"])

    def test_dimension_weight(self):
        _, br = penalty(self.prof(scope=0), mk(scope={3: 1.0}), POLICY)  # scope weight 0.5
        self.assertAlmostEqual(br["scope"], 0.5 * POLICY["scoring"]["under_penalty"] * 2.5)

    def test_dimension_left_out_of_point_is_free(self):
        _, br = penalty(self.prof(), mk(difficulty={4: 1.0}), POLICY)
        self.assertEqual(br["difficulty"], 0)

    def test_unknown_dimension_has_no_opinion(self):
        _, br = penalty(self.prof(scope=0), mk(difficulty={0: 1.0}), POLICY)
        self.assertNotIn("scope", br)

    def test_choice_affinity_with_default(self):
        prof = {"id": "p", "kind": "codex", "cost": 0,
                "affinity": {"task_type": {"*": 0, "review": 1}}}
        _, br = penalty(prof, mk(task_type={"review": 0.75, "bugfix": 0.25}), POLICY)
        self.assertAlmostEqual(br["task_type"], 6.0 * 0.25)  # task_type weight 6

    def test_cost_and_time_terms(self):
        sc = POLICY["scoring"]
        _, br = penalty({"id": "p", "kind": "claude", "cost": 4, "time": 10}, mk(), POLICY)
        self.assertAlmostEqual(br["cost"], 4 * sc["cost_weight"])
        self.assertAlmostEqual(br["time"], 10 * sc["time_weight"])
        _, br = penalty({"id": "p", "kind": "opencode", "cost": 4}, mk(), POLICY)  # flat subscription
        self.assertAlmostEqual(br["cost"], 4 * sc["cost_weight"] * sc["agent_cost"]["opencode"])


class RankTest(unittest.TestCase):
    def test_unavailable_skipped_but_listed(self):
        which = lambda b: None if b == "claude" else ALL(b)  # noqa: E731
        which = lambda b: None if b == "codex" else ALL(b)  # noqa: E731
        d = decide("t", mk(difficulty={0: 1.0}, duration={0: 1.0}), POLICY, which=which)
        self.assertEqual(d.profile_id, "haiku-medium")
        self.assertIn("luna-low", d.reason)
        self.assertFalse(next(r for r in d.ranking if r["profile_id"] == "luna-low")["available"])

    def test_cost_breaks_ties(self):
        r = rank(mk(difficulty={0: 1.0}, duration={0: 1.0}), POLICY, ALL)
        self.assertEqual([x.profile_id for x in r[:3]], ["luna-low", "haiku-medium", "deepseek-flash"])

    def test_disabled_excluded(self):
        pol = copy.deepcopy(POLICY)
        model.profile(pol, "luna-low")["enabled"] = False
        d = decide("t", mk(difficulty={0: 1.0}, duration={0: 1.0}), pol, which=ALL)
        self.assertEqual(d.profile_id, "haiku-medium")

    def test_nothing_available(self):
        with self.assertRaises(NoAgentAvailable):
            decide("t", mk(), POLICY, which=lambda b: None)

    def test_kind_filter_and_patches(self):
        d = decide("t", mk(difficulty={4: 1.0}, duration={3: 1.0}), POLICY,
                   kind="codex", effort="max", which=ALL)
        self.assertEqual((d.kind, d.effort, d.profile_id), ("codex", "max", "sol-xhigh"))
        self.assertEqual(d.args, ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=max"])

    def test_pinned_profile_even_if_disabled(self):
        pol = copy.deepcopy(POLICY)
        model.profile(pol, "cursor-ui")["enabled"] = False
        d = decide("t", mk(difficulty={4: 1.0}), pol, pinned_profile="cursor-ui", which=ALL)
        self.assertEqual((d.profile_id, d.reason), ("cursor-ui", "explicitly pinned"))

    def test_prompt_prefix(self):
        d = decide("do it", mk(difficulty={4: 1.0}), POLICY, pinned_profile="opus-xhigh", which=ALL)
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
            p.setdefault("point", {})["security"] = 0.5
        self.pol["profiles"].append({
            "id": "opus-security", "label": "Opus 安全", "kind": "claude", "model": "opus",
            "effort": "max", "cost": 6, "point": {"difficulty": 2.5, "security": 2.5}})

    def test_valid(self):
        self.assertEqual(model.validate(self.pol), [])

    def test_question_generated(self):
        q = systemone_request("x", self.pol)["questions"]["security"]
        self.assertEqual((q["type"], len(q["criteria"])), ("score", 4))

    def test_routes_on_new_dimension(self):
        a = mk(difficulty={2: 1.0}, duration={1: 1.0}, security={3: 0.8, 2: 0.2})
        self.assertEqual(decide("t", a, self.pol, which=ALL).profile_id, "opus-security")
        a = mk(difficulty={2: 1.0}, duration={1: 1.0}, security={0: 1.0})
        self.assertNotEqual(decide("t", a, self.pol, which=ALL).profile_id, "opus-security")


class ValidateTest(unittest.TestCase):
    def errors(self, mutate) -> list[str]:
        pol = copy.deepcopy(POLICY)
        mutate(pol)
        return [e["path"] for e in model.validate(pol)]

    def test_default_is_valid(self):
        self.assertEqual(model.validate(POLICY), [])

    def test_rejections(self):
        cases = {
            "profiles[0].point.difficulty": lambda p: p["profiles"][0]["point"].update(difficulty=9),
            "profiles[1].point.nope": lambda p: p["profiles"][1]["point"].update(nope=1),
            "profiles[2].ranges": lambda p: p["profiles"][2].update(ranges={"difficulty": [0, 1]}),
            "models.glm-5.3.via.nope": lambda p: p["models"]["glm-5.3"]["via"].update(nope="x"),
            "models.glm-5.3.via.droid": lambda p: p["models"]["glm-5.3"]["via"].update(droid="glm; rm"),
            "profiles[0].kind": lambda p: p["profiles"][0].update(kind="nope"),
            "profiles[0].time": lambda p: p["profiles"][0].update(time=-1),
            "scoring.agent_cost.nope": lambda p: p["scoring"]["agent_cost"].update(nope=1),
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


class MigrateTest(unittest.TestCase):
    def v2(self):
        pol = copy.deepcopy(POLICY)
        pol["version"] = 2
        for p in pol["profiles"]:
            p.pop("point", None)
        pol["profiles"][0]["ranges"] = {"difficulty": [0, 1], "duration": [0, 3], "scope": [1, 2]}
        return pol

    def test_ranges_become_midpoints(self):
        pol = model.migrate(self.v2())
        self.assertEqual(pol["version"], 3)
        self.assertNotIn("ranges", pol["profiles"][0])
        self.assertEqual(pol["profiles"][0]["point"], {"difficulty": 0.5, "scope": 1.5})  # full range dropped
        self.assertEqual(model.validate(pol), [])

    def test_load_migrates_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.json"
            path.write_text(json.dumps(self.v2()))
            self.assertEqual(model.load(path)["profiles"][0]["point"]["difficulty"], 0.5)


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
        self.assertEqual((text, ov.profile), ("fix it", "sol-medium"))
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
        self.assertEqual(agents.build_args("codex", None, "max"), ["-c", "model_reasoning_effort=max"])
        self.assertEqual(agents.build_args("kimi", None, "low"), ["--no-thinking"])
        self.assertEqual(agents.build_args("cursor", "gpt-5", "high"), ["--model", "gpt-5[effort=high]"])
        self.assertEqual(agents.build_args("opencode", "opencode-go/glm-5.3", "max"), ["mini", "-m", "opencode-go/glm-5.3"])
        self.assertEqual(agents.build_args("opencode", None, None), [])
        self.assertEqual(agents.build_args("droid", None, None), [])

    def test_droid_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}):
            args = agents.build_args("droid", "glm-5.3", "high")
            self.assertEqual(args[0], "--settings")
            agents.prepare("droid", "glm-5.3", "high")
            written = json.loads(Path(args[1]).read_text())
        self.assertEqual(written, {"sessionDefaultSettings": {"model": "glm-5.3", "reasoningEffort": "high"}})

    def test_bad_effort(self):
        with self.assertRaises(ValueError):
            agents.build_args("claude", None, "ultra")


class TerritoryTest(unittest.TestCase):
    def winner(self, x, y, choice=None, which=ALL):
        return rank_at(POLICY, {"duration": x, "difficulty": y}, choice, which)[0].profile_id

    def test_corners(self):
        self.assertEqual(self.winner(0, 0), "luna-low")
        self.assertEqual(self.winner(0.9, 2.6), "sol-medium")
        self.assertEqual(self.winner(1.3, 2.0), "glm-flash")
        self.assertEqual(self.winner(3, 3), "sol-xhigh")
        self.assertEqual(self.winner(3, 4), "opus-xhigh")

    def test_grid_shape(self):
        m = territory(POLICY, "duration", "difficulty", which=ALL, steps=12)
        self.assertEqual((len(m["grid"]), len(m["grid"][0])), (12, 12))
        self.assertEqual((m["x"], m["y"]), ([-0.5, 3.5], [-0.5, 4.5]))
        self.assertEqual(m["ids"][m["grid"][0][0]], "luna-low")  # bottom-left sample
        self.assertEqual(m["ids"][m["grid"][-1][-1]], "opus-xhigh")  # top-right: expert, long

    def test_choice_pins_specialist(self):
        self.assertEqual(self.winner(1, 2, {"task_type": "review"}), "codex-review")
        self.assertNotEqual(self.winner(1, 2, {"task_type": "nope"}), "codex-review")

    def test_unavailable_skipped(self):
        self.assertEqual(self.winner(0, 0, which=lambda b: None if b == "codex" else ALL(b)), "haiku-medium")


class CatalogTest(unittest.TestCase):
    def test_codex_cache_reads_names_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "models_cache.json").write_text(json.dumps({
                "identity": {"email": "someone@example.com"},
                "models": [
                    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list",
                     "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "max", "ultra")]},
                    {"slug": "gpt-reserve", "visibility": "hide", "supported_reasoning_levels": []},
                ]}))
            out = catalog.codex_models(Path(tmp))
        self.assertEqual(out, [{"id": "gpt-6.1-sol", "label": "GPT-6.1-Sol", "efforts": ["low", "high", "max"]}])
        self.assertNotIn("example.com", json.dumps(out))

    def test_codex_cache_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(catalog.codex_models(Path(tmp)), [])

    def test_cursor_list(self):
        text = "Available models\n\nauto - Auto (current, default)\ngpt-5.3-codex-high - Codex 5.3 High\n"
        self.assertEqual([m["id"] for m in catalog.parse_cursor_list(text)], ["auto", "gpt-5.3-codex-high"])
        self.assertEqual(catalog.parse_cursor_list(text)[0]["label"], "Auto")


if __name__ == "__main__":
    unittest.main()
