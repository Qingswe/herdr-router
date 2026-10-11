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
import sys
import time
import unicodedata
from pathlib import Path

from . import PLUGIN_ID, __version__, agents, herdr, model
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


_BARS = " ▁▂▃▄▅▆▇█"


def _spark(dist: dict, n: int) -> str:
    return "".join(_BARS[min(8, round(float(dist.get(str(i), 0)) * 8))] for i in range(n))


def render(d: Decision, policy: dict, top: int = 3, width: int | None = None) -> str:
    a = d.assessment
    conf = f"{a['source']}, confidence {a['confidence']}"
    if a.get("bumped"):
        conf += ", 低置信度→难度上调"
    rows = [("判断来源", conf)]
    for dim in model.dimensions(policy):
        dist = a["dists"].get(dim["id"])
        if not dist:
            rows.append((dim.get("label", dim["id"]), "—"))
        elif dim["type"] == "score":
            n = len(dim["levels"])
            rows.append((dim.get("label", dim["id"]),
                         f"{a['expected'][dim['id']]} / {n - 1}   [{_spark(dist, n)}]"))
        else:
            best = max(dist, key=dist.get)
            rows.append((dim.get("label", dim["id"]), f"{best} ({dist[best]:.2f})"))
    rows.append(("选择", f"{d.profile_label} [{d.profile_id}]  →  {d.summary}"))
    rows.append(("原因", d.reason))
    for i, r in enumerate(d.ranking[:top], 1):
        parts = ", ".join(f"{k}={v}" for k, v in r["breakdown"].items() if v)
        flag = "" if r["available"] else f"  ✗ {r['why_unavailable']}"
        rows.append((f"#{i}", f"{r['profile_id']:<18} {r['total']:<6} ({parts or '0'}){flag}"))
    if err := a.get("details", {}).get("systemone_error"):
        rows.append(("决策模型", f"不可用，已用关键词规则 ({err[:60]})"))
    native = " ".join(shlex.quote(x) for x in d.args)
    rows.append(("启动参数", f"--kind {d.kind}" + (f" -- {native}" if native else "")))
    if not width:
        return "\n".join(f"{_pad(k, 9)}{v}" for k, v in rows)
    return "\n".join(_wrap(f"{_pad(k, 9)}{v}", width, 9) for k, v in rows)


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


def dispatch_recovery(e: herdr.HerdrError) -> str:
    status = getattr(e, "prompt_status", "not_sent")
    location = f"pane {e.pane_id}" if e.pane_id else "Herdr"
    if e.agent:
        location += f"（agent {e.agent}）"
    if status == "confirmed":
        return f"任务已确认送达，但等待结束失败。请查看 {location}，不要重新派发。"
    if status == "unknown":
        return f"任务可能已送达。请先查看 {location} 确认执行状态，不要直接重新派发。"
    if e.pane_id:
        return f"任务未发送，{location} 仍保留。请处理该 pane 的启动或确认问题，再决定如何发送任务。"
    return "任务未发送。请修正错误；若创建 pane 时发生通信错误，先检查 Herdr 是否已留下 pane。"


def dispatch_error(e: herdr.HerdrError, d: Decision | None = None) -> dict:
    out = {"error": {"code": e.code, "message": e.message},
           "dispatch_id": getattr(e, "dispatch_id", None), "stage": getattr(e, "stage", None),
           "status": "failed", "prompt_status": getattr(e, "prompt_status", "not_sent"),
           "pane_id": e.pane_id, "agent": e.agent, "recovery": dispatch_recovery(e)}
    if d is not None:
        out["decision"] = d.to_dict()
    return out


