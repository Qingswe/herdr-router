"""herdr-router command line.

  herdr-router classify <task>     per-dimension assessment only
  herdr-router route <task>        assessment + profile ranking + chosen agent (explain is an alias)
  herdr-router dispatch <task>     route, then split a pane, start the agent, send the task
  herdr-router ui                  web editor for dimensions and agent profiles
  herdr-router popup               interactive picker (Herdr plugin popup pane)
  herdr-router action <id>         Herdr plugin action entrypoint
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import signal
import stat
import sys
import time
import unicodedata
from pathlib import Path

from . import PLUGIN_ID, __version__, agents, herdr, model, sessions, workspace as workspaces
from .classify import assess, parse_overrides
from .dispatch import dispatch
from .score import Decision, NoAgentAvailable, decide


def extend_path(policy: dict) -> None:
    """Plugin commands can run with the server's thin PATH; add the usual bin dirs."""
    extra = [os.path.expanduser(p) for p in policy.get("env", {}).get("extra_path", [])]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(current + [p for p in extra if p not in current])


def route_task(task: str, policy: dict, *, profile=None, kind=None, model_=None, effort=None,
               cwd=None, use_backend=True) -> Decision:
    cleaned, ov = parse_overrides(task, policy)
    a = assess(cleaned, policy, ov, cwd=cwd, use_backend=use_backend)
    return decide(cleaned, a, policy, pinned_profile=profile or ov.profile,
                  kind=kind, model=model_, effort=effort)


# Keep the existing terminal copy centralized; policy dimension labels are data.
RENDER_LABELS = {
    "source": "判断来源",
    "selection": "选择",
    "reason": "原因",
    "decision_model": "决策模型",
    "launch_args": "启动参数",
    "low_confidence": "低置信度→难度上调",
    "keyword_fallback": "不可用，已用关键词规则",
}
RENDER_LABELS_EN = {
    "source": "Source",
    "selection": "Selected",
    "reason": "Reason",
    "decision_model": "Assessor",
    "launch_args": "Launch",
    "low_confidence": "low confidence, difficulty raised",
    "keyword_fallback": "unavailable, used keyword rules",
}

# English names for the shipped dimensions, used only while a policy keeps the shipped Chinese label.
BUILTIN_DIMENSION_LABELS = {
    "difficulty": ("难度", "Difficulty"),
    "duration": ("时长", "Duration"),
    "scope": ("范围", "Scope"),
    "task_type": ("类型", "Type"),
}

