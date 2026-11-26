"""Command line: python3 -m coder_agents <command>. Runs from a source checkout, no pip install needed."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import ROOT, SCRIPTS, __version__, legacy, missing_resources, service, panel_lifecycle

LEGACY_SETTINGS = Path.home() / ".codex/skills/kiro-coding/settings.json"  # the old install's location, read only on migrate
SETTINGS_KEYS = {"proxy_defaults", "temp"}
PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
EXAMPLE = ROOT / "examples/settings.example.json"


def settings_file():
    sys.path.insert(0, str(SCRIPTS))
    import run_kiro
    return run_kiro.settings_path()


def validate_settings(data):
    if not isinstance(data, dict) or set(data) - SETTINGS_KEYS:
        raise ValueError("settings 只允许 proxy_defaults 和 temp 两个顶层字段")
    proxies = data.get("proxy_defaults", {})
    if not isinstance(proxies, dict) or any(k not in PROXY_KEYS or not isinstance(v, str) for k, v in proxies.items()):
        raise ValueError("proxy_defaults 只允许字符串值的 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY")
    if not isinstance(data.get("temp", {}), dict):
        raise ValueError("temp 必须是对象")
    return data


def show_plan(plan, lines):
    for line in lines:
        print(line)
    for text in plan.get("warnings", []):
        print("警告：" + text)
    for text in plan["blockers"]:
        print("拒绝：" + text)


def cmd_status(args):
    config_file = service.config_path()
    config, problem = service.read_config(config_file)
    settings = settings_file()
    active, stale = service.find_active_tasks(config_file)
    print(f"Coder-Agents {__version__}（源码目录 {ROOT}）")
    print(f"平台: {sys.platform}（前台面板支持 macOS / Linux；systemd 服务仅 Linux）  Python: {sys.version.split()[0]}")
    for binary in ("kiro-cli", "claude"):
        print(f"{binary}: {shutil.which(binary) or '未在 PATH 中找到'}")
    print(f"设置文件: {settings}（{'存在' if settings.is_file() else '不存在，使用默认值'}）")
    print(f"面板配置: {config_file}（{'存在' if config else '未找到' if not problem else problem}）")
    if config:
        port = config["port"]
        print(f"固定入口: http://127.0.0.1:{port}/ （{'端口已被监听' if service.port_in_use(port) else '当前没有进程监听'}）")
        print(f"auto_cleanup_enabled: {config.get('auto_cleanup_enabled')!r}")
    print(f"活跃任务: {len(active)}" + (f"（另有 {len(stale)} 个陈旧记录）" if stale else ""))
    units = service.unit_dir()
    for name in (f"{service.UNIT}.socket", f"{service.UNIT}.service", f"{service.LEGACY_UNIT}.socket"):
        print(f"单元 {name}: {'已安装' if (units / name).exists() else '未安装'}")
    print("Codex 集成: 可选，见 docs/codex-integration.md；没有 Codex 也可在浏览器使用。")
    return 0


def cmd_serve(args):
    """Foreground or explicitly requested background server; no system service installed."""
    if args.port is not None and not 1024 <= args.port <= 65535:
        print("--port 必须在 1024-65535 之间", file=sys.stderr)
        return 2
    config_file = service.config_path()
    config, problem = service.read_config(config_file)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if config is None:
        config = service.new_config(args.port or service.DEFAULT_PORT)
        try:
            config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(config, handle, indent=2)
                handle.write("\n")
        except OSError as error:
            print(f"无法创建配置 {config_file}：{error}", file=sys.stderr)
            return 1
        print(f"已创建配置 {config_file}")
    elif args.port and args.port != config["port"]:
        print(f"现有配置端口为 {config['port']}，不会改写；请去掉 --port", file=sys.stderr)
        return 1
    config_file = config_file.resolve()
    if args.command == "start":
        try:
            print(panel_lifecycle.start_panel(config_file, config))
            return 0
        except (OSError, ValueError) as error:
            print("启动失败：" + str(error), file=sys.stderr)
            return 1
    if service.port_in_use(config["port"]):
        print(f"端口 127.0.0.1:{config['port']} 已有服务在监听，不再启动第二个服务。", file=sys.stderr)
        return 1
    command = [sys.executable, str(SCRIPTS / "watch_kiro.py"), "--serve", "--config", str(config_file)]
    return subprocess.call(command)


def passthrough(script, args):
    return subprocess.call([sys.executable, str(SCRIPTS / script), *args])


def cmd_settings(args):
    try:
        return _settings(args)
    except OSError as error:
        print(f"设置操作失败（权限或文件系统错误）：{error}", file=sys.stderr)
        return 1


def _settings(args):
    target = settings_file()
    if args.action == "path":
        print(target)
        return 0
    if args.action == "init":
        source, text = EXAMPLE, f"将复制 {EXAMPLE} 到 {target}"
    else:
        source = Path(args.source).expanduser() if args.source else LEGACY_SETTINGS
        text = f"将校验并复制旧设置 {source} 到 {target}（旧文件保持不动）"
    try:
        data = validate_settings(json.loads(source.read_text()))
    except (OSError, ValueError) as error:
        print(f"无法使用 {source}: {error}", file=sys.stderr)
        return 1
    if target.exists():
        print(f"拒绝：{target} 已存在，不会覆盖", file=sys.stderr)
        return 3
    print(text)
    if not args.apply:
        print("这是预演，未写入任何文件；加 --apply 才会执行。")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"已写入 {target}")
    return 0


def cmd_service(args):
    if args.action == "status":
        units = f"{service.UNIT}.socket {service.UNIT}.service {service.LEGACY_UNIT}.socket {service.LEGACY_UNIT}.service"
        try:
            done = service.systemctl("show", *units.split(), "--property=Id,ActiveState,SubState", check=False)
            print(done.stdout.strip() or done.stderr.strip() or "systemctl 没有输出")
        except (OSError, subprocess.SubprocessError) as error:
            print(f"无法访问用户 systemd：{error}")
        return 0
    install = args.action == "install"
    plan = service.plan_install(args.port) if install else service.plan_uninstall()
    if install:
        show_plan(plan, [f"配置: {plan['config']}（{'沿用现有，不修改' if plan['config_exists'] else '不存在，将创建，auto_cleanup_enabled=false'}）",
                         f"端口: 127.0.0.1:{plan['port']}", f"单元目录: {plan['unit_dir']}"])
        for name, text in plan["units"].items():
            print(f"--- {name}\n{text}")
    else:
        show_plan(plan, [f"将删除单元: {', '.join(plan['remove']) or '无'}（配置、注册表、历史保持不动）"])
    print("systemctl --user 步骤: " + " ; ".join(" ".join(a) for a in plan["systemctl"]))
    if plan["blockers"]:
        return 3
    if not args.apply:
        print("这是预演，系统未做任何改动；确认后加 --apply 才会执行。")
        return 0
    try:
        (service.apply_install if install else service.apply_uninstall)(plan)
    except PermissionError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 3
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"执行失败：{error}", file=sys.stderr)
        return 1
    print("已完成。")
    return 0


def cmd_legacy(args):
    link = args.action == "link"
    plan = legacy.plan_link(args.legacy, args.config) if link else legacy.plan_unlink(args.legacy, args.backup, args.config)
    print(f"旧安装: {plan['legacy']}\n新仓库: {ROOT}")
    if link:
        for name, state in plan["steps"].items():
            print(f"  {name}: " + {"ok": "已指向本仓库（无需处理）", "todo": "将改名进备份并创建符号链接",
                                  "refuse": "拒绝"}[state])
        print("备份目录将创建在旧安装内（唯一名称，不删除任何文件）。SKILL.md、settings.json、配置、注册表、历史均不动。")
    else:
        print(f"备份: {plan['backup']}\n将恢复: {', '.join(plan['moves']) or '无'}")
    for text in plan["blockers"]:
        print("拒绝：" + text)
    if plan["blockers"]:
        return 3
    if link and plan["already_linked"]:
        print("已经迁移，无需操作。")
        return 0
    if not args.apply:
        print("这是预演，未做任何改动；确认后加 --apply 才会执行。")
        return 0
    try:
        backup = legacy.apply_link(plan) if link else legacy.apply_unlink(plan)
    except PermissionError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 3
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"已完成。备份目录：{backup}")
    return 0


def cmd_selfcheck(args):
    """Verify the runtime files this install needs are present (also after pip install)."""
    missing = missing_resources()
    relay = SCRIPTS / "kiro_acp_relay.py"
    problems = [f"缺少 {name}" for name in missing]
    if relay.is_file() and not os.access(relay, os.X_OK):
        problems.append(f"{relay} 不可执行（runner 以它作为 KIRO_KAS_NODE_PATH）")
    if not problems:
        for script in ("run_kiro.py", "run_claude.py", "run_agent.py"):
            done = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True,
                                  timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            if done.returncode != 0:
                problems.append(script + " --help 失败：" + done.stderr.strip()[-300:])
    for problem in problems:
        print("失败：" + problem, file=sys.stderr)
    if not problems:
        print(f"运行资源完整（{ROOT}）")
    return 1 if problems else 0


def cmd_roles(args):
    sys.path.insert(0, str(SCRIPTS))
    from agent_roles import RoleStore
    try:
        store = RoleStore()
        if args.action == "show":
            data = store.get(args.id)
        else:
            data = store.snapshot()
            if not args.all:
                data["roles"] = [r for r in data["roles"] if r["enabled"]]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as error:
        print("读取角色失败：" + str(error), file=sys.stderr)
        return 1


def cmd_tasks(args):
    sys.path.insert(0, str(SCRIPTS))
    import agent_tasks
    try:
        if args.action == "list":
            data = agent_tasks.list_tasks()
        elif args.action == "stop":
            data = agent_tasks.stop(args.run_dir)
        else:
            data = agent_tasks.read(args.run_dir)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print("任务操作失败：" + str(error), file=sys.stderr)
        return 1


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m coder_agents",
                                     description="Coder-Agents：本机 Kiro / Claude Code 编码面板（macOS / Linux；systemd 仅 Linux）。")
    parser.add_argument("--version", action="version", version=f"coder-agents {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")
    sub.add_parser("status", help="只读：显示路径、配置、端口、活跃任务与服务单元状态")
    serve = sub.add_parser("serve", help="前台启动面板（无需 systemd），用浏览器打开 http://127.0.0.1:<端口>/")
    serve.add_argument("--port", type=int, help="仅在首次创建配置时使用，默认 %d" % service.DEFAULT_PORT)
    start = sub.add_parser("start", help="后台启动面板，已运行时复用，确认就绪后返回地址")
    start.add_argument("--port", type=int, help="仅在首次创建配置时使用，默认 %d" % service.DEFAULT_PORT)
    sub.add_parser("run", help="派发任务：--role ID --cwd DIR；诊断：--provider kiro|claude --check",
                   add_help=False)
    tasks = sub.add_parser("tasks", help="查询任务、读取结果或停止单个任务")
    task_sub = tasks.add_subparsers(dest="action", required=True)
    task_sub.add_parser("list", help="列出所有已登记任务")
    for action in ("show", "stop"):
        task = task_sub.add_parser(action)
        task.add_argument("--run-dir", required=True, type=Path, help="目标任务的运行记录目录")
    roles = sub.add_parser("roles", help="读取最新全局角色配置（JSON），不会启动 CLI")
    role_sub = roles.add_subparsers(dest="action", required=True)
    role_list = role_sub.add_parser("list", help="列出启用的角色")
    role_list.add_argument("--all", action="store_true", help="包含停用角色")
    role_show = role_sub.add_parser("show", help="读取单个角色")
    role_show.add_argument("id")
    sub.add_parser("register", help="向运行中的面板注册项目，参数传给 scripts/watch_kiro.py（--cwd DIR [--root DIR]）",
                   add_help=False)
    sub.add_parser("stop", help="关闭面板 Python 服务（不影响任务与历史）")
    st = sub.add_parser("settings", help="用户设置（代理等）：path / init / migrate-legacy")
    st.add_argument("action", choices=["path", "init", "migrate-legacy"])
    st.add_argument("--from", dest="source", help="migrate-legacy 的旧设置文件，默认旧 kiro-coding 安装位置")
    st.add_argument("--apply", action="store_true", help="真正写入；默认只预演")
    sub.add_parser("selfcheck", help="检查运行资源（scripts/assets/vendor）是否完整，安装后可用它验证")
    lg = sub.add_parser("legacy", help="把旧 kiro-coding 安装的 scripts/assets 指向本仓库：link / unlink（默认预演）")
    lg.add_argument("action", choices=["link", "unlink"])
    lg.add_argument("--legacy", type=Path, help="旧安装目录，默认 ~/.codex/skills/kiro-coding")
    lg.add_argument("--backup", type=Path, help="unlink 使用的备份目录（有多个备份时必须指定）")
    lg.add_argument("--config", type=Path, help="面板配置（用于检查活跃任务），默认与 service 相同")
    lg.add_argument("--apply", action="store_true", help="真正执行；默认只预演")
    sv = sub.add_parser("service", help="systemd 用户服务：status / install / uninstall（默认预演）")
    sv.add_argument("action", choices=["status", "install", "uninstall"])
    sv.add_argument("--port", type=int, help="仅用于首次创建配置")
    sv.add_argument("--apply", action="store_true", help="真正修改系统；默认只预演")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["run"]:
        # Replace this process so SIGTERM reaches the runner, which owns child cleanup.
        os.execv(sys.executable, [sys.executable, str(SCRIPTS / "run_agent.py"), *argv[1:]])
    if argv[:1] == ["register"]:
        return passthrough("watch_kiro.py", argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "stop":
        return passthrough("watch_kiro.py", ["--stop", "--config", str(service.config_path())])
    handler = {"status": cmd_status, "serve": cmd_serve, "start": cmd_serve, "settings": cmd_settings, "service": cmd_service,
               "legacy": cmd_legacy, "selfcheck": cmd_selfcheck, "roles": cmd_roles, "tasks": cmd_tasks}.get(args.command)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
