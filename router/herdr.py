"""Thin CLI wrappers; native prompt/wait gates also accept plain-text success."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence


RPC_TIMEOUT_S = 15.0
RPC_TIMEOUT_GRACE_S = 5.0


class HerdrError(Exception):
    def __init__(self, code: str, message: str, *, pane_id: str | None = None,
                 agent: str | None = None, before_submission: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.pane_id = pane_id
        self.agent = agent
        self.dispatch_id: str | None = None
        self.stage: str | None = None
        self.prompt_status = "not_sent"
        self.before_submission = before_submission


def herdr_bin() -> str:
    path = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not path:
        raise HerdrError("herdr_not_found", "herdr binary not found on PATH", before_submission=True)
    return path


def inside_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1" or bool(os.environ.get("HERDR_SOCKET_PATH"))


def _reply_error(reply: object, args: tuple[str, ...], fallback: str) -> HerdrError | None:
    if not isinstance(reply, dict) or not isinstance(reply.get("error"), dict):
        return None
    err = reply["error"]
    code = err.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", code):
        code = fallback
    message = err.get("message", "")
    if not isinstance(message, str):
        message = "Herdr CLI reported an error"
    # Some CLI errors echo their arguments. Keep task text out of diagnostics.
    if args[:2] == ("agent", "prompt") and len(args) > 3 and args[3]:
        task = args[3]
        for text in (task, json.dumps(task, ensure_ascii=False)[1:-1],
                     json.dumps(task, ensure_ascii=True)[1:-1]):
            message = message.replace(text, "<task omitted>")
    message = message[:400]
    return HerdrError(code, message,
                      pane_id=err.get("pane_id") if isinstance(err.get("pane_id"), str) else None,
                      agent=err.get("agent") if isinstance(err.get("agent"), str) else None)


def call(*args: str, timeout: float | None = RPC_TIMEOUT_S,
         allow_non_json: bool = False) -> dict:
    """Run one RPC; only native prompt/wait gates opt into plain-text success."""
    operation = " ".join(args[:2])
    try:
        r = subprocess.run([herdr_bin(), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HerdrError("timeout", f"herdr {operation} exceeded its local RPC deadline") from None
    except OSError as e:
        raise HerdrError("rpc_error", f"could not run herdr {operation} (errno {e.errno})",
                         before_submission=isinstance(e, (FileNotFoundError, PermissionError))) from None
    if r.returncode != 0:
        code = f"exit_{r.returncode}"
        error = None
        for output in (r.stderr, r.stdout):
            for candidate in (output.strip(), *output.strip().splitlines()[-1:]):
                try:
                    reply = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                error = _reply_error(reply, args, code)
                if error:
                    break
            if error:
                break
        raise error or HerdrError(
            code, f"herdr {operation} exited with status {r.returncode}")
    try:
        reply = json.loads(r.stdout)
    except json.JSONDecodeError:
        if allow_non_json:
            return {}
        raise HerdrError("invalid_response", f"herdr {operation} returned invalid JSON") from None
    error = _reply_error(reply, args, "error")
    if error:
        raise error
    if not isinstance(reply, dict):
        if allow_non_json:
            return {}
        raise HerdrError("invalid_response", f"herdr {operation} returned a non-object JSON reply")
    return reply


def _result(reply: dict, operation: str) -> dict:
    result = reply.get("result")
    if not isinstance(result, dict):
        raise HerdrError("invalid_response", f"herdr {operation} reply has no result object")
    return result


def _pane(reply: dict, operation: str) -> dict:
    pane = _result(reply, operation).get("pane")
    if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str) or not pane["pane_id"]:
        raise HerdrError("invalid_response", f"herdr {operation} reply has no pane ID")
    return pane


def pane_get(pane_id: str) -> dict:
    return _pane(call("pane", "get", pane_id), "pane get")


def current_pane_id() -> str:
    return _pane(call("pane", "current", "--current"), "pane current")["pane_id"]


def auto_direction(pane_id: str) -> str:
    """Wide panes split right, tall ones down (terminal cells are ~2:1)."""
    layout = _result(call("pane", "layout", "--pane", pane_id), "pane layout").get("layout")
    if not isinstance(layout, dict) or not isinstance(layout.get("panes", []), list):
        raise HerdrError("invalid_response", "herdr pane layout reply has no valid layout")
    for p in layout.get("panes", []):
        if not isinstance(p, dict) or not isinstance(p.get("pane_id"), str):
            raise HerdrError("invalid_response", "herdr pane layout reply has an invalid pane")
        if p["pane_id"] == pane_id:
            rect = p.get("rect")
            if not isinstance(rect, dict) or any(
                    not isinstance(rect.get(key), (int, float)) for key in ("width", "height")):
                raise HerdrError("invalid_response", "herdr pane layout reply has an invalid rectangle")
            return "right" if rect["width"] >= 2.2 * rect["height"] else "down"
    return "right"


def pane_screen(pane_id: str, lines: int = 12) -> str:
    """Last non-empty lines on a pane, for showing why an agent never became ready."""
    try:
        r = subprocess.run([herdr_bin(), "pane", "read", pane_id], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
    except (HerdrError, OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join([ln.rstrip() for ln in r.stdout.splitlines() if ln.strip()][-lines:])


def split(target: str, direction: str, cwd: str) -> str:
    res = call("pane", "split", "--pane", target, "--direction", direction,
               "--cwd", cwd, "--no-focus")
    return _pane(res, "pane split")["pane_id"]


_NAME_RE = re.compile(r"[^a-z0-9_-]+")


def unique_agent_name(base: str) -> str:
    base = _NAME_RE.sub("-", base.lower()).strip("-") or "agent"
    if not base[0].isalpha():
        base = "r-" + base
    base = base[:28]
    agents = _result(call("agent", "list"), "agent list").get("agents", [])
    if not isinstance(agents, list) or any(
            not isinstance(a, dict) or (a.get("name") is not None and not isinstance(a["name"], str))
            for a in agents):
        raise HerdrError("invalid_response", "herdr agent list reply has an invalid agent list")
    taken = {a.get("name") for a in agents if a.get("name")}
    n = 1
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def agent_start(name: str, kind: str, pane_id: str, args: list[str], timeout_ms: int) -> dict:
    cmd = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(timeout_ms)]
    if args:
        cmd += ["--", *args]
    return call(*cmd, timeout=timeout_ms / 1000 + RPC_TIMEOUT_GRACE_S)


def agent_prompt(name: str, text: str, wait: bool = False, timeout_ms: int | None = None,
                 until: Sequence[str] | None = None) -> dict:
    cmd = ["agent", "prompt", name, text]
    if wait:
        cmd.append("--wait")
    for state in until or ():
        cmd += ["--until", state]
    if timeout_ms is not None:
        cmd += ["--timeout", str(timeout_ms)]
    timeout = RPC_TIMEOUT_S if timeout_ms is None else timeout_ms / 1000 + RPC_TIMEOUT_GRACE_S
    return call(*cmd, timeout=timeout, allow_non_json=True)


def agent_wait(name: str, until: Sequence[str] | None = None,
               timeout_ms: int | None = None) -> dict:
    cmd = ["agent", "wait", name]
    for state in until or ():
        cmd += ["--until", state]
    if timeout_ms is not None:
        cmd += ["--timeout", str(timeout_ms)]
    timeout = None if timeout_ms is None else timeout_ms / 1000 + RPC_TIMEOUT_GRACE_S
    return call(*cmd, timeout=timeout, allow_non_json=True)


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
        r = subprocess.run([herdr_bin(), "agent"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
    except (HerdrError, OSError, subprocess.TimeoutExpired):
        return []
    m = re.search(r"kinds:\s*([a-z0-9_|-]+)", r.stdout + r.stderr)
    return m.group(1).split("|") if m else []
