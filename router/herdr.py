"""Thin wrappers over the `herdr` CLI. Every call parses the JSON reply."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def herdr_bin() -> str:
    path = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not path:
        raise HerdrError("herdr_not_found", "herdr binary not found on PATH")
    return path


def inside_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1" or bool(os.environ.get("HERDR_SOCKET_PATH"))


def call(*args: str, timeout: float | None = None) -> dict:
    r = subprocess.run([herdr_bin(), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        try:
            err = json.loads(r.stderr.strip().splitlines()[-1])["error"]
            raise HerdrError(err.get("code", "error"), err.get("message", ""))
        except (IndexError, KeyError, json.JSONDecodeError):
            raise HerdrError(f"exit_{r.returncode}", (r.stderr or r.stdout).strip()) from None
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def pane_get(pane_id: str) -> dict:
    return call("pane", "get", pane_id)["result"]["pane"]


def current_pane_id() -> str:
    return call("pane", "current", "--current")["result"]["pane"]["pane_id"]


def auto_direction(pane_id: str) -> str:
    """Wide panes split right, tall ones down (terminal cells are ~2:1)."""
    layout = call("pane", "layout", "--pane", pane_id)["result"]["layout"]
    for p in layout.get("panes", []):
        if p["pane_id"] == pane_id:
            rect = p["rect"]
            return "right" if rect["width"] >= 2.2 * rect["height"] else "down"
    return "right"


def split(target: str, direction: str, cwd: str) -> str:
    res = call("pane", "split", "--pane", target, "--direction", direction,
               "--cwd", cwd, "--no-focus")
    return res["result"]["pane"]["pane_id"]


_NAME_RE = re.compile(r"[^a-z0-9_-]+")


def unique_agent_name(base: str) -> str:
    base = _NAME_RE.sub("-", base.lower()).strip("-") or "agent"
    if not base[0].isalpha():
        base = "r-" + base
    base = base[:28]
    agents = call("agent", "list")["result"].get("agents", [])
    taken = {a.get("name") for a in agents if a.get("name")}
    n = 1
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def agent_start(name: str, kind: str, pane_id: str, args: list[str], timeout_ms: int) -> dict:
    cmd = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(timeout_ms)]
    if args:
        cmd += ["--", *args]
    return call(*cmd)


def agent_prompt(name: str, text: str, wait: bool = False, timeout_ms: int | None = None) -> dict:
    cmd = ["agent", "prompt", name, text]
    if wait:
        cmd.append("--wait")
    if timeout_ms:
        cmd += ["--timeout", str(timeout_ms)]
    return call(*cmd)


def plugin_pane_open(plugin_id: str, entrypoint: str, placement: str,
                     target_pane: str | None = None, cwd: str | None = None,
                     env: dict[str, str] | None = None) -> dict:
    cmd = ["plugin", "pane", "open", "--plugin", plugin_id, "--entrypoint", entrypoint,
           "--placement", placement, "--focus"]
    if target_pane:
        cmd += ["--target-pane", target_pane]
    if cwd:
        cmd += ["--cwd", cwd]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    return call(*cmd)


def supported_kinds() -> list[str]:
    """Agent kinds this herdr build accepts, read from `herdr agent` usage text."""
    try:
        r = subprocess.run([herdr_bin(), "agent"], capture_output=True, text=True, timeout=5)
    except (HerdrError, OSError, subprocess.TimeoutExpired):
        return []
    m = re.search(r"kinds:\s*([a-z0-9_|-]+)", r.stdout + r.stderr)
    return m.group(1).split("|") if m else []
