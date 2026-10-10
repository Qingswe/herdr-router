"""Model choices per agent kind, for the editor's model picker.

Only model names and supported efforts are read. Codex's models_cache.json
also carries account identity; that field is never touched or returned.
Lists that need a slow CLI call (cursor-agent, opencode) are cached for a day.
Models in the policy's own `models` catalog come first, under their labels.
Kinds without a known list return [] and the editor falls back to free text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .agents import EFFORTS
from .model import state_dir

CLAUDE = [
    ("fable", "Fable（最新别名）"), ("opus", "Opus（最新别名）"),
    ("sonnet", "Sonnet（最新别名）"), ("haiku", "Haiku（最新别名）"),
    ("claude-fable-5-1", "Claude Fable 5.1"), ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5"),
]
LIST_TTL_S = 24 * 3600
_CURSOR_LINE = re.compile(r"^\s*([\w.\-\[\]=,]+)\s+-\s+(.+?)\s*(\(current.*\))?$")


def codex_models(home: Path | None = None) -> list[dict]:
    home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for m in data.get("models", []) if isinstance(data, dict) else []:
        if not isinstance(m, dict) or m.get("visibility") != "list" or not m.get("slug"):
            continue
        efforts = [lv.get("effort") for lv in m.get("supported_reasoning_levels") or [] if isinstance(lv, dict)]
        out.append({"id": str(m["slug"]), "label": str(m.get("display_name") or m["slug"]),
                    "efforts": [e for e in EFFORTS if e in efforts] or list(EFFORTS)})
    return out


def parse_cursor_list(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _CURSOR_LINE.match(line)
        if m and m.group(1).lower() != "available":
            out.append({"id": m.group(1), "label": m.group(2).strip(), "efforts": []})
    return out


def _cached_list(name: str, cmd: list[str], parse, timeout: float) -> list[dict]:
    cache = state_dir() / f"models-{name}.json"
    try:
        if time.time() - cache.stat().st_mtime < LIST_TTL_S:
            return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        text = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    models = parse(text)
    if models:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(models, ensure_ascii=False), encoding="utf-8")
    return models


def parse_opencode_list(text: str) -> list[dict]:
    return [{"id": ln.strip(), "label": ln.strip(), "efforts": []} for ln in text.splitlines()
            if re.match(r"^[\w.-]+/[\w.:-]+$", ln.strip())]


def cursor_models(binary: str = "cursor-agent") -> list[dict]:
    return _cached_list("cursor", [binary, "--list-models"], parse_cursor_list, 10)


def opencode_models(binary: str = "opencode") -> list[dict]:
    return _cached_list("opencode", [binary, "models"], parse_opencode_list, 40)


def models(policy: dict | None = None) -> dict[str, list[dict]]:
    binaries = (policy or {}).get("binaries", {})
    found = {
        "claude": [{"id": i, "label": label, "efforts": list(EFFORTS)} for i, label in CLAUDE],
        "codex": codex_models(),
        "cursor": cursor_models(binaries.get("cursor", "cursor-agent")),
        "opencode": opencode_models(binaries.get("opencode", "opencode")),
    }
    # the policy's catalog first, labelled; discovered entries keep their efforts
    out: dict[str, list[dict]] = {}
    for entry in ((policy or {}).get("models") or {}).values():
        for kind, mid in (entry.get("via") or {}).items():
            known = next((m for m in found.get(kind, []) if m["id"] == mid), None)
            out.setdefault(kind, []).append({"id": mid, "label": entry.get("label", mid),
                                             "efforts": known["efforts"] if known else []})
    for kind, lst in found.items():
        seen = {m["id"] for m in out.get(kind, [])}
        out.setdefault(kind, []).extend(m for m in lst if m["id"] not in seen)
    return out
