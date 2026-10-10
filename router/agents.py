"""Translate (kind, model, effort) into each agent CLI's native arguments.

Flags were checked against the installed CLIs' --help and a real launch in
Herdr. opencode's full TUI has no model flag, so a model starts the `mini`
interface, which takes `-m provider/model`. droid has no model flag either; it
gets a small settings file (`--settings`, merged for that process only) that
prepare() writes before launch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def build_args(kind: str, model: str | None = None, effort: str | None = None) -> list[str]:
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {', '.join(EFFORTS)}")
    args: list[str] = []
    if kind == "claude":
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
    elif kind == "codex":
        if model:
            args += ["-m", model]
        if effort:
            # codex 0.162 accepts low..max for gpt-6.x; older models stop at xhigh
            # (per-model levels: ~/.codex/models_cache.json, surfaced by catalog.py).
            args += ["-c", f"model_reasoning_effort={effort}"]
    elif kind == "kimi":
        if model:
            args += ["-m", model]
        if effort:
            args.append("--no-thinking" if effort == "low" else "--thinking")
    elif kind == "cursor":
        # cursor-agent takes effort as a bracket parameter on the model name.
        if model:
            args += ["--model", f"{model}[effort={effort}]" if effort else model]
    elif kind == "opencode":
        # effort has no flag here; the provider's default variant applies
        if model:
            args += ["mini", "-m", model]
    elif kind == "droid":
        if model or effort:
            args += ["--settings", str(droid_settings_path(model, effort))]
    # any other kind: no flags; agent defaults apply.
    return args


def droid_settings_path(model: str | None, effort: str | None) -> Path:
    from .model import state_dir  # model imports this module
    slug = re.sub(r"[^a-z0-9._-]+", "-", f"{model or 'default'}-{effort or 'default'}".lower())
    return state_dir() / "droid" / f"{slug}.json"


def prepare(kind: str, model: str | None, effort: str | None) -> None:
    """Write any files the launch arguments point at. Called right before dispatch."""
    if kind == "droid" and (model or effort):
        settings = {k: v for k, v in (("model", model), ("reasoningEffort", effort)) if v}
        path = droid_settings_path(model, effort)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessionDefaultSettings": settings}) + "\n", encoding="utf-8")


def describe(kind: str, model: str | None, effort: str | None) -> str:
    parts = [kind]
    if model:
        parts.append(model)
    if effort:
        parts.append(f"effort={effort}")
    return " / ".join(parts)
