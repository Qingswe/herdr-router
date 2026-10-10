"""Default policy from Artificial Analysis data (fetched 2026-10-11).

Each model with effort control becomes a line of profiles, one per effort, like AA's
per-model curves. Coordinates:
  difficulty = (II - 30) / 7      (II = AA Intelligence Index; 58 -> 4.0)
  duration   = by effort: low 0.3, medium 0.9, high 1.5, xhigh 2.2, max 2.8
  cost       = AA USD per task
  time       = AA time per task (min, the "Intelligence Index vs. Time per Task" chart) for the
               model's tested variant (max), times one shared factor per effort taken from
               GPT-6.1 Sol's agent wall times on AA's coding-agent index, the only family measured
               at every effort without outliers: low .35, medium .45, high .55, xhigh .64, max 1.
               (Per-family end-to-end ratios were tried and rejected: they shrink low efforts
               ~20x, which made e.g. Sol medium look 30x faster than GLM-5.3 Flash.)
Single-variant models (run through OpenCode, which has no effort flag) keep hand-placed
durations.

Usage: python3 tools/build_default_policy.py   (rewrites config/policy.default.json; keeps its
dimensions, backend, binaries, dispatch and env sections). Then run the tests.
"""
import json
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "config" / "policy.default.json"
d = json.loads(P.read_text(encoding="utf-8"))
import os
# Tuned with the user: easy work to cheap open models, Sol for most real work (~45% of the
# plane), Opus for expert-level, see CLAUDE.md. Area shares and the regression fixtures move
# with every one of these.
CW = float(os.environ.get("CW", 0.6))   # penalty per USD per task
TW = float(os.environ.get("TW", 0.01))  # penalty per minute per task; a tie-breaker, because
                                        # single-variant models only have their max-effort time
OP = float(os.environ.get("OP", 0.5))   # overkill penalty per level
UP = float(os.environ.get("UP", 4.0))   # underpowered penalty per level
# what a dollar of AA API price costs the user on each agent: OpenCode Go is a flat subscription,
# Codex / Claude quota is scarce
AGENT_COST = {"opencode": 0.25}
# a type mismatch must cost more than being far away, or specialists (codex-review, no point)
# win the empty corners of the plane once under_penalty is high
TYPE_WEIGHT = 6.0
next(x for x in d["dimensions"] if x["id"] == "task_type")["weight"] = TYPE_WEIGHT
EFF_TIME = {"low": .35, "medium": .45, "high": .55, "xhigh": .64, "max": 1.0}
# Every effort is drawn (the lines stay complete); only these route by default. The rest are
# disabled, so a few cheap families do not carpet the plane; enable any of them in the editor.
ROUTED = {"luna": {"low", "high"}, "haiku": {"medium", "high"}, "sonnet": {"medium"},
          "sol": {"medium", "high", "xhigh"}, "opus": {"medium", "high", "xhigh"}}
