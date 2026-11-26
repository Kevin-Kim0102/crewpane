"""Start the local panel on demand without keeping a terminal attached."""
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from . import SCRIPTS, service

LOCAL_HTTP = build_opener(ProxyHandler({}))


def panel_ready(config):
    """Verify our cookie-authenticated panel, not just an arbitrary listening port."""
    port = config["port"]
    cookie = hmac.new(config["token"].encode(), b"kiro-panel-browser-session-v1", hashlib.sha256).hexdigest()
    request = Request(f"http://127.0.0.1:{port}/api/health", headers={"Cookie": f"kiro_panel_{port}={cookie}"})
    try:
        with LOCAL_HTTP.open(request, timeout=0.5) as response:
            data = json.loads(response.read(4096))
        return isinstance(data, dict) and data.get("service") == "coder-agents" and data.get("ready") is True
    except (OSError, ValueError, URLError):
        return False


def start_panel(config_path, config, timeout=10):
    """Reuse a verified instance or start one detached and wait for readiness."""
    config_path = Path(config_path)
    url = f"http://127.0.0.1:{config['port']}/"
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(config_path.parent / "panel-start.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if panel_ready(config):
            return url
        if service.port_in_use(config["port"]):
            raise ValueError("面板端口已被占用，但无法确认是当前 crewpane 服务；不会启动第二个实例。")
        log_path = config_path.parent / "panel.log"
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as log:
            os.fchmod(log.fileno(), 0o600)
            process = subprocess.Popen([sys.executable, str(SCRIPTS / "watch_kiro.py"), "--serve", "--config", str(config_path)],
                                       cwd=str(SCRIPTS.parent), stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError(f"面板启动失败，请查看日志：{log_path}")
            if panel_ready(config):
                return url
            time.sleep(0.1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise ValueError(f"面板未在等待时间内就绪，请查看日志：{log_path}")
