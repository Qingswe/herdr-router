"""Local web editor for the routing policy.

Static files (web/) are public; every /api call needs the access token, from
the `hr_token` cookie (set on first visit via ?token=) or an X-Router-Token
header. The editor can change agent launch arguments, so the token guards
against cross-site requests even on 127.0.0.1. The web UI never dispatches
agents: it edits the policy and dry-runs routing only.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, catalog, herdr, model
from .classify import assess, parse_overrides
from .score import NoAgentAvailable, decide, rank_at, territory

WEB_ROOT = model.REPO_ROOT / "web"
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8"}
MAX_BODY = 1 << 20
BAD_TOKEN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>令牌不对</title><style>body{font:15px/1.6 -apple-system,"PingFang SC",sans-serif;max-width:560px;margin:12vh auto;padding:0 20px}
code{background:#8882;padding:1px 5px;border-radius:4px}</style>
<h2>链接里的令牌不对</h2>
<p>最常见的原因是终端把长链接折成了两行，点击时只取到了前半截。</p>
<ul><li>在终端运行 <code>herdr-router ui --open</code>，会直接用浏览器打开，不用复制链接；</li>
<li>或者复制终端里打印的完整链接，确认结尾没有被截断；</li>
<li>如果编辑器重启过，旧链接会失效，请用新打印的链接。</li></ul>"""
DEFAULT_PORT = 8765


def token_path() -> Path:
    return model.state_dir() / "ui-token"


def server_info_path() -> Path:
    return model.state_dir() / "ui.json"


TOKEN_BYTES = 16  # 128 bits; 22 characters keeps the URL inside a 60-column pane


def get_token(regenerate_legacy: bool = False) -> str:
    p = token_path()
    if p.is_file():
        tok = p.read_text().strip()
        # Tokens from 0.1 were 43 characters; their URLs wrapped in narrow panes
        # and lost their tail when clicked. The server swaps them on start.
        if len(tok) >= 22 and not (regenerate_legacy and len(tok) > 30):
            return tok
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(TOKEN_BYTES)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


# --------------------------------------------------------------------------
# Host allowlist (DNS-rebinding defence on top of the token)

def _host_allowed(host_header: str, extra: set[str]) -> bool:
    host = host_header.rsplit(":", 1)[0].strip("[]").lower() if host_header else ""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True  # IP literals cannot be rebound
    except ValueError:
        pass
    name = socket.gethostname().lower()
    allowed = {"localhost", name, name.split(".")[0], f"{name.split('.')[0]}.local"} | extra
    return host in allowed or host.endswith(".ts.net")