def positive_ms(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number of milliseconds")
    return n


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


def run_action(action_id: str) -> int:
    if action_id == "open-editor":
        from . import web
        url = web.ensure_running()
        web.open_browser(url)
        return 0
    ctx = plugin_context()
    target = ctx.get("focused_pane_id")
    cwd = live_cwd(target, ctx.get("focused_pane_cwd") or ctx.get("workspace_cwd"))
    env = {"HERDR_ROUTER_CWD": cwd}
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


def _ask_task(prompt: str = "任务> ") -> str:
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


def run_popup(policy: dict) -> int:
    try:
        dcfg = dispatch_settings(policy)
    except herdr.HerdrError as e:
        print(f"✗ {e}\n{dispatch_recovery(e)}")
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

    print(f"herdr-router  ·  目标 pane {target or '(当前)'}  ·  {cwd}\n")
    if task:
        print(f"选中文本作为任务：\n  {task[:300]}{'…' if len(task) > 300 else ''}\n")
    else:
        task = _ask_task()
    if not task:
        return 0

    profile_ids = [p["id"] for p in policy.get("profiles", [])]
    pinned: dict = {}
    while True:
        _clear()
        print(f"herdr-router  ·  {cwd}\n")
        flat = " ".join(task.split())
        print(_wrap(f"任务  {flat[:240]}{'…' if len(flat) > 240 else ''}", _width(), 6))
        try:
            d = route_task(task, policy, cwd=cwd, **pinned)
        except NoAgentAvailable as e:
            print(f"\n✗ {e}")
            d = None
        if d:
            print("\n" + render(d, policy, width=_width()) + "\n")
        print(_wrap("Enter 派发 · k 换档位 · m 换模型 · e 换 effort · t 改任务 · q 取消", _width(), 0))
        choice = _ask("> ").strip().lower()
        if choice == "q":
            return 0
        if choice == "k":
            if pid := _pick("档位编号> ", profile_ids):
                pinned = {"profile": pid}
            continue
        if choice in ("m", "e") and d:
            # Freeze the current pick, then patch only the field being changed.
            pinned = {"profile": d.profile_id, "model_": d.model, "effort": d.effort}
            if choice == "m":
                pinned["model_"] = _ask("模型名（留空=档位默认）> ").strip() or None
            elif e := _pick("effort 编号> ", agents.EFFORTS):
                pinned["effort"] = e
            continue
        if choice == "t":
            task = _ask_task() or task
            continue
        if choice == "" and d:
            break

    wait_s = int(dcfg.get("agent_start_timeout_ms", 60000)) // 1000
    print(f"\n正在启动 {d.kind}，最多等 {wait_s} 秒，随后确认任务送达…", flush=True)
    try:
        r = dispatch(d, target_pane=target, cwd=cwd,
                     direction=dcfg.get("direction", "auto"),
                     start_timeout_ms=int(dcfg.get("agent_start_timeout_ms", 60000)))
        print(f"\n✓ 任务已确认送达 {r.agent}（pane {r.pane_id}）")
        time.sleep(POPUP_DONE_S)  # the new pane is the result; no need to hold focus here
        return 0
    except herdr.HerdrError as e:
        print(f"\n✗ 派发失败：{e}")
        print(dispatch_recovery(e))
        if e.pane_id:
            screen = herdr.pane_screen(e.pane_id)
            print(f"\npane {e.pane_id} 现在的画面：")
            print("  " + (screen.replace("\n", "\n  ") or "(空)"))
    _ask("\n按回车关闭")  # only on failure: leave the error on screen
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
    policy = model.load(args.policy)
    extend_path(policy)

    if args.cmd == "ui":
        from . import web
        if args.stop:
            print("stopped" if web.stop_running() else "not running")
            return 0
        return web.serve(host="0.0.0.0" if args.lan else args.host, port=args.port,
                         launch=args.open, extra_hosts=set(args.allow_host))
    if args.cmd == "popup":
        return run_popup(policy)
    if args.cmd == "action":
        return run_action(args.action_id)

    if args.cmd == "dispatch" and not args.dry_run:
        try:
            dcfg = dispatch_settings(policy)
        except herdr.HerdrError as e:
            print(json.dumps(dispatch_error(e), ensure_ascii=False), file=sys.stderr)
            return 1

    task = read_task(args.task)
    if not task:
        ap.error("empty task")
    cwd = os.path.abspath(args.cwd or os.getcwd())
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
        payload = dispatch_error(failure) if args.cmd == "dispatch" else {
            "error": {"code": failure.code, "message": failure.message}}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.cmd in ("route", "explain") or args.dry_run:
        print(json.dumps(d.to_dict(), ensure_ascii=False) if args.json else render(d, policy))
        return 0

    # dispatch
    if not herdr.inside_herdr():
        failure = herdr.HerdrError("not_in_herdr", "run inside a Herdr pane")
        failure.stage, failure.prompt_status = "validate", "not_sent"
        print(json.dumps(dispatch_error(failure, d), ensure_ascii=False), file=sys.stderr)
        return 1
    wait_s = int(dcfg.get("agent_start_timeout_ms", 60000)) // 1000
    if not args.json:
        print(f"\n正在启动 {d.kind}，最多等 {wait_s} 秒，随后确认任务送达…", file=sys.stderr, flush=True)
    try:
        r = dispatch(d, target_pane=args.pane, cwd=cwd, name=args.name,
                     direction=args.direction or dcfg.get("direction", "auto"),
                     start_timeout_ms=int(dcfg.get("agent_start_timeout_ms", 60000)),
                     wait=args.wait, wait_timeout_ms=args.timeout)
    except herdr.HerdrError as e:
        print(json.dumps(dispatch_error(e, d), ensure_ascii=False), file=sys.stderr)
        return 1
    out = {"agent": r.agent, "pane_id": r.pane_id, "decision": d.to_dict(),
           "dispatch_id": r.dispatch_id, "stage": r.stage,
           "status": "confirmed", "prompt_status": r.prompt_status}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(render(d, policy) + f"\n\n✓ 任务已确认送达 {r.agent}（pane {r.pane_id}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