TEXT = {
    "zh": {
        "task_prompt": "任务> ",
        "header": "herdr-router  ·  目标 pane {target}  ·  {cwd}",
        "current_pane": "(当前)",
        "selected_task": "选中文本作为任务：",
        "task": "任务",
        "keys": "Enter 派发 · k 换档位 · m 换模型 · e 换 effort · t 改任务 · q 取消",
        "profile_prompt": "档位编号> ",
        "model_prompt": "模型名（留空=档位默认）> ",
        "effort_prompt": "effort 编号> ",
        "starting": "正在启动 {kind}，最多等 {seconds} 秒，随后确认任务送达…",
        "confirmed": "✓ 任务已确认送达 {agent}（pane {pane}）",
        "session_warning": "Session 记录未能更新：{warning}。任务已送达，请查看原 pane。",
        "failed": "✗ 派发失败：{error}",
        "pane_screen": "pane {pane} 现在的画面：",
        "empty_screen": "(空)",
        "close": "按回车关闭",
        "worktree_check": "正在检查隔离工作区；Git 准备总预算 {ms} ms…",
        "worktree_budget": "Git 准备预算 {ms} ms；路径与分支将在实际派发时分配。",
        "worktree_create": "将创建按需 worktree，再启动 agent…",
        "worktree_created": "创建 {ms} ms",
        "agent_suffix": "（agent {agent}）",
        "recover_confirmed": "任务已确认送达，但等待结束失败。请查看 {location}，不要重新派发。",
        "recover_unknown": "任务可能已送达。请先查看 {location} 确认执行状态，不要直接重新派发。",
        "recover_ready": " worktree 已准备好：{path}（分支 {branch}）。",
        "recover_pane": "任务未发送，{location} 仍保留。请处理该 pane 的启动或确认问题，再决定如何发送任务。{suffix}",
        "recover_worktree": "任务未发送。已记录 worktree 路径 {path}、分支 {branch}、状态 {state}；请先检查创建结果，再处理该工作区。",
        "recover_default": "任务未发送。请修正错误；若创建 pane 时发生通信错误，先检查 Herdr 是否已留下 pane。",
    },
    "en": {
        "task_prompt": "Task> ",
        "header": "herdr-router  ·  target pane {target}  ·  {cwd}",
        "current_pane": "(current)",
        "selected_task": "Selected text as the task:",
        "task": "Task",
        "keys": "Enter dispatch · k profile · m model · e effort · t edit task · q cancel",
        "profile_prompt": "Profile number> ",
        "model_prompt": "Model (empty = profile default)> ",
        "effort_prompt": "Effort number> ",
        "starting": "Starting {kind}; waiting up to {seconds} s, then confirming delivery…",
        "confirmed": "✓ Task delivered to {agent} (pane {pane})",
        "session_warning": "Could not update the session record: {warning}. The task was delivered; check the original pane.",
        "failed": "✗ Dispatch failed: {error}",
        "pane_screen": "Current screen of pane {pane}:",
        "empty_screen": "(empty)",
        "close": "Press Enter to close",
        "worktree_check": "Checking the isolated workspace; total Git budget {ms} ms…",
        "worktree_budget": "Git budget {ms} ms; the path and branch are assigned at dispatch.",
        "worktree_create": "Creating a worktree, then starting the agent…",
        "worktree_created": "created in {ms} ms",
        "agent_suffix": " (agent {agent})",
        "recover_confirmed": "The task was delivered, but waiting for it to finish failed. Check {location}; do not dispatch it again.",
        "recover_unknown": "The task may have been delivered. Check {location} first; do not dispatch it again blindly.",
        "recover_ready": " The worktree is ready: {path} (branch {branch}).",
        "recover_pane": "The task was not sent; {location} is still open. Fix the start-up or confirmation problem in that pane before deciding how to send the task.{suffix}",
        "recover_worktree": "The task was not sent. Recorded worktree path {path}, branch {branch}, state {state}; check what was created before handling that workspace.",
        "recover_default": "The task was not sent. Fix the error; if creating the pane hit a communication error, check whether Herdr left a pane behind.",
    },
}


def language(policy: dict | None = None) -> str:
    """Terminal language: HERDR_ROUTER_LANGUAGE, else the policy's terminal_language, else Chinese."""
    env = os.environ.get("HERDR_ROUTER_LANGUAGE", "").strip().lower()[:2]
    if env in TEXT:
        return env
    pref = (policy or {}).get("terminal_language")
    return pref if isinstance(pref, str) and pref in TEXT else "zh"


def _labels(lang: str) -> dict:
    return RENDER_LABELS_EN if lang == "en" else RENDER_LABELS


def dimension_label(dim: dict, lang: str = "zh") -> str:
    label = dim.get("label", dim["id"])
    builtin = BUILTIN_DIMENSION_LABELS.get(dim["id"])
    if lang == "en" and builtin and label == builtin[0]:
        return builtin[1]
    return label

_BARS = " ▁▂▃▄▅▆▇█"


def _spark(dist: dict, n: int) -> str:
    return "".join(_BARS[min(8, round(float(dist.get(str(i), 0)) * 8))] for i in range(n))


