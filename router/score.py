"""Rank agent profiles against an assessment.

A profile sits at a point on the score dimensions it cares about. Its penalty
is the expected distance from the task to that point, beyond a small
tolerance, and asymmetric: a task above the point (the agent is too weak)
costs under_penalty per level, a task below it (overkill) over_penalty.
Dimensions a profile leaves out of its point cost nothing. Choice dimensions
charge P(option) * (1 - affinity). Two plain terms follow: cost_weight x cost
(USD per task) x agent_cost[kind] and time_weight x time (minutes per task), so
cheaper and faster profiles win close calls. agent_cost says what a dollar on
each agent really costs the user (a flat subscription is cheaper than metered quota). Lowest total wins, so the policy draws a
weighted Voronoi map over task space.
"""

from __future__ import annotations

import functools
import shutil
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import agents
from .classify import Assessment
from .model import dimensions, profile


class NoAgentAvailable(Exception):
    pass


@dataclass
class Ranked:
    profile_id: str
    label: str
    kind: str
    total: float
    breakdown: dict  # dim_id | "cost" -> penalty
    available: bool
    why_unavailable: str | None = None


@dataclass
class Decision:
    profile_id: str
    profile_label: str
    kind: str
    model: str | None
    effort: str | None
    args: list[str]
    prompt: str
    reason: str
    assessment: dict
    ranking: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def summary(self) -> str:
        return agents.describe(self.kind, self.model, self.effort)


def penalty(prof: dict, a: Assessment, policy: dict) -> tuple[float, dict]:
    sc = policy.get("scoring", {})
    under = float(sc.get("under_penalty", 3.0))
    over = float(sc.get("over_penalty", 1.0))
    tol = float(sc.get("tolerance", 0.5))
    breakdown: dict[str, float] = {}
    for d in dimensions(policy):
        dist = a.dists.get(d["id"])
        if not dist:
            if d["type"] == "score":
                continue  # nothing known about this dimension: no opinion
            # Unknown choice: assume a uniform prior, so a narrow specialist does not win by default.
            dist = {opt: 1 / len(d["options"]) for opt in d["options"]}
        w = float(d.get("weight", 1.0))
        if d["type"] == "score":
            at = (prof.get("point") or {}).get(d["id"])
            pen = 0.0 if at is None else w * sum(
                p * (under * max(0.0, lv - at - tol) + over * max(0.0, at - lv - tol)) for lv, p in dist.items())
        else:
            aff = (prof.get("affinity") or {}).get(d["id"], {})
            default = float(aff.get("*", 1.0))
            pen = w * sum(p * (1 - float(aff.get(opt, default))) for opt, p in dist.items())
        breakdown[d["id"]] = round(pen, 4)
    factor = float((sc.get("agent_cost") or {}).get(prof.get("kind"), 1.0))
    breakdown["cost"] = round(float(sc.get("cost_weight", 0.05)) * float(prof.get("cost", 0)) * factor, 4)
    breakdown["time"] = round(float(sc.get("time_weight", 0.0)) * float(prof.get("time", 0)), 4)
    return round(sum(breakdown.values()), 4), breakdown


def rank(a: Assessment, policy: dict, which: Callable[[str], str | None] = shutil.which,
         include_disabled: bool = False) -> list[Ranked]:
    binaries = policy.get("binaries", {})
    out = []
    for i, prof in enumerate(policy.get("profiles", [])):
        if not prof.get("enabled", True) and not include_disabled:
            continue
        total, breakdown = penalty(prof, a, policy)
        binary = binaries.get(prof["kind"], prof["kind"])
        missing = which(binary) is None
        why = f"'{binary}' not on PATH" if missing else ("disabled" if not prof.get("enabled", True) else None)
        out.append((total, float(prof.get("cost", 0)), i,
                    Ranked(prof["id"], prof.get("label", prof["id"]), prof["kind"], total, breakdown,
                           available=why is None, why_unavailable=why)))
    out.sort(key=lambda t: t[:3])
    return [r for *_, r in out]