d["scoring"] = {"under_penalty": UP, "over_penalty": OP, "tolerance": 0.5, "cost_weight": CW, "agent_cost": AGENT_COST, "time_weight": TW, "min_confidence": 0.35}
d['models'] = {
  "claude-haiku":   {"label": "Claude Haiku 5.5",  "vendor": "anthropic", "via": {"claude": "haiku", "opencode": "opencode-go/claude-haiku-5-5", "droid": "claude-haiku-5-5"}},
  "claude-sonnet":  {"label": "Claude Sonnet 5.5", "vendor": "anthropic", "via": {"claude": "sonnet", "opencode": "opencode/claude-sonnet-5-5", "droid": "claude-sonnet-5-5"}},
  "claude-opus":    {"label": "Claude Opus 5.5",   "vendor": "anthropic", "via": {"claude": "opus", "opencode": "opencode/claude-opus-5-5", "droid": "claude-opus-5-5"}},
  "gpt-6.1-sol":    {"label": "GPT-6.1 Sol",       "vendor": "openai",    "via": {"codex": "gpt-6.1-sol", "opencode": "opencode/gpt-6.1-sol", "droid": "gpt-6.1-sol"}},
  "gpt-6-luna":     {"label": "GPT-6 Luna",        "vendor": "openai",    "via": {"codex": "gpt-6-luna", "opencode": "opencode-go/gpt-6-luna", "droid": "gpt-6-luna"}},
  "glm-5.3":        {"label": "GLM-5.3",           "vendor": "zai",       "via": {"opencode": "opencode-go/glm-5.3", "droid": "glm-5.3"}},
  "glm-5.3-flash":  {"label": "GLM-5.3 Flash",     "vendor": "zai",       "via": {"opencode": "opencode-go/glm-5.3-flash", "droid": "glm-5.3-flash"}},
  "deepseek-v4.1-flash": {"label": "DeepSeek V4.1 Flash", "vendor": "deepseek", "via": {"opencode": "opencode-go/deepseek-v4.1-flash", "droid": "deepseek-v4.1-flash"}},
  "mimo-v2.6-pro":  {"label": "MiMo V2.6 Pro",     "vendor": "xiaomi",    "via": {"opencode": "opencode-go/mimo-v2.6-pro"}},
  "mimo-v2.6-flash": {"label": "MiMo V2.6 Flash",  "vendor": "xiaomi",    "via": {"opencode": "opencode-go/mimo-v2.6-flash"}},
  "kimi-k3":        {"label": "Kimi K3",           "vendor": "moonshot",  "via": {"kimi": "kimi-k3", "opencode": "opencode-go/kimi-k3", "droid": "kimi-k3"}},
}
EFF = ["low", "medium", "high", "xhigh", "max"]
DUR = {"low": 0.3, "medium": 0.9, "high": 1.5, "xhigh": 2.2, "max": 2.8}
plan = "这是一个高难度、耗时较长的任务。请先给出简短的执行计划（关键步骤与风险），再按计划执行。\n\n"
diff = lambda ii: round(max(0.0, min(4.0, (ii - 30) / 7)), 2)
# family: (id prefix, label prefix, kind, model, {effort: (II, $/task)}, tested-variant minutes,
#          {effort: AA end-to-end response seconds, kept for reference only}).
LINES = [
  ("luna", "Luna", "codex", "gpt-6-luna", {"low": (22, .0045), "medium": (30, .02), "high": (33, .03), "xhigh": (35, .04), "max": (38, .07)},
   6.1, {"low": 6.84, "medium": 13.8, "high": 20.8, "xhigh": 27.89, "max": 102.43}),
  ("haiku", "Haiku", "claude", "haiku", {"low": (29, .02), "medium": (34, .05), "high": (38, .08), "xhigh": (41, .12), "max": (43, .21)},
   7.1, {"low": 12.53, "medium": 16.41, "high": 25.52, "xhigh": 59.78, "max": 291.54}),
  ("sonnet", "Sonnet", "claude", "sonnet", {"low": (36, .35), "medium": (41, .48), "high": (47, .88), "xhigh": (52, 2.01), "max": (56, 5.46)},
   14.4, {"low": 5.86, "medium": 6.91, "high": 16.84, "xhigh": 34.55, "max": 490.65}),
  ("sol", "Sol", "codex", "gpt-6.1-sol", {"low": (42, .13), "medium": (48, .21), "high": (50, .32), "xhigh": (51, .39), "max": (52, .72)},
   10.7, {"low": 13.41, "medium": 15.92, "high": 70.03, "xhigh": 160.92, "max": 335.94}),
  ("opus", "Opus", "claude", "opus", {"low": (42, .55), "medium": (51, 1.34), "high": (54, 1.82), "xhigh": (56, 3.46), "max": (58, 5.98)},
   12.8, {"low": 15.71, "medium": 28.28, "high": 42.91, "xhigh": 134.0, "max": 667.31}),
]
profiles = []
for pre, lab, kind, model, data, tmax, ratio in LINES:
    for e in EFF:
        ii, cost = data[e]
        o = {"id": f"{pre}-{e}", "label": f"{lab} {e}", "kind": kind, "model": model, "effort": e,
             "cost": cost, "time": round(tmax * EFF_TIME[e], 2), "enabled": e in ROUTED[pre]}
        if e in ("xhigh", "max"):
            o["prompt_prefix"] = plan
        o["point"] = {"difficulty": diff(ii), "duration": DUR[e]}
        if pre in ("luna", "haiku"):
            o["point"]["scope"] = 0.5
        o["affinity"] = {"task_type": {"review": 0.5}}
        o["note"] = f"AA: II {ii} · ${cost}/task · ~{o['time']} min/task"
        profiles.append(o)