def render(d: Decision, policy: dict, top: int = 3, width: int | None = None, lang: str = "zh") -> str:
    labels = _labels(lang)
    a = d.assessment
    conf = f"{a['source']}, confidence {a['confidence']}"
    if a.get("bumped"):
        conf += ", " + labels["low_confidence"]
    rows = [(labels["source"], conf)]
    for dim in model.dimensions(policy):
        dist = a["dists"].get(dim["id"])
        if not dist:
            rows.append((dimension_label(dim, lang), "—"))
        elif dim["type"] == "score":
            n = len(dim["levels"])
            rows.append((dimension_label(dim, lang),
                         f"{a['expected'][dim['id']]} / {n - 1}   [{_spark(dist, n)}]"))
        else:
            best = max(dist, key=dist.get)
            rows.append((dimension_label(dim, lang), f"{best} ({dist[best]:.2f})"))
    rows.append((labels["selection"], f"{d.profile_label} [{d.profile_id}]  →  {d.summary}"))
    rows.append((labels["reason"], d.reason))
    for i, r in enumerate(d.ranking[:top], 1):
        parts = ", ".join(f"{k}={v}" for k, v in r["breakdown"].items() if v)
        flag = "" if r["available"] else f"  ✗ {r['why_unavailable']}"
        rows.append((f"#{i}", f"{r['profile_id']:<18} {r['total']:<6} ({parts or '0'}){flag}"))
    if err := a.get("details", {}).get("systemone_error"):
        rows.append((labels["decision_model"], f"{labels['keyword_fallback']} ({err[:60]})"))
    native = " ".join(shlex.quote(x) for x in d.args)
    rows.append((labels["launch_args"], f"--kind {d.kind}" + (f" -- {native}" if native else "")))
    col = 11 if lang == "en" else 9  # "Difficulty" is wider than the Chinese labels
    if not width:
        return "\n".join(f"{_pad(k, col)}{v}" for k, v in rows)
    return "\n".join(_wrap(f"{_pad(k, col)}{v}", width, col) for k, v in rows)