def _pinned_choices(policy: dict, choice: dict | None) -> dict:
    dims = {d["id"]: d for d in dimensions(policy, "choice")}
    return {did: {opt: 1.0} for did, opt in (choice or {}).items() if did in dims and opt in dims[did]["options"]}


def rank_at(policy: dict, at: dict[str, float], choice: dict | None = None,
            which: Callable[[str], str | None] = shutil.which) -> list[Ranked]:
    """Available profiles ranked for a task sitting exactly at `at` ({score_dim: value})."""
    a = Assessment({d: {v: 1.0} for d, v in at.items()} | _pinned_choices(policy, choice), "map", 1.0)
    return [r for r in rank(a, policy, which) if r.available]


def territory(policy: dict, x_id: str, y_id: str, choice: dict | None = None,
              which: Callable[[str], str | None] = shutil.which, steps: int = 48) -> dict:
    """Who wins a task at each point of the (x, y) plane, sampled on a steps x steps grid.

    The plane spans [-0.5, levels - 0.5] on both axes. choice pins choice
    dimensions ({dim_id: option}); unpinned ones keep the uniform prior penalty()
    applies. Other score dimensions are unknown, so they do not count. This is
    what the editor's territory map draws.
    """
    dims = {d["id"]: d for d in dimensions(policy, "score")}
    which = functools.cache(which)
    hi_x, hi_y = len(dims[x_id]["levels"]) - 0.5, len(dims[y_id]["levels"]) - 0.5
    xs = [round(-0.5 + (i + 0.5) * (hi_x + 0.5) / steps, 4) for i in range(steps)]
    ys = [round(-0.5 + (j + 0.5) * (hi_y + 0.5) / steps, 4) for j in range(steps)]
    ids = [p["id"] for p in policy.get("profiles", [])]
    grid = []
    for y in ys:
        row = []
        for x in xs:
            ranking = rank_at(policy, {x_id: x, y_id: y}, choice, which)
            row.append(ids.index(ranking[0].profile_id) if ranking else -1)
        grid.append(row)
    return {"x": [-0.5, hi_x], "y": [-0.5, hi_y], "ids": ids, "grid": grid}


def decide(task: str, a: Assessment, policy: dict, *, pinned_profile: str | None = None,
           kind: str | None = None, model: str | None = None, effort: str | None = None,
           which: Callable[[str], str | None] = shutil.which) -> Decision:
    """Pick a profile.

    pinned_profile (from @alias or --profile) wins outright. kind restricts the
    ranking to that agent kind. model / effort patch whichever profile wins.
    """
    ranking = rank(a, policy, which, include_disabled=bool(pinned_profile))
    if pinned_profile:
        chosen = next((r for r in ranking if r.profile_id == pinned_profile), None)
        if chosen is None:
            raise NoAgentAvailable(f"unknown profile '{pinned_profile}'")
        if chosen.why_unavailable and chosen.why_unavailable != "disabled":
            raise NoAgentAvailable(f"{pinned_profile}: {chosen.why_unavailable}")
        reason = "explicitly pinned"
    else:
        pool = [r for r in ranking if r.available and (not kind or r.kind == kind)]
        if not pool:
            scope = f"kind '{kind}'" if kind else "any profile"
            raise NoAgentAvailable(f"no available profile for {scope}: " + "; ".join(
                f"{r.profile_id}: {r.why_unavailable}" for r in ranking if r.why_unavailable))
        chosen = pool[0]
        skipped = [r.profile_id for r in ranking[:ranking.index(chosen)] if not r.available]
        reason = f"lowest penalty {chosen.total}" + (f" (kind={kind})" if kind else "")
        if skipped:
            reason += f", skipped unavailable {', '.join(skipped)}"

    prof = profile(policy, chosen.profile_id)
    final_model = model or prof.get("model")
    final_effort = effort or prof.get("effort")
    return Decision(
        profile_id=prof["id"], profile_label=prof.get("label", prof["id"]),
        kind=prof["kind"], model=final_model, effort=final_effort,
        args=agents.build_args(prof["kind"], final_model, final_effort),
        prompt=prof.get("prompt_prefix", "") + task,
        reason=reason,
        assessment=a.to_dict(policy),
        ranking=[asdict(r) for r in ranking],
    )
