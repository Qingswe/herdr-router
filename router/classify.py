"""Assess a task along the policy's dimensions.

Each dimension becomes one System One question (Jev on OpenCode Zen, or a
local Kev). The result is a probability distribution per dimension. When the
decision model is unreachable, keyword heuristics fill in the well-known
dimensions (difficulty, duration, task_type) with point estimates.
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

from . import __version__
from .model import dimensions, levels_max, profile


@dataclass
class Assessment:
    """dists: score dim -> {level(int): p}; choice dim -> {option: p}."""

    dists: dict
    source: str  # "systemone" | "heuristic", plus "+override"
    confidence: float
    bumped: bool = False
    details: dict = field(default_factory=dict)

    def expected(self, dim_id: str) -> float | None:
        d = self.dists.get(dim_id)
        if not d or not all(isinstance(k, int) for k in d):
            return None
        return round(sum(k * p for k, p in d.items()), 2)

    def top_choice(self, dim_id: str) -> str | None:
        d = self.dists.get(dim_id)
        if not d or all(isinstance(k, int) for k in d):
            return None
        return max(d, key=d.get)

    def to_dict(self, policy: dict | None = None) -> dict:
        out = asdict(self)
        out["dists"] = {k: {str(lv): p for lv, p in v.items()} for k, v in self.dists.items()}
        if policy:
            out["expected"] = {d["id"]: self.expected(d["id"]) for d in dimensions(policy, "score")
                               if d["id"] in self.dists}
            out["choices"] = {d["id"]: self.top_choice(d["id"]) for d in dimensions(policy, "choice")
                              if d["id"] in self.dists}
        return out


def point(value: float) -> dict:
    """A degenerate distribution; fractional values split between neighbours."""
    lo = math.floor(value)
    frac = round(value - lo, 6)
    return {lo: 1.0} if frac == 0 else {lo: round(1 - frac, 6), lo + 1: frac}


# --------------------------------------------------------------------------
# Inline overrides: @alias / @profile-id pins a profile, !alias / !dim=value pins dimensions

@dataclass
class Overrides:
    profile: str | None = None
    dims: dict = field(default_factory=dict)  # dim_id -> value
    tokens: list[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"(?<!\S)([@!][a-z][a-z0-9_-]*(?:=\d+(?:\.\d+)?)?)(?!\S)")


def parse_overrides(task: str, policy: dict) -> tuple[str, Overrides]:
    aliases = policy.get("aliases", {})
    score_dims = {d["id"]: d for d in dimensions(policy, "score")}
    ov = Overrides()

    def take(m: re.Match) -> str:
        tok = m.group(1)
        if tok.startswith("@"):
            target = aliases.get(tok) or (tok[1:] if profile(policy, tok[1:]) else None)
            if not target:
                return m.group(0)
            ov.profile = target
        elif tok in aliases:
            ov.dims.update(aliases[tok])
        elif "=" in tok:
            did, val = tok[1:].split("=", 1)
            if did not in score_dims or not 0 <= float(val) <= levels_max(score_dims[did]):
                return m.group(0)
            ov.dims[did] = float(val)
        else:
            return m.group(0)
        ov.tokens.append(tok)
        return ""

    cleaned = _TOKEN_RE.sub(take, task)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, ov


# --------------------------------------------------------------------------
# Keyword heuristics (fallback). Only know the dimension ids below.

_TYPE_WORDS = {
    "review": ["review", "code review", "审查", "审核", "评审", "检查一下", "看看有没有问题"],
    "bugfix": ["bug", "fix", "crash", "error", "exception", "broken", "regression",
               "修复", "报错", "崩溃", "异常", "失败", "不工作", "闪退"],
    "refactor": ["refactor", "rename", "migrate", "bulk", "replace all", "reformat",
                 "重构", "重命名", "改名", "迁移", "批量", "替换"],
    "test": ["test", "tests", "unit test", "coverage", "测试", "单测", "用例"],
    "docs": ["doc", "docs", "readme", "changelog", "文档", "注释", "说明"],
    "ui": ["ui", "css", "layout", "frontend", "component", "style",
           "界面", "样式", "布局", "前端", "组件", "页面", "按钮"],
    "research": ["explain", "why", "how does", "investigate", "research", "what is",
                 "解释", "为什么", "调研", "分析", "了解", "研究", "怎么回事"],
    "implement": ["implement", "add", "feature", "build", "create", "support",
                  "实现", "添加", "新增", "功能", "做一个", "写一个", "支持"],
}
_EASY_WORDS = ["typo", "rename", "format", "lint", "bump", "simple", "trivial", "one-line",
               "拼写", "错别字", "重命名", "改名", "格式化", "简单", "小改", "一行", "改个"]
_HARD_WORDS = ["architecture", "design", "race", "concurrency", "deadlock", "sync",
               "performance", "optimize", "security", "distributed", "network", "multiplayer",
               "memory leak", "intermittent", "rewrite", "protocol", "algorithm", "root cause",
               "架构", "设计", "竞态", "并发", "死锁", "同步", "性能", "优化", "安全", "分布式",
               "网络", "联机", "多人", "内存泄漏", "偶现", "原因不明", "重写", "协议", "算法", "疑难"]
_SHORT_WORDS = ["quick", "typo", "one-line", "this variable", "single",
                "快速", "一行", "这个变量", "单个", "一处", "看一下", "小改"]
_LONG_WORDS = ["all files", "entire", "whole", "across", "bulk", "migrate", "rewrite",
               "end-to-end", "system", "architecture", "every",
               "所有", "全部", "整个", "批量", "迁移", "重构", "重写", "系统", "完整",
               "端到端", "架构", "多个模块", "全面"]


def _hits(text: str, words: list[str]) -> list[str]:
    found = []
    for w in words:
        if w.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
                found.append(w)
        elif w in text:
            found.append(w)
    return found


def heuristic(task: str, policy: dict) -> Assessment:
    text = task.lower()
    by_id = {d["id"]: d for d in dimensions(policy)}
    easy, hard = _hits(text, _EASY_WORDS), _hits(text, _HARD_WORDS)
    short, long_ = _hits(text, _SHORT_WORDS), _hits(text, _LONG_WORDS)
    dists: dict = {}
    if (d := by_id.get("difficulty")) and d["type"] == "score":
        dists["difficulty"] = point(_clamp(1 + len(hard) - len(easy), levels_max(d)))
    if (d := by_id.get("duration")) and d["type"] == "score":
        extra = 1 if len(task) > 400 else 0
        dists["duration"] = point(_clamp(1 + len(long_) - len(short) + extra, levels_max(d)))
    type_hits: list[str] = []
    if (d := by_id.get("task_type")) and d["type"] == "choice":
        scores = {t: _hits(text, ws) for t, ws in _TYPE_WORDS.items() if t in d["options"]}
        best = max(scores, key=lambda t: len(scores[t]), default=None)
        if best and scores[best]:
            dists["task_type"] = {best: 1.0}
            type_hits = scores[best]
        elif "implement" in d["options"]:
            dists["task_type"] = {"implement": 1.0}
    signal = len(easy) + len(hard) + len(short) + len(long_)
    return Assessment(
        dists=dists, source="heuristic", confidence=round(min(0.9, 0.3 + 0.2 * signal), 2),
        details={"type_hits": type_hits, "easy": easy, "hard": hard, "short": short, "long": long_},
    )


# --------------------------------------------------------------------------
# System One backend (Jev / Kev share the API)

class SystemOneUnavailable(Exception):
    pass


def systemone_request(state: str, policy: dict) -> dict:
    questions = {}
    for d in dimensions(policy):
        q = {"type": d["type"], "instructions": d["instructions"]}
        q["criteria"] = d["levels"] if d["type"] == "score" else d["options"]
        questions[d["id"]] = q
    return {"state": state, "model": policy.get("backend", {}).get("model", "jev-1.13-free"),
            "questions": questions}


def parse_systemone(resp: dict, policy: dict) -> Assessment:
    answers = resp.get("answers")
    if not isinstance(answers, dict):
        raise SystemOneUnavailable("unexpected response shape: no answers")
    dists, confs = {}, []
    for d in dimensions(policy):
        a = answers.get(d["id"])
        probs = (a or {}).get("probabilities")
        if not isinstance(probs, dict):
            continue
        try:
            if d["type"] == "score":
                dist = {int(float(k)): float(v) for k, v in probs.items()}
            else:
                dist = {k: float(v) for k, v in probs.items() if k in d["options"]}
        except (TypeError, ValueError):
            continue
        total = sum(dist.values())
        if total <= 0:
            continue
        dists[d["id"]] = {k: round(v / total, 4) for k, v in dist.items()}
        if _is_num(a.get("confidence")):
            confs.append(float(a["confidence"]))
    if not dists:
        raise SystemOneUnavailable("unexpected response shape: no usable answers")
    return Assessment(dists=dists, source="systemone", confidence=round(min(confs, default=0.0), 2),
                      details={"latency_ms": resp.get("latency_ms"), "usage": resp.get("usage")})


def _ssl_context() -> ssl.SSLContext:
    """python.org builds ship without CA roots; fall back to certifi or the system bundle."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    paths = ssl.get_default_verify_paths()
    if not (paths.cafile and os.path.exists(paths.cafile)) and os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