def _cells(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """Left-justify by terminal display width (CJK characters take two cells)."""
    return text + " " * max(1, width - _cells(text))


def _wrap(line: str, width: int, indent: int) -> str:
    """Hard-wrap by display width, continuation lines indented under the value column."""
    out, cur, used = [], "", 0
    for ch in line:
        w = _cells(ch)
        if used + w > width:
            out.append(cur)
            cur, used = " " * indent, indent
        cur += ch
        used += w
    out.append(cur)
    return "\n".join(out)


def read_task(parts: list[str]) -> str:
    if parts == ["-"] or (not parts and not sys.stdin.isatty()):
        return sys.stdin.read().strip()
    return " ".join(parts).strip()


def dispatch_settings(policy: dict) -> dict:
    errors = model.validate_dispatch(policy)
    if errors:
        e = herdr.HerdrError("invalid_dispatch_config", "; ".join(f"{p}: {m}" for p, m in errors))
        e.stage, e.prompt_status = "validate", "not_sent"
        raise e
    return policy.get("dispatch", {})


def dispatch_recovery(e: herdr.HerdrError, lang: str = "zh") -> str:
    text = TEXT[lang]
    status = getattr(e, "prompt_status", "not_sent")
    location = f"pane {e.pane_id}" if e.pane_id else "Herdr"
    if e.agent:
        location += text["agent_suffix"].format(agent=e.agent)
    if status == "confirmed":
        return text["recover_confirmed"].format(location=location)
    if status == "unknown":
        return text["recover_unknown"].format(location=location)
    workspace = getattr(e, "workspace", None)
    if e.pane_id:
        suffix = (text["recover_ready"].format(path=workspace["path"], branch=workspace["branch"])
                  if workspace and workspace.get("kind") == "worktree" and workspace.get("state") == "ready" else "")
        return text["recover_pane"].format(location=location, suffix=suffix)
    if workspace and workspace.get("kind") == "worktree":
        return text["recover_worktree"].format(path=workspace["path"], branch=workspace["branch"],
                                               state=workspace["state"])
    return text["recover_default"]


def dispatch_error(e: herdr.HerdrError, d: Decision | None = None, lang: str = "zh") -> dict:
    out = {"error": {"code": e.code, "message": e.message},
           "dispatch_id": getattr(e, "dispatch_id", None), "stage": getattr(e, "stage", None),
           "status": "failed", "prompt_status": getattr(e, "prompt_status", "not_sent"),
           "pane_id": e.pane_id, "agent": e.agent, "recovery": dispatch_recovery(e, lang),
           "session_id": getattr(e, "session_id", None)}
    if getattr(e, "workspace", None):
        out["workspace"] = e.workspace
    if getattr(e, "session_warning", None):
        out["session_warning"] = e.session_warning
    if d is not None:
        out["decision"] = d.to_dict()
    return out


def public_dispatch_error(error: Exception) -> herdr.HerdrError:
    """Keep location/delivery context while hiding unexpected exception text."""
    if isinstance(error, herdr.HerdrError):
        return error
    result = herdr.HerdrError("unexpected_error", "dispatch failed unexpectedly; inspect the recorded attempt")
    for key in ("pane_id", "agent", "dispatch_id", "stage", "session_id", "session_warning", "workspace"):
        setattr(result, key, getattr(error, key, None))
    result.prompt_status = getattr(error, "prompt_status", "unknown")
    return result


def positive_ms(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number of milliseconds")
    return n


def session_limit(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 1000:
        raise argparse.ArgumentTypeError("limit must be from 1 to 1000")
    return n


_HANDOFF_BYTES = 48 * 1024
_PROMPT_BYTES = 64 * 1024


def read_handoff(path: Path) -> dict:
    """Snapshot a bounded ordinary UTF-8 file, never a pipe or stdin."""
    if str(path) == "-":
        raise sessions.SessionError("invalid_handoff", "handoff must be a UTF-8 file, not stdin")
    try:
        source = path.expanduser().resolve()
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise sessions.SessionError("invalid_handoff", "handoff must be an ordinary UTF-8 file")
            data = f.read(_HANDOFF_BYTES + 1)
    except (OSError, ValueError, RuntimeError):
        raise sessions.SessionError("handoff_read_failed", "could not read handoff file") from None
    if len(data) > _HANDOFF_BYTES:
        raise sessions.SessionError("handoff_too_large", "handoff exceeds 48 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise sessions.SessionError("invalid_handoff", "handoff must contain valid UTF-8 text") from None
    if not text.strip() or "\x00" in text:
        raise sessions.SessionError("invalid_handoff", "handoff must contain nonempty text without NUL")
    return {"path": str(source), "text": text}


def session_input(args) -> tuple[str, dict | None, dict | None]:
    """Resolve continuation before assessment; the dispatcher rechecks the parent transactionally."""
    if not args.phase.strip() or len(args.phase) > 64 or any(ord(c) < 32 for c in args.phase):
        raise sessions.SessionError("invalid_phase", "phase must be nonempty, at most 64 characters, without control characters")
    if args.cwd is not None and "\x00" in args.cwd:
        raise sessions.SessionError("invalid_cwd", "working directory must not contain NUL")
    cwd = os.path.realpath(os.path.expanduser(args.cwd or os.getcwd()))
    parent_workspace = None
    if args.session:
        parent = sessions.get(args.session)
        if parent["prompt_status"] != "confirmed":
            raise sessions.SessionError("session_parent_unconfirmed", "inspect the parent attempt before continuing; delivery is not confirmed")
        if not args.handoff:
            raise sessions.SessionError("handoff_required", "continuing a session requires an explicit handoff file")
        parent_cwd = os.path.realpath(parent["cwd"])
        if args.cwd is not None and cwd != parent_cwd:
            raise sessions.SessionError("session_cwd_mismatch", "continuation must use the parent working directory")
        cwd = parent_cwd
        parent_workspace = parent["workspace"]
    if not os.path.isdir(cwd):
        raise sessions.SessionError("invalid_cwd", "working directory does not exist")
    return cwd, read_handoff(args.handoff) if args.handoff else None, parent_workspace


def handoff_prompt(prompt: str, *, parent_id: str | None, phase: str, handoff: dict | None) -> str:
    if handoff:
        prompt = (f"Session handoff (parent: {parent_id or 'none'}, phase: {phase})\n"
                  "The earlier agent conversation is not automatically available. "
                  "Use this explicit handoff as context for the next task.\n\n"
                  f"--- Handoff ---\n{handoff['text']}\n--- End handoff ---\n\nNext task:\n{prompt}")
    if "\x00" in prompt:
        raise sessions.SessionError("invalid_prompt", "task prompt must not contain NUL")
    try:
        size = len(prompt.encode("utf-8"))
    except UnicodeEncodeError:
        raise sessions.SessionError("invalid_prompt", "task prompt must contain valid UTF-8 text") from None
    if size > _PROMPT_BYTES:
        raise sessions.SessionError("prompt_too_large", "final task prompt exceeds 64 KiB")
    return prompt


def run_session(args) -> int:
    try:
        result = sessions.list_sessions(args.limit) if args.session_cmd == "list" else sessions.get(args.id)
    except sessions.SessionError as e:
        print(json.dumps({"error": {"code": e.code, "message": e.message}}, ensure_ascii=False), file=sys.stderr)
        return 1
    if args.json or args.session_cmd == "show":
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    elif not result:
        print("No sessions recorded.")
    else:
        for item in result:
            print(f"{item['id']}  {item['phase']}  {item['status']}/{item['prompt_status']}  {item['cwd']}")
    return 0


# --------------------------------------------------------------------------
# Plugin entrypoints

def plugin_context() -> dict:
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except json.JSONDecodeError:
        return {}


def live_cwd(pane_id: str | None, fallback: str | None) -> str:
    if pane_id:
        try:
            p = herdr.pane_get(pane_id)
            return p.get("foreground_cwd") or p.get("cwd") or fallback or os.getcwd()
        except herdr.HerdrError:
            pass
    return fallback or os.getcwd()


def run_action(action_id: str, lang: str = "zh") -> int:
    if action_id == "open-editor":
        from . import web
        url = web.ensure_running()
        web.open_browser(url)
        return 0
    ctx = plugin_context()
    target = ctx.get("focused_pane_id")
    cwd = live_cwd(target, ctx.get("focused_pane_cwd") or ctx.get("workspace_cwd"))
    env = {"HERDR_ROUTER_CWD": cwd, "HERDR_ROUTER_LANGUAGE": lang}
    if target:
        env["HERDR_ROUTER_TARGET"] = target
    if action_id == "route-selection" and ctx.get("selected_text"):
        d = model.state_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"pending-{int(time.time() * 1000)}.txt"
        f.write_text(ctx["selected_text"], encoding="utf-8")
        env["HERDR_ROUTER_TASK_FILE"] = str(f)
    herdr.plugin_pane_open(PLUGIN_ID, "popup", "popup", cwd=cwd, env=env)
    return 0


POPUP_IDLE_S = 300
POPUP_DONE_S = 1.2


def _ask(prompt: str) -> str:
    """input() that gives up after POPUP_IDLE_S, so an unattended popup cannot hold focus forever.

    Keep prompts short and ASCII: readline mis-measures wide characters, and a prompt longer than the
    pane makes it redraw over the lines above.
    """
    signal.alarm(POPUP_IDLE_S)
    try:
        return input(prompt)
    finally:
        signal.alarm(0)


def _drain_paste() -> list[str]:
    """Lines of a multi-line paste that input() left in the tty queue (it returns at the first newline)."""
    lines: list[str] = []
    try:
        while select.select([0], [], [], 0.08)[0]:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            lines += chunk.decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        pass
    return lines


def _ask_task(prompt: str = TEXT["zh"]["task_prompt"]) -> str:
    """Like _ask, but a pasted multi-line task stays whole instead of spilling into later prompts."""
    first = _ask(prompt)
    rest = _drain_paste() if sys.stdin.isatty() else []
    return "\n".join([first, *rest]).strip()


def _width() -> int:
    return max(30, shutil.get_terminal_size((80, 24)).columns - 1)


def _clear() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def _idle_exit(signum, frame):
    raise SystemExit(0)


def _pick(prompt: str, options: list[str]) -> str | None:
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    raw = _ask(prompt).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return None


def run_popup(policy: dict, lang: str = "zh") -> int:
    text = TEXT[lang]
    try:
        dcfg = dispatch_settings(policy)
    except herdr.HerdrError as e:
        print(f"✗ {e}\n{dispatch_recovery(e, lang)}")
        return 1
    try:
        import readline  # noqa: F401  (line editing for input())
    except ImportError:
        pass
    signal.signal(signal.SIGALRM, _idle_exit)
    target = os.environ.get("HERDR_ROUTER_TARGET")
    cwd = os.environ.get("HERDR_ROUTER_CWD") or os.getcwd()
    task = ""
    if tf := os.environ.get("HERDR_ROUTER_TASK_FILE"):
        p = Path(tf)
        if p.is_file():
            task = p.read_text(encoding="utf-8").strip()
            p.unlink(missing_ok=True)

    print(text["header"].format(target=target or text["current_pane"], cwd=cwd) + "\n")
    if task:
        print(f"{text['selected_task']}\n  {task[:300]}{'…' if len(task) > 300 else ''}\n")
    else:
        task = _ask_task(text["task_prompt"])
    if not task:
        return 0

    profile_ids = [p["id"] for p in policy.get("profiles", [])]
    pinned: dict = {}
    while True:
        _clear()
        print(f"herdr-router  ·  {cwd}\n")
        flat = " ".join(task.split())
        print(_wrap(f"{text['task']}  {flat[:240]}{'…' if len(flat) > 240 else ''}", _width(), 6))
        try:
            d = route_task(task, policy, cwd=cwd, **pinned)
        except NoAgentAvailable as e:
            print(f"\n✗ {e}")
            d = None
        if d:
            print("\n" + render(d, policy, width=_width(), lang=lang) + "\n")
        print(_wrap(text["keys"], _width(), 0))
        choice = _ask("> ").strip().lower()
        if choice == "q":
            return 0
        if choice == "k":
            if pid := _pick(text["profile_prompt"], profile_ids):
                pinned = {"profile": pid}
            continue
        if choice in ("m", "e") and d:
            # Freeze the current pick, then patch only the field being changed.
            pinned = {"profile": d.profile_id, "model_": d.model, "effort": d.effort}
            if choice == "m":
                pinned["model_"] = _ask(text["model_prompt"]).strip() or None
            elif e := _pick(text["effort_prompt"], agents.EFFORTS):
                pinned["effort"] = e
            continue
        if choice == "t":
            task = _ask_task(text["task_prompt"]) or task
            continue
        if choice == "" and d:
            break

    wait_s = int(dcfg.get("agent_start_timeout_ms", 60000)) // 1000
    print("\n" + text["starting"].format(kind=d.kind, seconds=wait_s), flush=True)
    try:
        r = dispatch(d, target_pane=target, cwd=cwd,
                     direction=dcfg.get("direction", "auto"),
                     start_timeout_ms=int(dcfg.get("agent_start_timeout_ms", 60000)), task=task)
        print("\n" + text["confirmed"].format(agent=r.agent, pane=r.pane_id))
        print(f"Session: {getattr(r, 'session_id', r.dispatch_id)}")
        if getattr(r, "session_warning", None):
            print(text["session_warning"].format(warning=r.session_warning))
        time.sleep(POPUP_DONE_S)  # the new pane is the result; no need to hold focus here
        return 0
    except Exception as error:
        e = public_dispatch_error(error)
        print("\n" + text["failed"].format(error=e))
        print(dispatch_recovery(e, lang))
        if e.pane_id:
            screen = herdr.pane_screen(e.pane_id)
            print("\n" + text["pane_screen"].format(pane=e.pane_id))
            print("  " + (screen.replace("\n", "\n  ") or text["empty_screen"]))
    _ask("\n" + text["close"])  # only on failure: leave the error on screen
    return 1


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="herdr-router", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"herdr-router {__version__}")
    ap.add_argument("--policy", type=Path, help="policy JSON (default: config dir policy.json, else built-in)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def task_cmd(name: str, help_: str, pick: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("task", nargs="*", help="task text, or '-' to read stdin")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument("--no-systemone", action="store_true", help="skip the Jev/Kev decision model")
        p.add_argument("--cwd", default=None, help="working directory (default: $PWD)")
        if pick:
            p.add_argument("--profile", help="pin a profile id")
            p.add_argument("--kind", help="restrict the ranking to one agent kind")
            p.add_argument("--model", help="override the winning profile's model")
            p.add_argument("--effort", choices=agents.EFFORTS, help="override the winning profile's effort")
        return p

    task_cmd("classify", "assess a task along each dimension", pick=False)
    for name in ("route", "explain"):
        task_cmd(name, "assess, rank profiles, pick an agent")
    p = task_cmd("dispatch", "route, then launch the agent in a new Herdr pane")
    p.add_argument("--name", help="agent name (default: r-<model|kind>-N)")
    p.add_argument("--pane", help="pane to split (default: the calling pane)")
    p.add_argument("--direction", choices=["auto", "right", "down"])
    p.add_argument("--wait", action="store_true", help="wait until the agent settles")
    p.add_argument("--timeout", type=positive_ms, help="settled-state wait timeout in ms (with --wait)")
    p.add_argument("--dry-run", action="store_true", help="decide only; do not launch")
    p.add_argument("--session", help="parent session id; create a new attempt with its cwd and explicit handoff")
    p.add_argument("--handoff", type=Path, help="UTF-8 handoff file, up to 48 KiB (required with --session)")
    p.add_argument("--phase", default="task", help="phase label for the new session (default: task)")
    p.add_argument("--isolation", choices=("shared", "worktree"),
                   help="new task: shared by default; session continuation inherits its workspace")
    p.add_argument("--worktree-timeout", type=positive_ms, default=60000,
                   help="total Git preparation budget in ms, excluding assessment (default: 60000)")
    s = sub.add_parser("session", help="inspect durable dispatch sessions without contacting Herdr")
    ss = s.add_subparsers(dest="session_cmd", required=True)
    sl = ss.add_parser("list", help="list recent session summaries")
    sl.add_argument("--limit", type=session_limit, default=20)
    sl.add_argument("--json", action="store_true")
    sh = ss.add_parser("show", help="show a session and its saved handoff")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")
    u = sub.add_parser("ui", help="web editor for dimensions and profiles")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    u.add_argument("--lan", action="store_true", help="bind 0.0.0.0 for LAN / Tailscale access")
    u.add_argument("--open", action="store_true", help="open the editor in a browser")
    u.add_argument("--allow-host", action="append", default=[], help="extra Host name to accept (repeatable)")
    u.add_argument("--stop", action="store_true", help="stop a background editor server")
    sub.add_parser("popup", help="interactive picker (plugin pane)")
    a = sub.add_parser("action", help="plugin action entrypoint")
    a.add_argument("action_id", choices=["route", "route-selection", "open-editor"])

    args = ap.parse_args(argv)
    if args.cmd == "session":
        return run_session(args)
    policy = model.load(args.policy)
    extend_path(policy)
    lang = language(policy)

    if args.cmd == "ui":
        from . import web
        if args.stop:
            print("stopped" if web.stop_running() else "not running")
            return 0
        return web.serve(host="0.0.0.0" if args.lan else args.host, port=args.port,
                         launch=args.open, extra_hosts=set(args.allow_host))
    if args.cmd == "popup":
        return run_popup(policy, lang)
    if args.cmd == "action":
        return run_action(args.action_id, lang)

    if args.cmd == "dispatch" and not args.dry_run:
        try:
            dcfg = dispatch_settings(policy)
        except herdr.HerdrError as e:
            print(json.dumps(dispatch_error(e, lang=lang), ensure_ascii=False), file=sys.stderr)
            return 1

    task = read_task(args.task)
    if not task:
        ap.error("empty task")
    cwd = os.path.abspath(args.cwd or os.getcwd())
    handoff = None
    workspace_plan = None
    if args.cmd == "dispatch":
        try:
            cwd, handoff, parent_workspace = session_input(args)
            if not args.json and (args.isolation == "worktree" or
                                  (parent_workspace and parent_workspace["kind"] == "worktree")):
                print(TEXT[lang]["worktree_check"].format(ms=args.worktree_timeout), file=sys.stderr, flush=True)
            workspace_plan = workspaces.inspect(cwd, isolation=args.isolation,
                                                 parent_workspace=parent_workspace,
                                                 timeout_ms=args.worktree_timeout)
        except (sessions.SessionError, workspaces.WorkspaceError) as e:
            failure = herdr.HerdrError(e.code, e.message)
            failure.stage = "session_input"
            failure.workspace = getattr(e, "workspace", None)
            print(json.dumps(dispatch_error(failure, lang=lang), ensure_ascii=False), file=sys.stderr)
            return 1
    use_backend = not args.no_systemone

    if args.cmd == "classify":
        cleaned, ov = parse_overrides(task, policy)
        a = assess(cleaned, policy, ov, cwd=cwd, use_backend=use_backend)
        print(json.dumps(a.to_dict(policy), ensure_ascii=False, indent=None if args.json else 2))
        return 0

    try:
        d = route_task(task, policy, profile=args.profile, kind=args.kind, model_=args.model,
                       effort=args.effort, cwd=cwd, use_backend=use_backend)
    except NoAgentAvailable as e:
        failure = herdr.HerdrError("no_agent_available", str(e))
        failure.stage, failure.prompt_status = "route", "not_sent"
        payload = dispatch_error(failure, lang=lang) if args.cmd == "dispatch" else {
            "error": {"code": failure.code, "message": failure.message}}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.cmd == "dispatch":
        try:
            d.prompt = handoff_prompt(d.prompt, parent_id=args.session, phase=args.phase, handoff=handoff)
        except sessions.SessionError as e:
            failure = herdr.HerdrError(e.code, e.message)
            failure.stage = "session_input"
            print(json.dumps(dispatch_error(failure, d, lang), ensure_ascii=False), file=sys.stderr)
            return 1

    if args.cmd in ("route", "explain") or args.dry_run:
        preview = d.to_dict()
        if args.cmd == "dispatch":
            preview["workspace_plan"] = {"mode": workspace_plan["mode"], "source_cwd": cwd,
                                          "workspace": workspace_plan["workspace"],
                                          "inspection_ms": workspace_plan.get("inspection_ms", 0),
                                          "timeout_ms": args.worktree_timeout}
        if args.json:
            print(json.dumps(preview, ensure_ascii=False))
        else:
            print(render(d, policy, lang=lang))
            if args.cmd == "dispatch":
                print(f"\nWorkspace: {workspace_plan['mode']} · {cwd}")
                if workspace_plan["mode"] == "create":
                    print(TEXT[lang]["worktree_budget"].format(ms=args.worktree_timeout))
        return 0

    # dispatch
    if not herdr.inside_herdr():
        failure = herdr.HerdrError("not_in_herdr", "run inside a Herdr pane")
        failure.stage, failure.prompt_status = "validate", "not_sent"
        print(json.dumps(dispatch_error(failure, d, lang), ensure_ascii=False), file=sys.stderr)
        return 1
    wait_s = int(dcfg.get("agent_start_timeout_ms", 60000)) // 1000
    if not args.json:
        if workspace_plan["mode"] == "create":
            print(TEXT[lang]["worktree_create"], file=sys.stderr, flush=True)
        print("\n" + TEXT[lang]["starting"].format(kind=d.kind, seconds=wait_s), file=sys.stderr, flush=True)
    try:
        r = dispatch(d, target_pane=args.pane, cwd=cwd, name=args.name,
                     direction=args.direction or dcfg.get("direction", "auto"),
                     start_timeout_ms=int(dcfg.get("agent_start_timeout_ms", 60000)),
                     wait=args.wait, wait_timeout_ms=args.timeout,
                     parent_id=args.session, phase=args.phase, task=task, handoff=handoff,
                     isolation=args.isolation, workspace_plan=workspace_plan,
                     worktree_timeout_ms=args.worktree_timeout)
    except Exception as error:
        e = public_dispatch_error(error)
        print(json.dumps(dispatch_error(e, d, lang), ensure_ascii=False), file=sys.stderr)
        return 1
    out = {"agent": r.agent, "pane_id": r.pane_id, "decision": d.to_dict(),
           "dispatch_id": r.dispatch_id, "stage": r.stage,
           "status": "confirmed", "prompt_status": r.prompt_status,
           "session_id": getattr(r, "session_id", r.dispatch_id),
           "parent_id": args.session, "phase": args.phase,
           "workspace": getattr(r, "workspace", None) or workspace_plan["workspace"]}
    if getattr(r, "session_warning", None):
        out["session_warning"] = r.session_warning
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(render(d, policy, lang=lang) + "\n\n" + TEXT[lang]["confirmed"].format(agent=r.agent, pane=r.pane_id))
        print(f"Session: {out['session_id']}")
        if out["workspace"]["kind"] == "worktree":
            w = out["workspace"]
            print(f"Worktree: {w['cwd']}\nBranch: {w['branch']} · "
                  + TEXT[lang]["worktree_created"].format(ms=w["creation_ms"]))
        if out.get("session_warning"):
            print(TEXT[lang]["session_warning"].format(warning=out["session_warning"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
