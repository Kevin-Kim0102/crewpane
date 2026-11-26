"""Standalone CLI, settings and service-installer checks. Nothing here touches the real HOME, systemd or the fixed port."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import tmpdirs
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from coder_agents import service, cli  # noqa: E402
import run_kiro  # noqa: E402
import watch_kiro  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.TemporaryDirectory(prefix="coder-agents-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "XDG_CONFIG_HOME": str(self.home / "cfg"), "CODER_AGENTS_CONFIG": str(self.home / "state/config.json")}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = self.home / "state/config.json"

    def write_config(self, **extra):
        data = {"port": free_port(), "token": "T" * 32, "idle_timeout": 120, "auto_cleanup_enabled": False, **extra}
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(data))
        return data

    def run_cli(self, *args, extra_env=None, module="crewpane"):
        env = {**self.env, **(extra_env or {}), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=60)

    def make_run(self, status, boot_id=None):
        root = self.home / "proj/work/kiro-runs"
        run = root / "run1"
        run.mkdir(parents=True)
        state = {"status": status}
        if boot_id:
            state["boot_id"] = boot_id
        (run / "state.json").write_text(json.dumps(state))
        (self.config.parent / "projects.json").write_text(json.dumps({"projects": [{"cwd": str(root.parent), "root": str(root)}]}))
        return run


class CliTests(Env):
    def test_help_status_and_module_entry(self):
        for args in (["--help"], ["status"], ["--version"], ["service", "--help"]):
            done = self.run_cli(*args)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Linux", self.run_cli("--help").stdout)
        self.assertIn("未找到", self.run_cli("status").stdout)
        legacy = self.run_cli("--version", module="coder_agents")
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(legacy.stdout, self.run_cli("--version").stdout)

    def test_status_hides_token(self):
        self.write_config()
        self.assertNotIn("T" * 32, self.run_cli("status").stdout)

    def test_run_passthrough_reaches_runner(self):
        done = self.run_cli("run", "--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("--cwd", done.stdout)

    def test_no_personal_paths_or_proxy_in_sources(self):
        banned = ("/home/jty", "7897", "kiro-coding/scripts")
        paths = [p for d in ("scripts", "coder_agents", "tests", "examples", "assets") for p in (ROOT / d).rglob("*")
                 if p.is_file() and "vendor" not in p.parts and p.suffix in {".py", ".cjs", ".html", ".json", ".md"}]
        paths += [ROOT / "package.json"]
        for path in paths:
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(errors="ignore")
            for word in banned:
                self.assertNotIn(word, text, f"{word} in {path}")


class KiroRuntimeTests(Env):
    def test_macos_native_runtime_and_server(self):
        import kiro_acp
        data = self.home / "Library/Application Support/kiro-cli"
        node = data / "node"
        server = data / "kas/3/node_modules/@kiro/agent/dist/server/acp-server.js"
        server.parent.mkdir(parents=True)
        server.touch()
        node.touch()
        node.chmod(0o700)
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(run_kiro.locate_node(self.env), str(node))
            self.assertEqual(kiro_acp.server_command(self.env)[0], str(node))
            self.assertEqual(kiro_acp.find_server_js(self.env), str(server))

    def test_xdg_runtime_override_is_preserved(self):
        data = self.home / "custom"
        node = data / "kiro-cli/node"
        node.parent.mkdir(parents=True)
        node.touch()
        node.chmod(0o700)
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(run_kiro.locate_node({**self.env, "XDG_DATA_HOME": str(data)}), str(node))


class SettingsTests(Env):
    def test_settings_location_and_explicit_env_priority(self):
        self.assertEqual(run_kiro.settings_path(), self.home / "cfg/coder-agents/settings.json")
        explicit = self.home / "custom.json"
        explicit.write_text(json.dumps({"proxy_defaults": {"HTTP_PROXY": "http://proxy.invalid:1"}}))
        with mock.patch.dict(os.environ, {"CODER_AGENTS_SETTINGS": str(explicit)}):
            self.assertEqual(run_kiro.kiro_environment()["HTTP_PROXY"], "http://proxy.invalid:1")
            with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://env.invalid:2"}):
                self.assertEqual(run_kiro.kiro_environment()["HTTP_PROXY"], "http://env.invalid:2")

    def test_no_settings_means_no_proxy_defaults(self):
        self.assertNotIn("HTTPS_PROXY", run_kiro.kiro_environment())

    def test_repository_settings_json_is_ignored(self):
        self.assertNotIn("settings.json", str(run_kiro.settings_path()).replace("coder-agents/settings.json", ""))

    def test_init_and_migrate_dry_run_then_apply(self):
        target = self.home / "cfg/coder-agents/settings.json"
        done = self.run_cli("settings", "init")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(target.exists())
        legacy = self.home / "old.json"
        legacy.write_text(json.dumps({"proxy_defaults": {"HTTP_PROXY": "http://p.invalid:3"}}))
        done = self.run_cli("settings", "migrate-legacy", "--from", str(legacy), "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(target.read_text())["proxy_defaults"]["HTTP_PROXY"], "http://p.invalid:3")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertTrue(legacy.exists())
        self.assertEqual(self.run_cli("settings", "migrate-legacy", "--from", str(legacy), "--apply").returncode, 3)

    def test_migrate_rejects_unknown_fields(self):
        legacy = self.home / "bad.json"
        legacy.write_text(json.dumps({"proxy_defaults": {"EVIL": "x"}}))
        self.assertEqual(self.run_cli("settings", "migrate-legacy", "--from", str(legacy), "--apply").returncode, 1)
        self.assertFalse((self.home / "cfg/coder-agents/settings.json").exists())
        with self.assertRaises(ValueError):
            cli.validate_settings({"token": "x"})


class ServiceTests(Env):
    def setUp(self):
        super().setUp()
        # Exercise Linux installer planning without invoking systemd on the host.
        platform = mock.patch.object(service.sys, "platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)
        boot = mock.patch.object(service, "current_boot_id", return_value="test-boot")
        boot.start()
        self.addCleanup(boot.stop)

    def plan(self, port=None, listening=lambda p: False):
        return service.plan_install(port, python="/usr/bin/python3", root=ROOT, listening=listening)

    def test_fresh_plan_is_clean_loopback_and_absolute(self):
        port = free_port()
        plan = self.plan(port)
        self.assertEqual(plan["blockers"], [])
        socket_unit = plan["units"]["coder-agents.socket"]
        self.assertIn(f"ListenStream=127.0.0.1:{port}", socket_unit)
        service_unit = plan["units"]["coder-agents.service"]
        self.assertIn(str(ROOT / "scripts/watch_kiro.py"), service_unit)
        self.assertIn(str(self.config), service_unit)
        self.assertEqual(set(plan["units"]), {"coder-agents.socket", "coder-agents.service"})  # no cleanup timer
        self.assertNotIn("cleanup", service_unit)

    def test_dry_run_changes_nothing(self):
        done = self.run_cli("service", "install", "--port", str(free_port()))
        # The subprocess uses the real host platform: macOS must refuse systemd installation.
        self.assertIn(done.returncode, (0, 3), done.stdout + done.stderr)
        self.assertTrue("预演" in done.stdout or "仅支持 Linux" in done.stdout)
        self.assertEqual(list(self.home.rglob("*.socket")) + list(self.home.rglob("config.json")), [])

    def test_apply_writes_config_and_units_without_stopping_anything(self):
        calls = []
        plan = self.plan(free_port())
        service.apply_install(plan, run=lambda *a, **k: calls.append(a))
        config = json.loads(self.config.read_text())
        self.assertIs(config["auto_cleanup_enabled"], False)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.home / "cfg/systemd/user/coder-agents.socket").is_file())
        self.assertEqual(calls, [("daemon-reload",), ("enable", "--now", "coder-agents.socket")])
        self.assertFalse(any(a[0] in {"stop", "restart", "disable"} for a in calls))

    def test_existing_config_and_registry_are_preserved(self):
        data = self.write_config()
        registry = self.config.parent / "projects.json"
        registry.write_text('{"projects": []}')
        before = (self.config.read_bytes(), registry.read_bytes())
        plan = self.plan()
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["port"], data["port"])
        service.apply_install(plan, run=lambda *a, **k: None)
        self.assertEqual((self.config.read_bytes(), registry.read_bytes()), before)

    def test_refuses_when_cleanup_flag_is_not_false(self):
        for extra in ({"auto_cleanup_enabled": True},):
            self.write_config(**extra)
            plan = self.plan()
            self.assertTrue(any("auto_cleanup_enabled" in b for b in plan["blockers"]))
        data = self.write_config()
        del data["auto_cleanup_enabled"]
        self.config.write_text(json.dumps(data))
        self.assertTrue(any("auto_cleanup_enabled" in b for b in self.plan()["blockers"]))

    def test_refuses_with_active_task_and_applies_nothing(self):
        self.write_config()
        run = self.make_run("running")
        plan = self.plan()
        self.assertTrue(any("活跃任务" in b and str(run) in b for b in plan["blockers"]))
        calls = []
        with self.assertRaises(PermissionError):
            service.apply_install(plan, run=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])
        self.assertFalse((self.home / "cfg/systemd").exists())
        self.assertEqual(self.run_cli("service", "install").returncode, 3)
        self.assertEqual(self.run_cli("service", "install", "--apply").returncode, 3)
        self.assertFalse((self.home / "cfg/systemd").exists())

    def test_stale_boot_record_does_not_block_but_current_boot_does(self):
        self.write_config()
        self.make_run("running", boot_id="some-earlier-boot")
        plan = self.plan()
        self.assertEqual(plan["blockers"], [])
        self.assertTrue(plan["warnings"])
        (self.home / "proj/work/kiro-runs/run1/state.json").write_text(
            json.dumps({"status": "checking", "boot_id": service.current_boot_id()}))
        self.assertTrue(self.plan()["blockers"])

    def test_refuses_legacy_unit_and_busy_port(self):
        units = self.home / "cfg/systemd/user"
        units.mkdir(parents=True)
        (units / "codex-kiro-panel.socket").write_text("[Socket]\n")
        self.assertTrue(any("旧版服务单元" in b for b in self.plan()["blockers"]))
        (units / "codex-kiro-panel.socket").unlink()
        self.assertTrue(any("已被占用" in b for b in self.plan(listening=lambda p: True)["blockers"]))

    def test_uninstall_refuses_active_task(self):
        self.write_config()
        self.make_run("running")
        units = self.home / "cfg/systemd/user"
        units.mkdir(parents=True)
        (units / "coder-agents.socket").write_text("x")
        plan = service.plan_uninstall()
        self.assertTrue(plan["blockers"])
        with self.assertRaises(PermissionError):
            service.apply_uninstall(plan, run=lambda *a, **k: self.fail("must not run"))

    def test_unit_quoting_escapes_percent_and_quotes(self):
        text = service.render_units('/usr/bin/py"x', Path("/a b/100%/c"), Path("/c d/config.json"), 4000, "/x")
        self.assertIn('"/a b/100%%/c/scripts/watch_kiro.py"', text["coder-agents.service"])
        self.assertIn('\\"', text["coder-agents.service"])


class EmptyPanelTests(Env):
    def test_panel_starts_without_any_registered_project(self):
        panel = watch_kiro.Panel({"port": free_port(), "token": "T" * 32}, self.config, codex_state=self.home / "missing.json",
                                 daily_base=self.home / "daily", models=mock.Mock(), usage=None)
        view = panel.view()
        self.assertEqual(view["default"], {"group": "", "source": ""})
        self.assertTrue(any(g["kind"] == "other" for g in view["groups"]))  # 其他对话 is available
        self.assertEqual(view["catalog"]["status"], "missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