def call_systemone(state: str, policy: dict) -> Assessment:
    be = policy.get("backend", {})
    body = json.dumps(systemone_request(state, policy)).encode()
    url = be.get("base_url", "https://opencode.ai/zen").rstrip("/") + "/v1/systemone"
    # Cloudflare in front of opencode.ai rejects urllib's default User-Agent (error 1010).
    headers = {"Content-Type": "application/json", "User-Agent": f"herdr-router/{__version__}"}
    key = os.environ.get(be.get("api_key_env") or "", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = _ssl_context() if url.startswith("https:") else None
    try:
        with urllib.request.urlopen(req, timeout=float(be.get("timeout_s", 5.0)), context=ctx) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemOneUnavailable(f"HTTP {e.code}: {e.read()[:200].decode(errors='replace').strip()}") from e
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise SystemOneUnavailable(str(e)) from e
    return parse_systemone(resp, policy)


def repo_signals(cwd: str | None) -> str:
    """Opt-in context. Measured against jev-1.13 it helped bulk tasks' duration
    but pushed easy tasks toward hard (rename: P(hard) 0.10 -> 0.34), so it is
    off by default; the scope dimension covers bulk tasks instead."""
    try:
        r = subprocess.run(["git", "ls-files"], cwd=cwd, capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    return f"Repository: {len(r.stdout.splitlines())} tracked files."


# --------------------------------------------------------------------------

def _clamp(v: float, hi: float) -> float:
    return max(0, min(hi, v))


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def bump(value: float, hi: float) -> float:
    """Round up a fractional score; step an integral one up by one."""
    up = math.ceil(value) if value != int(value) else value + 1
    return _clamp(up, hi)


def assess(task: str, policy: dict, overrides: Overrides | None = None,
           cwd: str | None = None, use_backend: bool = True) -> Assessment:
    overrides = overrides or Overrides()
    be = policy.get("backend", {})
    result: Assessment | None = None
    fallback_reason = None

    if use_backend and be.get("enabled", True):
        state = task
        if be.get("include_repo_signals", False):
            if signals := repo_signals(cwd):
                state = f"{signals}\n\nTask: {task}"
        try:
            result = call_systemone(state, policy)
        except SystemOneUnavailable as e:
            fallback_reason = str(e)
    if result is None:
        result = heuristic(task, policy)
        if fallback_reason:
            result.details["systemone_error"] = fallback_reason
        # Point estimates carry no spread: lean toward the stronger agent when unsure.
        diff = next((d for d in dimensions(policy, "score") if d["id"] == "difficulty"), None)
        if (diff and "difficulty" in result.dists and "difficulty" not in overrides.dims
                and result.confidence < float(policy.get("scoring", {}).get("min_confidence", 0.35))):
            (lv,) = result.dists["difficulty"]
            result.dists["difficulty"] = point(bump(lv, levels_max(diff)))
            result.bumped = True

    if overrides.dims:
        for did, val in overrides.dims.items():
            result.dists[did] = point(float(val))
        result.source += "+override"
    if overrides.tokens:
        result.details["inline_overrides"] = overrides.tokens
    return result
