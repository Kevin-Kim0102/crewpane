"""systemd user-service installer (Linux). Pure planning functions plus an explicit `apply`.

Nothing here changes the system unless apply_install/apply_uninstall is called. Both refuse while a Kiro task is
active, never touch the panel's config/registry/history, and never enable automatic conversation cleanup."""
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys

from . import ROOT

UNIT = "coder-agents"
LEGACY_UNIT = "codex-kiro-panel"  # the installer shipped before crewpane; this module never edits it
DEFAULT_PORT = 47831
ACTIVE_STATUSES = {"checking", "running"}
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{24,}")


def config_path(environment=None):
    """Panel config. The location stays compatible with existing Kiro Panel installs."""
    environment = os.environ if environment is None else environment
    explicit = environment.get("CODER_AGENTS_CONFIG")
    return Path(explicit).expanduser() if explicit else Path.home() / ".local/state/kiro-panel/config.json"


def unit_dir(environment=None):
    environment = os.environ if environment is None else environment
    base = environment.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd/user"


def _quote(value):
    """One systemd command-line word / Environment value: quoted, with % and $ escaped."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"' + text + '"'


def service_path_value(environment=None):
    environment = os.environ if environment is None else environment
    parts = [str(Path.home() / ".local/bin")]
    for item in environment.get("PATH", "").split(os.pathsep):
        if item and os.path.isabs(item) and item not in parts:
            parts.append(item)
    for item in ("/usr/local/bin", "/usr/bin", "/bin"):
        if item not in parts:
            parts.append(item)
    return os.pathsep.join(parts)


def render_units(python, root, config, port, path_value):
    watch = Path(root) / "scripts/watch_kiro.py"
    socket_text = (f"[Unit]\nDescription=crewpane fixed local entry (loopback only)\n\n"
                   f"[Socket]\nListenStream=127.0.0.1:{int(port)}\nAccept=no\nNoDelay=true\nService={UNIT}.service\n\n"
                   f"[Install]\nWantedBy=sockets.target\n")
    service_text = (f"[Unit]\nDescription=crewpane panel on demand\nRequires={UNIT}.socket\nAfter={UNIT}.socket\n\n"
                    f"[Service]\nType=simple\n"
                    f"ExecStart={_quote(python)} {_quote(watch)} {_quote('--serve')} {_quote('--systemd-socket')} "
                    f"{_quote('--config')} {_quote(config)}\n"
                    f"Environment={_quote('PATH=' + path_value)}\nUMask=0077\nKillMode=process\nTimeoutStopSec=5\nRestart=no\n")
    return {f"{UNIT}.socket": socket_text, f"{UNIT}.service": service_text}


def current_boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


class TaskCheckError(RuntimeError):
    """The registry or run states could not be read, so "no active tasks" cannot be shown."""


def registered_roots(config_file):
    """Log roots known to the panel: its registry plus the config's default project.

    Fails closed: a registry or config that exists but cannot be read or parsed raises TaskCheckError."""
    config_file = Path(config_file)
    roots = []

    def load(path):
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise TaskCheckError(f"无法读取 {path}（{error}），不能确认没有活跃任务") from error
        if not isinstance(data, dict):
            raise TaskCheckError(f"{path} 格式无效，不能确认没有活跃任务")
        return data

    saved = load(config_file.parent / "projects.json")
    for key in ("projects", "discovered", "daily"):
        items = (saved or {}).get(key, [])
        if not isinstance(items, list):
            raise TaskCheckError(f"projects.json 的 {key} 格式无效，不能确认没有活跃任务")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("root"), str):
                raise TaskCheckError(f"projects.json 的 {key} 含无法识别的条目，不能确认没有活跃任务")
            roots.append(Path(item["root"]))
    default = (load(config_file) or {}).get("default_project")
    if isinstance(default, dict) and isinstance(default.get("root"), str):
        roots.append(Path(default["root"]))
    return list(dict.fromkeys(roots))


def find_active_tasks(config_file, boot_id=None):
    """(active, stale): run directories whose state.json says checking/running.

    A state recorded in an earlier boot cannot be running any more; it is reported as stale and does not block.
    Fail closed: unreadable registry, run directory listing or state file raises TaskCheckError or counts as active.
    A root directory that does not exist has no runs."""
    boot_id = boot_id or current_boot_id()
    active, stale = [], []
    for root in registered_roots(config_file):
        try:
            entries = list(root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise TaskCheckError(f"无法列出 {root}（{error}），不能确认没有活跃任务") from error
        for run in entries:
            state_file = run / "state.json"
            try:
                if not state_file.is_file():
                    continue
                state = json.loads(state_file.read_text())
            except (OSError, ValueError):
                active.append(run)
                continue
            if not isinstance(state, dict) or state.get("status") not in ACTIVE_STATUSES:
                continue
            recorded = state.get("boot_id")
            (stale if recorded and boot_id and recorded != boot_id else active).append(run)
    return active, stale


def ensure_no_active_tasks(config_file):
    """Re-checked immediately before any system change; a stale plan is never trusted."""
    try:
        active, _ = find_active_tasks(config_file)
    except TaskCheckError as error:
        raise PermissionError(str(error)) from error
    if active:
        raise PermissionError(f"有 {len(active)} 个活跃任务（checking/running），已拒绝：" + "; ".join(str(p) for p in active[:5]))


def port_in_use(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0


def read_config(config_file):
    """(config dict or None, problem or None)."""
    try:
        data = json.loads(Path(config_file).read_text())
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as error:
        return None, f"现有配置无法读取：{error}"
    port = data.get("port") if isinstance(data, dict) else None
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        return None, "现有配置的 port 无效"
    if not isinstance(data.get("token"), str) or not TOKEN_RE.fullmatch(data["token"]):
        return None, "现有配置的 token 无效"
    return data, None


def new_config(port):
    return {"port": port, "token": secrets.token_urlsafe(32), "idle_timeout": 120, "auto_cleanup_enabled": False}


def plan_install(port=None, environment=None, python=None, root=None, listening=port_in_use):
    """Everything install would do, and why it would be refused. Changes nothing."""
    environment = os.environ if environment is None else environment
    config_file = config_path(environment)
    units = unit_dir(environment)
    blockers, warnings = [], []
    if not sys.platform.startswith("linux"):
        blockers.append("仅支持 Linux（systemd 用户服务）")
    existing, problem = read_config(config_file)
    if problem:
        blockers.append(problem)
    if existing is not None:
        if port is not None and port != existing["port"]:
            blockers.append(f"现有配置端口为 {existing['port']}，与 --port {port} 不一致；安装器不会改写旧配置")
        port = existing["port"]
        if existing.get("auto_cleanup_enabled") is not False:
            blockers.append("现有配置的 auto_cleanup_enabled 不是 false；安装器不会修改旧配置，也不会安装带自动清理的服务，"
                            "请自行确认并设置为 false 后重试")
    else:
        port = port or DEFAULT_PORT
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            blockers.append("端口必须在 1024-65535 之间")
    legacy = [n for n in (f"{LEGACY_UNIT}.socket", f"{LEGACY_UNIT}.service") if (units / n).exists()]
    own = [n for n in (f"{UNIT}.socket", f"{UNIT}.service") if (units / n).exists()]
    if legacy and not own:
        blockers.append(f"检测到旧版服务单元 {', '.join(legacy)}，它可能占用固定端口。切换服务是单独的迁移步骤，"
                        "本安装器不会停用或修改旧服务")
    if not own and isinstance(port, int) and listening(port):
        blockers.append(f"端口 127.0.0.1:{port} 已被占用（可能是旧服务），拒绝安装")
    active, stale = [], []
    try:
        active, stale = find_active_tasks(config_file)
    except TaskCheckError as error:
        blockers.append(str(error))
    if active:
        blockers.append(f"有 {len(active)} 个活跃任务（checking/running），安装器不会中断它们：" +
                        "; ".join(str(p) for p in active[:5]))
    if stale:
        warnings.append(f"{len(stale)} 个运行记录来自更早的开机周期且仍标记为运行中，已视为陈旧，不阻止安装")
    python = python or sys.executable
    root = root or ROOT
    files = render_units(python, root, config_file, port if isinstance(port, int) else DEFAULT_PORT,
                         service_path_value(environment))
    return {"platform": sys.platform, "config": str(config_file), "config_exists": existing is not None,
            "port": port, "unit_dir": str(units), "units": files, "blockers": blockers, "warnings": warnings,
            "systemctl": [["daemon-reload"], ["enable", "--now", f"{UNIT}.socket"]]}


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30, check=check)


def apply_install(plan, run=systemctl):
    """Write the config (only if absent), the two units, then enable the socket. Refuses on any blocker."""
    if plan["blockers"]:
        raise PermissionError("; ".join(plan["blockers"]))
    config_file = Path(plan["config"])
    existing, problem = read_config(config_file)
    if problem:
        raise ValueError(f"源配置 {config_file} 无效：{problem}")
    ensure_no_active_tasks(config_file)
    if existing is not None and (not plan["config_exists"] or existing["port"] != plan["port"]):
        raise ValueError(f"配置 {config_file} 在生成计划之后发生了变化，请重新预演")
    if existing is not None and existing.get("auto_cleanup_enabled") is not False:
        raise PermissionError("现有配置的 auto_cleanup_enabled 不是 false，已拒绝")
    if existing is None and plan["config_exists"]:
        raise ValueError(f"配置 {config_file} 在生成计划之后消失，请重新预演")
    if not plan["config_exists"]:
        config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(new_config(plan["port"]), handle, indent=2)
            handle.write("\n")
    directory = Path(plan["unit_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in plan["units"].items():
        (directory / name).write_text(text)
    for args in plan["systemctl"]:
        run(*args)


def plan_uninstall(environment=None):
    environment = os.environ if environment is None else environment
    units = unit_dir(environment)
    blockers = []
    try:
        active, _ = find_active_tasks(config_path(environment))
        if active:
            blockers.append(f"有 {len(active)} 个活跃任务，卸载会中断面板服务，已拒绝")
    except TaskCheckError as error:
        blockers.append(str(error))
    present = [n for n in (f"{UNIT}.socket", f"{UNIT}.service") if (units / n).exists()]
    if not present:
        blockers.append("没有找到 crewpane 的服务单元")
    return {"unit_dir": str(units), "config": str(config_path(environment)), "remove": present, "blockers": blockers,
            "systemctl": [["disable", "--now", f"{UNIT}.socket"], ["stop", f"{UNIT}.service"]]}


def apply_uninstall(plan, run=systemctl):
    if plan["blockers"]:
        raise PermissionError("; ".join(plan["blockers"]))
    ensure_no_active_tasks(plan["config"])
    run(*plan["systemctl"][0], check=False)
    run(*plan["systemctl"][1], check=False)
    for name in plan["remove"]:
        (Path(plan["unit_dir"]) / name).unlink(missing_ok=True)
    run("daemon-reload", check=False)  # config, registry, history and settings are left untouched
