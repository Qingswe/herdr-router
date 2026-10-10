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
import shlex
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


def render(d: Decision, policy: dict, top: int = 3) -> str:
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
    return "\n".join(f"{_pad(k, 9)}{v}" for k, v in rows)


def _pad(text: str, width: int) -> str:
    """Left-justify by terminal display width (CJK characters take two cells)."""
    cells = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(1, width - cells)


def read_task(parts: list[str]) -> str:
    if parts == ["-"] or (not parts and not sys.stdin.isatty()):
        return sys.stdin.read().strip()
    return " ".join(parts).strip()


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


def _ask(prompt: str) -> str:
    """input() that gives up after POPUP_IDLE_S, so an unattended popup cannot hold focus forever."""
    signal.alarm(POPUP_IDLE_S)
    try:
        return input(prompt)
    finally:
        signal.alarm(0)


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
        task = _ask("任务> ").strip()
    if not task:
        return 0

    profile_ids = [p["id"] for p in policy.get("profiles", [])]
    pinned: dict = {}
    while True:
        try:
            d = route_task(task, policy, cwd=cwd, **pinned)
        except NoAgentAvailable as e:
            print(f"\n✗ {e}")
            d = None
        if d:
            print("\n" + render(d, policy) + "\n")
        choice = _ask("[Enter] 派发   k 换档位   m 换模型   e 换 effort   t 改任务   q 取消 > ").strip().lower()
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
            task = _ask("任务> ").strip() or task
            continue
        if choice == "" and d:
            break

    dcfg = policy.get("dispatch", {})
    try:
        r = dispatch(d, target_pane=target, cwd=cwd,
                     direction=dcfg.get("direction", "auto"),
                     start_timeout_ms=int(dcfg.get("agent_start_timeout_ms", 60000)))
        print(f"\n✓ 已派发给 {r.agent}（pane {r.pane_id}）")
    except herdr.HerdrError as e:
        print(f"\n✗ 派发失败：{e}")
    _ask("\n按回车关闭")
    return 0


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
    p.add_argument("--timeout", type=int, help="wait timeout in ms")
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
        print(json.dumps({"error": {"code": "no_agent_available", "message": str(e)}}), file=sys.stderr)
        return 1

    if args.cmd in ("route", "explain") or args.dry_run:
        print(json.dumps(d.to_dict(), ensure_ascii=False) if args.json else render(d, policy))
        return 0

    # dispatch
    if not herdr.inside_herdr():
        print(json.dumps({"error": {"code": "not_in_herdr", "message": "run inside a Herdr pane"}}), file=sys.stderr)
        return 1
    dcfg = policy.get("dispatch", {})
    try:
        r = dispatch(d, target_pane=args.pane, cwd=cwd, name=args.name,
                     direction=args.direction or dcfg.get("direction", "auto"),
                     start_timeout_ms=int(dcfg.get("agent_start_timeout_ms", 60000)),
                     wait=args.wait, wait_timeout_ms=args.timeout)
    except herdr.HerdrError as e:
        print(json.dumps({"error": {"code": e.code, "message": e.message},
                          "decision": d.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    out = {"agent": r.agent, "pane_id": r.pane_id, "decision": d.to_dict()}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(render(d, policy) + f"\n\n✓ 已派发给 {r.agent}（pane {r.pane_id}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