def _lan_addresses() -> list[tuple[str, str]]:
    """(label, ip) for private LAN and Tailscale addresses. Proxy TUN interfaces
    (198.18.0.0/15) own the default route on many setups, so enumerate interfaces
    instead of asking the routing table."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        try:
            out = subprocess.run(["ip", "-4", "addr"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
    tailnet = ipaddress.ip_network("100.64.0.0/10")
    found: list[tuple[str, str]] = []
    for raw in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        ip = ipaddress.ip_address(raw)
        if ip in tailnet:
            found.append(("Tailscale", raw))
        elif ip.is_private and not ip.is_loopback and not ip.is_link_local \
                and ip not in ipaddress.ip_network("198.18.0.0/15"):
            found.append(("局域网", raw))
    return sorted(set(found), key=lambda t: (t[0] != "局域网", t[1]))


# --------------------------------------------------------------------------

def _installed_kinds(policy: dict) -> dict[str, bool]:
    binaries = policy.get("binaries", {})
    kinds = herdr.supported_kinds() or model.KNOWN_KINDS
    return {k: shutil.which(binaries.get(k, k)) is not None for k in kinds}


def _recent_log(limit: int) -> list[dict]:
    path = model.state_dir() / "decisions.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        d = rec.get("decision", {})
        a = d.get("assessment", {})
        out.append({
            "ts": rec.get("ts"), "agent": rec.get("agent"), "error": rec.get("error"),
            "profile_id": d.get("profile_id"), "kind": d.get("kind"),
            "task": (d.get("prompt") or "")[-160:],
            "expected": a.get("expected"), "choices": a.get("choices"),
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"herdr-router/{__version__}"
    token: str = ""
    extra_hosts: set[str] = set()

    def log_message(self, fmt, *args):  # keep the terminal quiet; errors still surface
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write(f"[ui] {self.command} {self.path.split('?')[0]} -> {args[1]}\n")

    # ---- plumbing
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _authed(self) -> bool:
        supplied = self.headers.get("X-Router-Token", "")
        if not supplied:
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "hr_token":
                    supplied = v
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("Content-Type must be application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            raise ValueError("missing or oversized body")
        return json.loads(self.rfile.read(n))

    def _guard(self) -> bool:
        if not _host_allowed(self.headers.get("Host", ""), self.extra_hosts):
            self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            return False
        return True

    # ---- routes
    def do_GET(self):
        if not self._guard():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            return self._json(200, {"ok": True, "version": __version__})
        if url.path in STATIC:
            qs = urllib.parse.parse_qs(url.query)
            if "token" in qs:  # trade the URL token for a cookie, then drop it from the address bar
                if not hmac.compare_digest(qs["token"][0], self.token):
                    return self._send(HTTPStatus.UNAUTHORIZED, BAD_TOKEN_PAGE.encode(), MIME[".html"])
                cookie = f"hr_token={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": url.path, "Set-Cookie": cookie})
            f = WEB_ROOT / STATIC[url.path]
            return self._send(200, f.read_bytes(), MIME[f.suffix])
        if not url.path.startswith("/api/"):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if not self._authed():
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
        if url.path == "/api/policy":
            user = model.user_policy_path()
            policy = model.load()
            return self._json(200, {"policy": policy, "source": "user" if user.is_file() else "default",
                                    "path": str(user), "installed": _installed_kinds(policy)})
        if url.path == "/api/policy/default":
            return self._json(200, {"policy": model.load_default()})
        if url.path == "/api/models":
            return self._json(200, {"models": catalog.models(model.load())})
        if url.path == "/api/log":
            limit = int(urllib.parse.parse_qs(url.query).get("limit", ["100"])[0])
            return self._json(200, {"entries": _recent_log(max(1, min(limit, 1000)))})
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    do_HEAD = do_GET

    def do_PUT(self):
        if not self._guard():
            return
        if not self._authed():
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
        if urllib.parse.urlsplit(self.path).path != "/api/policy":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            policy = model.migrate(self._body())
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        kinds = herdr.supported_kinds() or None
        try:
            path = model.save(policy, kinds=kinds)
        except model.InvalidPolicy as e:
            return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": e.errors})
        return self._json(200, {"ok": True, "path": str(path)})

    def do_POST(self):
        if not self._guard():
            return
        if not self._authed():
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/route", "/api/map"):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        if path == "/api/map":
            return self._map(body)
        task = str(body.get("task", "")).strip()
        if not task:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "empty task"})
        # Dry-run against the editor's unsaved draft when one is sent.
        policy = model.migrate(body.get("policy")) or model.load()
        if errors := model.validate(policy, herdr.supported_kinds() or None):
            return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": errors})
        cleaned, ov = parse_overrides(task, policy)
        a = assess(cleaned, policy, ov, use_backend=bool(body.get("use_backend", True)))
        try:
            d = decide(cleaned, a, policy, pinned_profile=ov.profile)
        except NoAgentAvailable as e:
            return self._json(200, {"assessment": a.to_dict(policy), "error": str(e)})
        return self._json(200, {"decision": d.to_dict()})

    def _map(self, body: dict) -> None:
        policy = model.migrate(body.get("policy")) or model.load()
        if errors := model.validate(policy, herdr.supported_kinds() or None):
            return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": errors})
        score_ids = {d["id"] for d in model.dimensions(policy, "score")}
        x, y = body.get("x"), body.get("y")
        if x not in score_ids or y not in score_ids:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "x and y must be score dimension ids"})
        choice = body.get("choice") if isinstance(body.get("choice"), dict) else {}
        at = body.get("at")
        if at is not None:  # one point: the top three there, for the hover readout
            if not (isinstance(at, list) and len(at) == 2 and all(isinstance(v, (int, float)) for v in at)):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "at must be [x, y]"})
            ranking = rank_at(policy, {x: float(at[0]), y: float(at[1])}, choice)
            return self._json(200, {"top": [{"id": r.profile_id, "total": r.total} for r in ranking[:3]]})
        try:
            steps = max(8, min(128, int(body.get("steps", 48))))
        except (TypeError, ValueError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "steps must be an integer"})
        return self._json(200, territory(policy, x, y, choice, steps=steps))


# --------------------------------------------------------------------------

def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, launch: bool = False,
          extra_hosts: set[str] | None = None) -> int:
    tok = get_token(regenerate_legacy=True)
    Handler.token = tok
    Handler.extra_hosts = {h.lower() for h in (extra_hosts or set())}
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"cannot listen on {host}:{port}: {e}", file=sys.stderr)
        return 1
    info = {"pid": os.getpid(), "host": host, "port": port}
    server_info_path().parent.mkdir(parents=True, exist_ok=True)
    server_info_path().write_text(json.dumps(info))

    local = f"http://127.0.0.1:{port}/?token={tok}"
    # One URL per line, no indent: narrow panes wrap long lines and clicks lose the tail.
    print("herdr-router editor（本机）:")
    print(local)
    if host in ("0.0.0.0", "::"):
        addrs = _lan_addresses()
        for label, ip in addrs:
            print(f"{label}:")
            print(f"http://{ip}:{port}/?token={tok}")
        if not addrs:
            print("（没找到局域网 / Tailscale 地址）")
    print("Ctrl+C 停止；加 --open 可直接在浏览器打开", flush=True)
    if launch:
        open_browser(local)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            if json.loads(server_info_path().read_text()).get("pid") == os.getpid():
                server_info_path().unlink()
        except (OSError, json.JSONDecodeError):
            pass
    return 0


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
            return json.load(r).get("ok") is True
    except (OSError, ValueError):
        return False


def running_info() -> dict | None:
    try:
        info = json.loads(server_info_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return info if _healthy(int(info.get("port", 0))) else None


def ensure_running(port: int = DEFAULT_PORT) -> str:
    """Start a background editor server if none answers; return its tokenised URL."""
    info = running_info()
    if not info:
        log = (model.state_dir() / "ui.log").open("a")
        subprocess.Popen([sys.executable, str(model.REPO_ROOT / "bin" / "herdr-router"), "ui", "--port", str(port)],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            time.sleep(0.1)
            if info := running_info():
                break
        else:
            raise RuntimeError(f"editor did not start; see {model.state_dir() / 'ui.log'}")
    return f"http://127.0.0.1:{info['port']}/?token={get_token()}"


def stop_running() -> bool:
    info = running_info()
    if not info:
        return False
    os.kill(int(info["pid"]), signal.SIGTERM)
    return True


def open_browser(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        import webbrowser
        webbrowser.open(url)
