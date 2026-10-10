"""The routing policy: dimensions, agent profiles, scoring knobs.

The policy is plain JSON so the web editor can round-trip it. The user's copy
(config dir `policy.json`) replaces the shipped default wholesale.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import PLUGIN_ID
from .agents import EFFORTS

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO_ROOT / "config" / "policy.default.json"
BACKUPS_KEPT = 10

# herdr 0.9.3 `herdr agent` kinds; herdr.supported_kinds() reads the live list.
KNOWN_KINDS = ["pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline", "omp",
               "mastracode", "opencode", "copilot", "kimi", "kiro", "droid", "amp", "grok",
               "hermes", "kilo", "qodercli", "qwen", "letta", "maki", "muse"]

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def config_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".config" / "herdr" / "plugins" / "config" / PLUGIN_ID


def state_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state" / "herdr" / "plugins" / PLUGIN_ID


def user_policy_path() -> Path:
    return config_dir() / "policy.json"


def load(path: Path | None = None) -> dict:
    """Explicit path > user policy.json > shipped default."""
    for p in (path, user_policy_path(), DEFAULT_POLICY):
        if p and p.is_file():
            with p.open(encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(DEFAULT_POLICY)


def load_default() -> dict:
    with DEFAULT_POLICY.open(encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Accessors

def dimensions(policy: dict, type_: str | None = None) -> list[dict]:
    return [d for d in policy.get("dimensions", []) if type_ is None or d.get("type") == type_]


def levels_max(dim: dict) -> int:
    return len(dim.get("levels", [])) - 1


def profile(policy: dict, profile_id: str) -> dict | None:
    return next((p for p in policy.get("profiles", []) if p.get("id") == profile_id), None)


# --------------------------------------------------------------------------
# Validation: a list of {"path": "profiles[2].ranges.difficulty", "message": ...}

def validate(policy: dict, kinds: list[str] | None = None) -> list[dict]:
    errs: list[dict] = []
    kinds = kinds or KNOWN_KINDS

    def err(path: str, msg: str) -> None:
        errs.append({"path": path, "message": msg})

    if not isinstance(policy, dict):
        return [{"path": "", "message": "policy must be a JSON object"}]

    dims = policy.get("dimensions")
    if not isinstance(dims, list) or not dims:
        err("dimensions", "at least one dimension is required")
        dims = []
    dim_by_id: dict[str, dict] = {}
    for i, d in enumerate(dims):
        p = f"dimensions[{i}]"
        did = d.get("id") if isinstance(d, dict) else None
        if not isinstance(did, str) or not _ID_RE.match(did):
            err(f"{p}.id", "id must match [a-z][a-z0-9_-]{0,31}")
            continue
        if did in dim_by_id:
            err(f"{p}.id", f"duplicate dimension id '{did}'")
        dim_by_id[did] = d
        if d.get("type") == "score":
            lv = d.get("levels")
            if not isinstance(lv, list) or len(lv) < 2 or not all(isinstance(x, str) and x.strip() for x in lv):
                err(f"{p}.levels", "a score dimension needs at least 2 non-empty levels")
        elif d.get("type") == "choice":
            opts = d.get("options")
            if not isinstance(opts, dict) or len(opts) < 2:
                err(f"{p}.options", "a choice dimension needs at least 2 options")
            elif not all(isinstance(k, str) and _ID_RE.match(k) for k in opts):
                err(f"{p}.options", "option keys must match [a-z][a-z0-9_-]{0,31}")
        else:
            err(f"{p}.type", "type must be 'score' or 'choice'")
        if not _is_num(d.get("weight", 1.0)) or d.get("weight", 1.0) < 0:
            err(f"{p}.weight", "weight must be a number >= 0")
        if not isinstance(d.get("instructions", ""), str) or not d.get("instructions", "").strip():
            err(f"{p}.instructions", "instructions (the question asked) must not be empty")

    profiles = policy.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        err("profiles", "at least one profile is required")
        profiles = []
    seen: set[str] = set()
    for i, pr in enumerate(profiles):
        p = f"profiles[{i}]"
        if not isinstance(pr, dict):
            err(p, "profile must be an object")
            continue
        pid = pr.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            err(f"{p}.id", "id must match [a-z][a-z0-9_-]{0,31}")
        elif pid in seen:
            err(f"{p}.id", f"duplicate profile id '{pid}'")
        else:
            seen.add(pid)
        if pr.get("kind") not in kinds:
            err(f"{p}.kind", f"unknown herdr agent kind '{pr.get('kind')}'")
        if pr.get("effort") not in (None, *EFFORTS):
            err(f"{p}.effort", f"effort must be one of {', '.join(EFFORTS)}")
        if pr.get("model") is not None and (not isinstance(pr["model"], str) or not re.match(r"^[\w.:\[\]=,/-]+$", pr["model"])):
            err(f"{p}.model", "model must be a plain identifier (letters, digits, . _ - : / [ ] = ,)")
        if not _is_num(pr.get("cost", 0)) or pr.get("cost", 0) < 0:
            err(f"{p}.cost", "cost must be a number >= 0")
        for did, rng in (pr.get("ranges") or {}).items():
            rp = f"{p}.ranges.{did}"
            d = dim_by_id.get(did)
            if not d or d.get("type") != "score":
                err(rp, f"'{did}' is not a score dimension")
                continue
            if (not isinstance(rng, list) or len(rng) != 2 or not all(_is_num(x) for x in rng)):
                err(rp, "range must be [min, max]")
                continue
            hi = levels_max(d)
            if not (0 <= rng[0] <= rng[1] <= hi):
                err(rp, f"need 0 <= min <= max <= {hi}")
        for did, aff in (pr.get("affinity") or {}).items():
            ap = f"{p}.affinity.{did}"
            d = dim_by_id.get(did)
            if not d or d.get("type") != "choice":
                err(ap, f"'{did}' is not a choice dimension")
                continue
            if not isinstance(aff, dict):
                err(ap, "affinity must be an object of option -> 0..1")
                continue
            for opt, v in aff.items():
                if opt != "*" and opt not in d.get("options", {}):
                    err(f"{ap}.{opt}", f"unknown option '{opt}'")
                if not _is_num(v) or not 0 <= v <= 1:
                    err(f"{ap}.{opt}", "affinity must be between 0 and 1")
    if profiles and not any(isinstance(pr, dict) and pr.get("enabled", True) for pr in profiles):
        err("profiles", "at least one profile must be enabled")

    for tok, target in (policy.get("aliases") or {}).items():
        ap = f"aliases.{tok}"
        if tok.startswith("@"):
            if target not in seen:
                err(ap, f"alias points to unknown profile '{target}'")
        elif tok.startswith("!"):
            if not isinstance(target, dict):
                err(ap, "a ! alias maps dimension ids to values")
                continue
            for did, v in target.items():
                d = dim_by_id.get(did)
                if not d or d.get("type") != "score":
                    err(f"{ap}.{did}", f"'{did}' is not a score dimension")
                elif not _is_num(v) or not 0 <= v <= levels_max(d):
                    err(f"{ap}.{did}", f"value must be within 0..{levels_max(d)}")
        else:
            err(ap, "alias must start with @ (profile) or ! (dimension values)")

    sc = policy.get("scoring", {})
    for key in ("under_penalty", "over_penalty", "cost_weight", "min_confidence"):
        if key in sc and (not _is_num(sc[key]) or sc[key] < 0):
            err(f"scoring.{key}", "must be a number >= 0")
    be = policy.get("backend", {})
    if be.get("enabled") and not str(be.get("base_url", "")).startswith(("http://", "https://")):
        err("backend.base_url", "base_url must be an http(s) URL")
    return errs


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------
# Saving

class InvalidPolicy(ValueError):
    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} validation error(s)")
        self.errors = errors


def save(policy: dict, kinds: list[str] | None = None, path: Path | None = None) -> Path:
    """Validate, back up the current file, then replace it atomically."""
    errors = validate(policy, kinds)
    if errors:
        raise InvalidPolicy(errors)
    path = path or user_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backups = path.parent / "policy.backups"
        backups.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (backups / f"policy-{stamp}-{time.time_ns() % 1000000:06d}.json").write_bytes(path.read_bytes())
        for old in sorted(backups.glob("policy-*.json"))[:-BACKUPS_KEPT]:
            old.unlink(missing_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".policy-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(policy, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