def single(id, label, model, ii, cost, time, dur, note, **kw):
    o = {"id": id, "label": label, "kind": "opencode", "model": model, "cost": cost, "time": time, "enabled": kw.pop("enabled", True),
         "point": {"difficulty": diff(ii), "duration": dur}, "affinity": {"task_type": {"review": 0.5}}, "note": note}
    if "scope" in kw: o["point"]["scope"] = kw.pop("scope")
    return o
profiles += [
  single("deepseek-flash", "DeepSeek V4.1 Flash", "opencode-go/deepseek-v4.1-flash", 39, .27, 4.8, 0.15, "AA: II 39 (max) · $0.27/task · 4.8 min/task，最快", scope=0.5),
  single("mimo-flash", "MiMo V2.6 Flash", "opencode-go/mimo-v2.6-flash", 38, .06, 16.8, 0.8, "AA: II 38 · $0.06/task · ~16.8 min/task（按 Pro 估）"),
  single("glm-flash", "GLM-5.3 Flash", "opencode-go/glm-5.3-flash", 42, .25, 14.3, 0.9, "AA: II 42 · $0.25/task · 14.3 min/task"),
  single("mimo-pro", "MiMo V2.6 Pro", "opencode-go/mimo-v2.6-pro", 46, .13, 22.6, 1.0, "AA: II 46 · $0.13/task · 22.6 min/task"),
  single("glm", "GLM-5.3", "opencode-go/glm-5.3", 45, 2.01, 11.4, 1.0, "AA: II 45 (max) · $2.01/task · 11.4 min/task；编程 agent 指数 53.6"),
  {"id": "kimi", "label": "Kimi K3（备用）", "kind": "kimi", "effort": "high", "cost": 2.0, "time": 18.8, "enabled": False,
   "point": {"difficulty": diff(44), "duration": 1.5}, "affinity": {"task_type": {"review": 0.5}},
   "note": "AA: II 44 (max) · $2.00/task · 18.8 min/task。被 MiMo Pro 和 Sol 全面压制，默认停用"},
  {"id": "codex-review", "label": "Codex 交叉审查", "kind": "codex", "model": "gpt-6.1-sol", "effort": "high", "cost": .32, "time": 5.9, "enabled": True,
   "affinity": {"task_type": {"*": 0, "review": 1}}, "note": "AA: II 50 · $0.32/task"},
  {"id": "sonnet-research", "label": "Sonnet 调研", "kind": "claude", "model": "sonnet", "effort": "medium", "cost": .48, "time": 1.4, "enabled": True,
   "point": {"duration": 0.6}, "affinity": {"task_type": {"*": 0, "research": 1}}, "note": "AA: II 41 · $0.48/task · 7s"},
  {"id": "cursor-ui", "label": "Cursor 界面", "kind": "cursor", "cost": .5, "enabled": True,
   "point": {"difficulty": 1.5, "duration": 1.0}, "affinity": {"task_type": {"*": 0, "ui": 1}}, "note": "Cursor auto 模型，AA 无数据"},
]
d['profiles'] = profiles
d['aliases'] = {"@opus": "opus-medium", "@deep": "opus-xhigh", "@sonnet": "sonnet-research", "@haiku": "haiku-high",
  "@codex": "sol-medium", "@sol": "sol-medium", "@luna": "luna-high", "@review": "codex-review", "@kimi": "kimi",
  "@cursor": "cursor-ui", "@glm": "glm", "@deepseek": "deepseek-flash", "@mimo": "mimo-pro",
  "!quick": {"difficulty": 0, "duration": 0}, "!easy": {"difficulty": 0}, "!hard": {"difficulty": 3},
  "!deep": {"difficulty": 4}, "!long": {"duration": 3}, "!short": {"duration": 0}}
P.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
