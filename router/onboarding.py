"""Local installation and read-only checks. Never overwrite an existing skill or key."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from . import model

# Shared root avoids installing duplicate copies for compatible agents.
# Claude and Kimi use their native roots; see the official sources in README.
SKILL_DIRS = {
    "claude": ".claude/skills", "codex": ".agents/skills",
    "cursor": ".agents/skills", "kimi": ".kimi/skills",
    "opencode": ".agents/skills", "droid": ".agents/skills",
}
BINDINGS = {
    "prefix+d": "herdr-router.route",
    "prefix+ctrl+d": "herdr-router.route-selection",
    "prefix+shift+e": "herdr-router.open-editor",
}
BAD = {"missing", "conflict", "error"}


def search_path(policy: dict) -> str:
    return os.pathsep.join([os.environ.get("PATH", ""), *[
        os.path.expanduser(p) for p in policy.get("env", {}).get("extra_path", [])]])


def detected_agents(policy: dict) -> list[str]:
    return [kind for kind in SKILL_DIRS if shutil.which(
        policy.get("binaries", {}).get(kind, "cursor-agent" if kind == "cursor" else kind),
        path=search_path(policy))]


def skill_path(kind: str) -> Path:
    if kind == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "skills/herdr-route"
    return Path.home() / SKILL_DIRS[kind] / "herdr-route"


def link_status(destination: Path, source: Path) -> str:
    if not source.exists() or (source.is_dir() and not (source / "SKILL.md").is_file()):
        return "error"
    if destination.resolve() == source.resolve() and destination.exists():
        return "ok"
    return "conflict" if os.path.lexists(destination) else "missing"


def ensure_link(destination: Path, source: Path, install: bool) -> str:
    status = link_status(destination, source)
    if install and status == "missing":
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source, target_is_directory=source.is_dir())
        return "ok"
    return status


def missing_skills(policy: dict) -> list[str]:
    source = model.REPO_ROOT / "skill"
    return [kind for kind in detected_agents(policy)
            if link_status(skill_path(kind), source) != "ok"]


def setup_hint(policy: dict, lang: str) -> str | None:
    try:
        missing = missing_skills(policy)
    except (OSError, RuntimeError):
        return None  # A best-effort hint must never prevent routing.
    if not missing:
        return None
    names = ", ".join(missing)
    command = "herdr-router" if shutil.which("herdr-router") else shlex.quote(str(model.REPO_ROOT / "bin/herdr-router"))
    if lang == "en":
        return f"Skill setup incomplete ({names}). Run {command} setup, then start a new agent session."
    return f"Skill 尚未配置完成（{names}）。运行 {command} setup，然后新开 agent 会话。"


def herdr_config() -> Path:
    return Path(os.environ.get("HERDR_CONFIG_PATH") or Path.home() / ".config/herdr/config.toml").expanduser()


def plugin_status() -> str:
    # Herdr's local registry is independent of the running server. Doctor needs no RPC.
    registry = Path.home() / ".config/herdr/plugins.json"
    if not registry.exists():
        return "missing"
    plugins = json.loads(registry.read_text(encoding="utf-8"))
    if not isinstance(plugins, list):
        raise ValueError("invalid Herdr plugin registry")
    for plugin in plugins:
        if plugin.get("plugin_id") == "herdr-router":
            if (Path(plugin.get("plugin_root", "")).resolve() == model.REPO_ROOT.resolve()
                    and plugin.get("enabled") is True):
                return "ok"
            return "conflict"
    return "missing"


def run_herdr(binary: str, *args: str) -> None:
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise OSError(f"herdr {' '.join(args[:2])}: "
                      + (result.stderr or result.stdout or f"exit {result.returncode}").strip()[:300])


def key_name(key: str) -> str:
    return "+".join(sorted(p.strip().lower().replace("control", "ctrl") for p in key.split("+")))


def read_bindings(path: Path) -> tuple[bytes, dict]:
    original = path.read_bytes() if path.exists() else b""
    parsed = tomllib.loads(original.decode("utf-8"))
    keys = parsed.get("keys", {})
    if not isinstance(keys, dict):
        raise ValueError("keys must be a TOML table")
    return original, keys


def binding_status(keys: dict, key: str, command: str) -> str:
    matches = []
    # Protect keys in any keybinding category, not only plugin commands.
    for entries in keys.values():
        if isinstance(entries, list):
            matches.extend(e for e in entries if isinstance(e, dict)
                           and isinstance(e.get("key"), str) and key_name(e["key"]) == key_name(key))
    if matches:
        return "ok" if len(matches) == 1 and matches[0].get("type") == "plugin_action" and matches[0].get("command") == command else "conflict"
    # A user may have chosen a different shortcut for this action already.
    if any(isinstance(e, dict) and e.get("type") == "plugin_action" and e.get("command") == command
           for e in keys.get("command", [])):
        return "ok"
    return "missing"


def save_bindings(path: Path, original: bytes, additions: dict) -> Path | None:
    content = original + b"\n# herdr-router shortcuts (added by herdr-router setup)\n"
    for key, command in additions.items():
        content += (f'\n[[keys.command]]\nkey = "{key}"\ntype = "plugin_action"\ncommand = "{command}"\n').encode()
    tomllib.loads(content.decode("utf-8"))  # Validate the complete document before touching it.
    path = path.resolve()  # Keep a user's config symlink intact.
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        if path.read_bytes() != original:
            raise OSError("Herdr config changed during setup; rerun setup")
        fd, name = tempfile.mkstemp(prefix=path.name + ".backup-", dir=path.parent)
        backup = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
    fd, name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def check(args, policy: dict) -> list[dict]:
    install = args.cmd == "setup"
    rows = []

    def add(id_, status, zh, en):
        rows.append({"id": id_, "status": status, "zh": zh, "en": en})

    def link(id_, destination, source):
        try:
            status = ensure_link(destination, source, install)
            add(id_, status, f"{destination} → {source}；已有内容不会被覆盖", f"{destination} -> {source}; existing content is preserved")
        except (OSError, RuntimeError) as error:
            add(id_, "error", str(error), str(error))

    add("python", "ok" if sys.version_info >= (3, 11) else "missing",
        f"Python {sys.version.split()[0]}（需要 3.11+）", f"Python {sys.version.split()[0]} (requires 3.11+)")
    link("cli", Path.home() / ".local/bin/herdr-router", model.REPO_ROOT / "bin/herdr-router")
    visible = shutil.which("herdr-router")
    path_ok = bool(visible and Path(visible).resolve() == (model.REPO_ROOT / "bin/herdr-router").resolve())
    add("path", "ok" if path_ok else "warning",
        "当前 shell 能找到本仓库的 herdr-router" if path_ok else '把 ~/.local/bin 加入 shell 启动配置：export PATH="$HOME/.local/bin:$PATH"；并检查 PATH 中是否有旧版本',
        "This checkout is on the shell PATH" if path_ok else 'Add to your shell startup file: export PATH="$HOME/.local/bin:$PATH"; check for an older router on PATH')
    selected = args.agent or detected_agents(policy)
    detected = detected_agents(policy)
    add("agents", "ok" if detected else "missing",
        "检测到 CLI：" + ", ".join(detected) if detected else "未找到支持的 agent CLI；先安装并登录至少一个 agent",
        "Detected CLIs: " + ", ".join(detected) if detected else "No supported agent CLI found; install and sign in to at least one agent")
    for kind in dict.fromkeys(selected):
        link("skill:" + kind, skill_path(kind), model.REPO_ROOT / "skill")
    if not (model.REPO_ROOT / "skill/SKILL.md").is_file():
        add("skill-source", "error", "仓库缺少 skill/SKILL.md", "Checkout is missing skill/SKILL.md")

    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr", path=search_path(policy))
    add("herdr", "ok" if binary else "missing", binary or "未找到 Herdr；请先安装 Herdr", binary or "Herdr not found; install Herdr first")
    if install and args.no_plugin:
        add("plugin", "skipped", "已跳过插件链接", "Plugin linking skipped")
    else:
        try:
            status = plugin_status()
            if install and status == "missing" and binary:
                run_herdr(binary, "plugin", "link", str(model.REPO_ROOT))
                status = plugin_status()
            add("plugin", status,
                "Herdr 插件；缺失时运行 setup；版本不同或已禁用时检查 herdr plugin list --plugin herdr-router --json",
                "Herdr plugin; run setup if missing; inspect conflicts with herdr plugin list --plugin herdr-router --json")
        except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired) as error:
            add("plugin", "error", str(error), str(error))

    if install and args.no_shortcuts:
        add("shortcuts", "skipped", "已跳过快捷键配置", "Shortcut configuration skipped")
    else:
        try:
            path = herdr_config()
            original, keys = read_bindings(path)
            statuses = {key: binding_status(keys, key, command) for key, command in BINDINGS.items()}
            additions = {key: BINDINGS[key] for key, status in statuses.items() if status == "missing"}
            if install and additions:
                backup = save_bindings(path, original, additions)
                if backup:
                    add("config-backup", "ok", f"配置备份：{backup}", f"Config backup: {backup}")
                statuses.update({key: "ok" for key in additions})
            for key, status in statuses.items():
                add("key:" + key, status, f"{key} → {BINDINGS[key]}；冲突时保留原快捷键", f"{key} -> {BINDINGS[key]}; conflicting keys are preserved")
            if install and additions:
                try:
                    if binary:
                        run_herdr(binary, "server", "reload-config")
                    else:
                        raise OSError("Herdr not found")
                    add("reload", "ok", "已重新加载 Herdr 配置", "Herdr config reloaded")
                except (OSError, subprocess.TimeoutExpired) as error:
                    add("reload", "warning", f"配置已保存；启动 Herdr 或运行 herdr server reload-config：{error}", f"Config saved; start Herdr or run herdr server reload-config: {error}")
        except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as error:
            add("shortcuts", "error", f"保留原配置：{error}", f"Original config preserved: {error}")
    return rows


def run(args) -> int:
    rows = []
    try:
        policy = model.load(args.policy)
        errors = model.validate(policy)
        if errors:
            raise ValueError(json.dumps(errors, ensure_ascii=False))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        rows.append({"id": "policy", "status": "error", "zh": str(error), "en": str(error)})
        policy = model.load_default()
    rows.extend(check(args, policy))
    # Import here to share terminal-language behavior without an import cycle.
    from .cli import language
    lang = language(policy)
    rows = [{"id": row["id"], "status": row["status"], "message": row[lang]} for row in rows]
    ok = not any(row["status"] in BAD for row in rows)
    if args.json:
        print(json.dumps({"command": args.cmd, "ok": ok, "checks": rows}, ensure_ascii=False))
    else:
        for row in rows:
            print(f"[{row['status']}] {row['id']}: {row['message']}")
        if args.cmd == "setup":
            print("\n新开 agent 会话后使用 herdr-route；安装 CLI 不代表已经登录或有模型权限。" if lang == "zh" else
                  "\nStart a new agent session to load herdr-route. CLI detection does not check login or model access.")
        elif not ok:
            print("\n运行 herdr-router setup 补齐配置；冲突项请先检查已有内容。" if lang == "zh" else
                  "\nRun herdr-router setup; inspect existing content for conflicts.")
    return 0 if ok else 1
