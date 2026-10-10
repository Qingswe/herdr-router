"""Translate (kind, model, effort) into each agent CLI's native arguments.

Flags were checked against the installed CLIs' --help. Agents whose
interactive mode has no model/effort flag (opencode, droid) start with their
own configured defaults.
"""

from __future__ import annotations

EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# codex: model_reasoning_effort accepts minimal|low|medium|high|xhigh.
_CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


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
            args += ["-c", f"model_reasoning_effort={_CODEX_EFFORT[effort]}"]
    elif kind == "kimi":
        if model:
            args += ["-m", model]
        if effort:
            args.append("--no-thinking" if effort == "low" else "--thinking")
    elif kind == "cursor":
        # cursor-agent takes effort as a bracket parameter on the model name.
        if model:
            args += ["--model", f"{model}[effort={effort}]" if effort else model]
    # opencode, droid, and any other kind: no flags; agent defaults apply.
    return args


def describe(kind: str, model: str | None, effort: str | None) -> str:
    parts = [kind]
    if model:
        parts.append(model)
    if effort:
        parts.append(f"effort={effort}")
    return " / ".join(parts)
