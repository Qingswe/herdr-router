"""Rank agent profiles against an assessment.

A profile occupies a range on every score dimension. Its penalty is the
probability mass outside that range: mass above it (the agent is too weak) is
weighted by under_penalty, mass below it (overkill) by over_penalty. Choice
dimensions charge P(option) * (1 - affinity). A small cost term breaks ties
toward cheaper profiles. Lowest total wins.
"""

from __future__ import annotations

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
            lo, hi = (prof.get("ranges") or {}).get(d["id"], [0, len(d["levels"]) - 1])
            p_above = sum(p for lv, p in dist.items() if lv > hi)
            p_below = sum(p for lv, p in dist.items() if lv < lo)
            pen = w * (under * p_above + over * p_below)
        else:
            aff = (prof.get("affinity") or {}).get(d["id"], {})
            default = float(aff.get("*", 1.0))
            pen = w * sum(p * (1 - float(aff.get(opt, default))) for opt, p in dist.items())
        breakdown[d["id"]] = round(pen, 4)
    breakdown["cost"] = round(float(sc.get("cost_weight", 0.05)) * float(prof.get("cost", 0)), 4)
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
