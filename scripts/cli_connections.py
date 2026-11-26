"""Explicit local CLI connections. Reading the store never launches a process."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from agent_providers import PROVIDERS, provider


def store_path(environment=None):
    environment = os.environ if environment is None else environment
    config = Path(environment.get("CODER_AGENTS_CONFIG") or Path.home() / ".local/state/kiro-panel/config.json").expanduser()
    return config.parent / "connections.json"


def executable_path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("请输入 CLI 可执行文件的绝对路径")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("CLI 路径必须是绝对路径，不能包含命令参数")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("CLI 文件不存在或不可执行，请检查路径后重新连接")
    # Do not resolve symlinks: an updater can replace the target behind this stable path.
    return str(path)


def launch_login(binary, name):
    """Run the official interactive login in a visible terminal; never handle credentials."""
    command = [binary, 'auth', 'login'] if name == 'claude' else [binary, 'login']
    if sys.platform == 'darwin':
        script = 'tell application "Terminal"\nactivate\ndo script ' + json.dumps(shlex.join(command)) + '\nend tell'
        subprocess.run(['osascript', '-e', script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=True)
    else:
        terminal = next((shutil.which(item) for item in ('x-terminal-emulator', 'gnome-terminal', 'konsole', 'xterm')
                         if shutil.which(item)), None)
        if not terminal:
            raise ValueError('无法打开登录终端，请在终端执行 ' + shlex.join(command) + ' 后重新连接')
        subprocess.Popen([terminal, '--' if Path(terminal).name == 'gnome-terminal' else '-e', *command],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def logged_in(binary, name, timeout):
    command = [binary, 'auth', 'status', '--json'] if name == 'claude' else [binary, 'whoami']
    auth = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if auth.returncode:
        return False
    if name != 'claude':
        return True
    try:
        return json.loads(auth.stdout).get('loggedIn') is True
    except (ValueError, AttributeError):
        return False


class Connections:
    def __init__(self, path=None):
        self.path = Path(path) if path else store_path()
        self.lock = threading.RLock()

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "default_provider": None, "providers": {}}
        except (OSError, ValueError):
            raise ValueError("CLI 连接配置无法读取，请检查 connections.json")
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
            raise ValueError("CLI 连接配置格式无效")
        return data

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".connections-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def executable(self, name):
        if name is None:
            raise ValueError("尚未连接 CLI，请先在设置中点击连接")
        provider(name)
        record = self.read()["providers"].get(name) or {}
        if not record.get("enabled"):
            raise ValueError("尚未连接 " + PROVIDERS[name]["name"] + "，请在设置中点击连接")
        if record.get("paused"):
            raise ValueError(PROVIDERS[name]["name"] + " 已暂停，请在设置中启动")
        return executable_path(record.get("path"))

    def snapshot(self):
        with self.lock:
            data = self.read()
            rows = []
            for name, meta in PROVIDERS.items():
                record = data["providers"].get(name) or {}
                available, error = False, ""
                if record.get("enabled"):
                    try:
                        executable_path(record.get("path"))
                        available = True
                    except ValueError as failure:
                        error = str(failure)
                paused = bool(record.get("paused")) and bool(record.get("enabled"))
                rows.append({"id": name, "name": meta["name"], "available": available and not paused,
                             "paused": paused,
                             "connected": bool(record.get("enabled")), "path": record.get("path", ""),
                             "defaults": self.defaults(name),
                             "defaults_configured": record.get("defaults") is not None,
                             "version": record.get("version", ""), "checked_at": record.get("checked_at"),
                             "status": "paused" if available and paused else "connected" if available else "invalid" if record.get("enabled") else "disconnected",
                             "error": error, "login_command": "claude auth login" if name == "claude" else "kiro-cli login"})
            return {"providers": rows, "default_provider": data.get("default_provider")}

    def connect(self, name, path="", timeout=10, allow_login=False):
        meta = provider(name)
        try:
            if not isinstance(path, str):
                raise ValueError("CLI 路径必须是字符串")
            candidate = path.strip() or shutil.which(meta["binary"])
            if not candidate:
                raise ValueError("没有找到 " + meta["binary"] + "；请先安装 CLI 或填写绝对路径")
            binary = executable_path(candidate)
            version = subprocess.run([binary, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, timeout=timeout)
            text = version.stdout.strip()
            identity = r"Claude Code" if name == "claude" else r"\bkiro(?:-cli)?\b"
            if version.returncode or not re.search(identity, text, re.I):
                raise ValueError("该文件不是可识别的 " + meta["name"] + "，请检查路径")
            if name == "claude":
                match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
                if not match or tuple(map(int, match.groups())) < (2, 1, 78):
                    raise ValueError("Claude Code 需要 2.1.78 或更高版本，请先升级")
            authenticated = logged_in(binary, name, timeout)
            if not authenticated and allow_login:
                launch_login(binary, name)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if logged_in(binary, name, timeout):
                        authenticated = True
                        break
                    time.sleep(2)
                if not authenticated:
                    raise ValueError('登录尚未完成（等待超时）；请完成终端/浏览器授权后重新连接')
            if not authenticated:
                raise ValueError("CLI 尚未登录或登录状态无法确认，请在终端执行 " + (
                    "claude auth login" if name == "claude" else "kiro-cli login") + " 后重新连接")
            with self.lock:
                data = self.read()
                old = data["providers"].get(name) or {}
                data["providers"][name] = {**old, "enabled": True, "paused": False, "path": binary, "version": text[:120],
                                           "checked_at": datetime.now(timezone.utc).isoformat()}
                if not data.get("default_provider"):
                    data["default_provider"] = name
                self.save(data)
            return {"ok": True, "message": "连接成功：已检查版本和登录状态，未调用模型", **self.snapshot()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "连接检查超时，请在终端检查 CLI 后重试"}
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            # Never expose raw subprocess stderr/auth output; it can contain account data.
            message = str(error) if isinstance(error, ValueError) else "无法执行 CLI 或保存连接配置，请检查路径和文件权限"
            return {"ok": False, "message": message}

    def disconnect(self, name):
        provider(name)
        with self.lock:
            data = self.read()
            record = data["providers"].get(name) or {}
            data["providers"][name] = {**record, "enabled": False}
            if data.get("default_provider") == name:
                data["default_provider"] = next((key for key, value in data["providers"].items()
                                                  if value.get("enabled")), None)
            self.save(data)
        return self.snapshot()

    def set_default(self, name):
        with self.lock:
            self.executable(name)
            data = self.read()
            data["default_provider"] = name
            self.save(data)
        return self.snapshot()

    def defaults(self, name="claude", *, require_configured=False):
        meta = provider(name)
        record = self.read()["providers"].get(name) or {}
        value = record.get("defaults")
        if value is None:
            if require_configured:
                raise ValueError(f"请先在设置中保存 {meta['name']} 的默认模型，或为角色单独选择模型")
            if name == "claude":
                return {"model": "sonnet", "effort": "medium"}
            from kiro_catalog import DEFAULT_MODEL, DEFAULT_EFFORT
            return {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT}
        if not isinstance(value, dict) or set(value) != {"model", "effort"}:
            raise ValueError("CLI 默认模型配置无效，请在设置中重新保存")
        return dict(value)

    def defaults_configured(self, name):
        return (self.read()["providers"].get(name) or {}).get("defaults") is not None

    def set_defaults(self, name, model, effort):
        provider(name)
        if name == "claude":
            from agent_providers import ClaudeModels
            ClaudeModels().validate(model, effort)
        else:
            from kiro_catalog import MODEL_ID, EFFORT_ID
            if not isinstance(model, str) or not MODEL_ID.fullmatch(model) or (
                    effort is not None and (not isinstance(effort, str) or not EFFORT_ID.fullmatch(effort))):
                raise ValueError("Kiro 默认模型或思考强度无效")
        with self.lock:
            data = self.read()
            record = data["providers"].get(name) or {}
            data["providers"][name] = {**record, "defaults": {"model": model, "effort": effort}}
            self.save(data)
        return self.snapshot()

    def set_paused(self, name, paused):
        provider(name)
        with self.lock:
            data = self.read()
            record = data["providers"].get(name) or {}
            if not record.get("enabled"):
                raise ValueError("尚未连接 CLI，请先连接")
            if not paused:
                executable_path(record.get("path"))
            data["providers"][name] = {**record, "paused": bool(paused)}
            self.save(data)
        return self.snapshot()


def runner_executable(name, explicit=None):
    """Saved connections are authoritative; legacy CLI-only calls may still use PATH."""
    if explicit:
        return executable_path(explicit)
    store = Connections()
    if name in store.read()["providers"]:
        return store.executable(name)
    found = shutil.which(provider(name)["binary"])
    if not found:
        raise ValueError("找不到 CLI，请在面板设置中连接 " + name)
    return executable_path(found)
