"""Claude integration contract tests. All model output comes from a local fake CLI."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

FAKE = '''#!/usr/bin/env python3
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('2.1.78 (Claude Code)'); sys.exit(0)
if args[:2] == ['auth','status']:
    print(json.dumps({'loggedIn': True})); sys.exit(0)
prompt = sys.stdin.read()
Path(os.environ["CAPTURE"]).write_text(json.dumps({"args":args,"prompt":prompt,"cwd":os.getcwd()}))
mode = os.environ.get("FAKE_MODE", "success")
session = args[args.index("--resume")+1] if "--resume" in args else "12345678-1234-4234-8234-123456789abc"
def emit(value): print(json.dumps(value), flush=True)
emit({"type":"system","subtype":"init","session_id":session,"model":"claude-sonnet-test"})
if mode == "sleep":
    if os.environ.get("FAKE_GRANDCHILD"):
        child_code = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(os.environ['CAPTURE']+'.grandchild').write_text(str(os.getpid())); time.sleep(5); Path(os.environ['CAPTURE']+'.survived').write_text('alive'); time.sleep(60)"
        subprocess.Popen([sys.executable, "-c", child_code])
        while not Path(os.environ["CAPTURE"]+".grandchild").exists(): time.sleep(.01)
    Path(os.environ["CAPTURE"]+".pid").write_text(str(os.getpid()))
    time.sleep(60)
if mode == "malformed": print("not-json", flush=True)
emit({"type":"assistant","message":{"content":[{"type":"text","text":"hello"},{"type":"tool_use","id":"tool-1","name":"Read","input":{"file_path":"a.txt"}}]}})
emit({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tool-1","content":"file contents"}]}})
if mode != "missing":
    emit({"type":"result","subtype":"error_during_execution" if mode == "error" else "success", "is_error":mode == "error", "result":"hello", "session_id":session, "usage":{"input_tokens":12,"output_tokens":3}, "total_cost_usd":0.01,"permission_denials":[{"tool_name":"Bash"}] if mode == "denied" else []})
sys.exit(4 if mode == "exit" else 0)
'''


class ClaudeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.binary = self.base / "claude"
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.capture = self.base / "capture.json"
        self.env = {**os.environ, "PATH": str(self.base) + os.pathsep + os.environ.get("PATH", ""),
                    "CAPTURE": str(self.capture), "PYTHONDONTWRITEBYTECODE": "1",
                    "CODER_AGENTS_CONFIG": str(self.base / "config.json"),
                    "XDG_CONFIG_HOME": str(self.base / "user-config")}

    def run_task(self, mode="success", extra=()):
        directory = self.base / ("run-" + mode)
        backend = ["--provider", "claude"] if "--check" in extra else ["--role", "coder"]
        done = subprocess.run([sys.executable, "-m", "coder_agents", "run", *backend,
                               "--cwd", str(self.base), "--run-dir", str(directory), "--agent-id", "reader",
                               *extra], input="read only: 你好", text=True, capture_output=True,
                              cwd=ROOT, env={**self.env, "FAKE_MODE": mode}, timeout=15)
        self.assertTrue((directory / "state.json").exists(), done.stderr)
        return done, directory, json.loads((directory / "state.json").read_text())


class ClaudeRunnerTests(ClaudeFixture):
    def setUp(self):
        super().setUp()
        from agent_roles import RoleStore

        with mock.patch.dict(os.environ, self.env):
            store = RoleStore()
            role = {**store.get("coder"), "provider": "claude", "model": "sonnet", "effort": "medium"}
            store.mutate({"action": "save", "role": role, "revision": store.snapshot()["revision"]})

    def test_task_and_check_preserve_cli_environment_without_kiro_proxy_defaults(self):
        settings = self.base / "settings.json"
        settings.write_text(json.dumps({"proxy_defaults": {
            "HTTP_PROXY": "http://kiro-proxy.invalid:3128",
            "HTTPS_PROXY": "http://kiro-proxy.invalid:3128",
            "ALL_PROXY": "socks5://kiro-proxy.invalid:3128",
        }}))
        variables = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
        proxy_keys = (*variables, *(key.lower() for key in variables))
        for key in proxy_keys:
            self.env.pop(key, None)
        self.env.update(CODER_AGENTS_SETTINGS=str(settings),
                        ANTHROPIC_BASE_URL="https://claude-gateway.invalid")
        self.binary.write_text(FAKE.replace(
            '"cwd":os.getcwd()', '"cwd":os.getcwd(),"network":'
            f'{{key:os.environ[key] for key in {(*proxy_keys, "ANTHROPIC_BASE_URL")!r} if key in os.environ}}'
        ))

        done, _, _ = self.run_task()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(self.capture.read_text())["network"],
                         {"ANTHROPIC_BASE_URL": "https://claude-gateway.invalid"})

        self.env["https_proxy"] = "http://explicit-proxy.invalid:8080"
        done, _, _ = self.run_task("check", ("--check",))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(self.capture.read_text())["network"], {
            "ANTHROPIC_BASE_URL": "https://claude-gateway.invalid",
            "https_proxy": "http://explicit-proxy.invalid:8080",
        })

    def test_claude_test_scratch_is_managed_and_kept_for_unified_cleanup(self):
        original = self.binary.read_text()
        self.binary.write_text(original.replace('"cwd":os.getcwd()', '"cwd":os.getcwd(),"tmpdir":os.environ.get("TMPDIR")'))
        done, directory, state = self.run_task()
        self.assertEqual(done.returncode, 0, done.stderr)
        expected = self.base / 'work/tmp' / directory.name
        self.assertEqual(state['temp']['path'], str(expected))
        self.assertTrue(expected.is_dir())
        self.assertEqual(json.loads(self.capture.read_text())['tmpdir'], str(expected))

    def test_success_persists_private_logs_and_normalizes_tools(self):
        done, directory, state = self.run_task()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["provider"], "claude")
        self.assertEqual(state["actual_model"], "claude-sonnet-test")
        self.assertEqual(state["session_id"], "12345678-1234-4234-8234-123456789abc")
        self.assertEqual((directory / "result.txt").read_text(), "hello")
        self.assertIn('"type": "assistant"', (directory / "raw-events.jsonl").read_text())
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(e.get("type") == "runFinished" for e in events))
        self.assertIn("tool_call_update", (directory / "events.jsonl").read_text())
        for name in ("state.json", "task.txt", "events.jsonl", "raw-events.jsonl", "stderr.log", "result.txt"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        captured = json.loads(self.capture.read_text())
        self.assertTrue(captured["prompt"].endswith("# 本次任务\nread only: 你好"))
        self.assertEqual(captured["cwd"], str(self.base))
        self.assertIn("dontAsk", captured["args"])
        self.assertNotIn("--dangerously-skip-permissions", captured["args"])

    def test_native_resume_id_and_explicit_tool_policy(self):
        session = "12345678-1234-4234-8234-123456789abd"
        self.env["XDG_CONFIG_HOME"] = str(self.base / "user-config")
        with mock.patch.dict(os.environ, self.env):
            from agent_roles import RoleStore
            store = RoleStore()
            role = {**store.get("coder"), "provider": "claude", "model": "haiku", "effort": None}
            store.mutate({"action":"save", "role":role, "revision":store.snapshot()["revision"]})
            self.run_task("prior")
            recorded = self.base / "run-prior/state.json"
            recorded.write_text(json.dumps({**json.loads(recorded.read_text()), "session_id": session}))
        done, _, state = self.run_task(extra=("--resume-id", session, "--permission-mode", "acceptEdits",
                                             "--allowed-tools", "Read"))
        self.assertEqual(done.returncode, 0, done.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--resume") + 1], session)
        self.assertIn("acceptEdits", args)
        self.assertNotIn("--effort", args)
        self.assertEqual(state["session_id"], session)

    def test_completed_turn_preserves_tool_denials_as_warnings(self):
        from watch_kiro import turn_summary

        done, directory, state = self.run_task("denied")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["permission_denials"], [{"tool_name": "Bash"}])
        summary = turn_summary(state, (directory / "events.jsonl").read_text())
        self.assertEqual(summary["result"], "completed")
        self.assertEqual(summary["permission_denial_count"], 1)

    def test_incomplete_error_malformed_or_nonzero_are_failed(self):
        for mode in ("missing", "error", "malformed", "exit"):
            with self.subTest(mode=mode):
                done, _, state = self.run_task(mode)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(state["status"], "failed")

    def test_timeout_stops_child_and_records_failure(self):
        done, _, state = self.run_task("sleep", ("--timeout", "1.5"))
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(state["status"], "failed")
        pid = int(Path(str(self.capture) + ".pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_existing_directory_is_never_overwritten(self):
        self.run_task()
        before = (self.base / "run-success/state.json").read_bytes()
        done = subprocess.run([sys.executable, "-m", "coder_agents", "run", "--role", "coder",
                               "--cwd", str(self.base), "--run-dir", str(self.base / "run-success")],
                              input="another", text=True, capture_output=True, cwd=ROOT, env=self.env)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.base / "run-success/state.json").read_bytes(), before)

    def test_timeout_kills_descendants_that_ignore_sigterm(self):
        self.env["FAKE_GRANDCHILD"] = "1"
        done, _, state = self.run_task("sleep", ("--timeout", "3"))
        child = int(Path(str(self.capture) + ".grandchild").read_text())
        self.addCleanup(lambda: self.kill_if_alive(child))
        time.sleep(5.1)
        self.assertFalse(Path(str(self.capture) + ".survived").exists(), "a descendant outlived cancellation")

    def test_check_disables_tools(self):
        done, _, _ = self.run_task(extra=("--check",))
        self.assertEqual(done.returncode, 0, done.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--tools") + 1], "")

    def test_cli_sigterm_cancels_runner_and_child(self):
        directory = self.base / "cancelled"
        with tempfile.TemporaryFile(mode="w+t") as task, tempfile.TemporaryFile(mode="w+t") as output:
            task.write("wait")
            task.seek(0)
            process = subprocess.Popen([sys.executable, "-m", "coder_agents", "run", "--role", "coder",
                                        "--cwd", str(self.base), "--run-dir", str(directory)], cwd=ROOT,
                                       env={**self.env, "FAKE_MODE": "sleep"}, stdin=task, stdout=output, stderr=output)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            pid_path = Path(str(self.capture) + ".pid")
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pid_path.exists())
            child = int(pid_path.read_text())
            self.addCleanup(lambda: self.kill_if_alive(child))
            process.terminate()
            process.wait(timeout=5)
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual(state["status"], "cancelled")
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    @staticmethod
    def kill_if_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            "HOME": str(base / "home"),
            "CODER_AGENTS_CONFIG": str(base / "config.json"),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_claude_is_preferred_even_when_old_kiro_binary_exists(self):
        import agent_providers
        with mock.patch.object(agent_providers.shutil, "which", side_effect=lambda name: "/bin/" + name):
            self.assertEqual(agent_providers.default_provider(), "claude")

    def test_provider_defaults_and_catalog_are_not_account_entitlements(self):
        import agent_providers
        with mock.patch.object(agent_providers.shutil, "which", side_effect=lambda name: "/bin/claude" if name == "claude" else None):
            self.assertEqual(agent_providers.default_provider(), "claude")
            catalog = agent_providers.ClaudeModels().snapshot()
        self.assertFalse(catalog["default"]["available"])
        self.assertEqual(catalog["source"], "api-models")
        self.assertEqual(catalog["models"], [])  # non-refresh never invents a directory


class ClaudePanelTests(ClaudeFixture):
    def panel(self):
        import watch_kiro
        config = {"port": 47899, "token": "T" * 32, "default_provider": "claude",
                  "daily_base": str(self.base / "daily"), "auto_cleanup_enabled": False}
        config_path = self.base / "config.json"
        config_path.write_text(json.dumps(config))
        import cli_connections
        self.assertTrue(cli_connections.Connections(self.base / "connections.json").connect("claude", str(self.binary))["ok"])
        with mock.patch.object(watch_kiro.kiro_catalog, "default_catalog", side_effect=AssertionError("Kiro probed")), \
                mock.patch.object(watch_kiro.kiro_usage, "AcpUsageFetcher", side_effect=AssertionError("Kiro usage probed")):
            return watch_kiro.Panel(config, config_path, codex_state=self.base / "missing", start_wait=2)

    def test_claude_default_never_initializes_kiro_services(self):
        panel = self.panel()
        self.assertEqual(panel.models_view(provider="claude")["provider"], "claude")
        with mock.patch.object(panel.claude_usage, 'snapshot', return_value={'status':'idle'}) as usage:
            view = panel.view()
        usage.assert_called_once_with(cwd=None)
        self.assertEqual(view["viewer"]["default_provider"], "claude")
        self.assertEqual(view["viewer"]["account"]["status"], "idle")

    def test_panel_send_resume_and_immutable_provider(self):
        import watch_kiro
        with mock.patch.dict(os.environ, self.env):
            panel = self.panel()
            status, created = panel.send({"text": "hello", "group": watch_kiro.OTHER_GROUP, "provider": "claude"})
            self.assertIn(status, (201, 202))
            source = panel.source_by_id(created["source"])
            deadline = time.monotonic() + 5
            while any(r["status"] in watch_kiro.ACTIVE_STATUSES for r in source.list_runs()) and time.monotonic() < deadline:
                time.sleep(.05)
            agent = watch_kiro.group_agents(source.list_runs())[0]
            self.assertEqual(agent["provider"], "claude")
            payload = {"text": "continue", "group": watch_kiro.OTHER_GROUP,
                       "source": source.id, "agent": agent["id"], "provider": "kiro"}
            with self.assertRaisesRegex(ValueError, "后端"):
                panel.send(payload)
            payload["provider"] = "claude"
            status, resumed = panel.send(payload)
            self.assertIn(status, (201, 202))
            deadline = time.monotonic() + 5
            while any(r["status"] in watch_kiro.ACTIVE_STATUSES for r in source.list_runs()) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn("--resume", json.loads(self.capture.read_text())["args"])
            view = panel.view(watch_kiro.OTHER_GROUP, source.id, agent["id"])
            self.assertEqual(view["selected"]["provider"], "claude")
            self.assertEqual(view["turns"][-1]["summary"]["cost_usd"], .01)

    def test_legacy_conversation_defaults_to_kiro(self):
        import watch_kiro
        agents = watch_kiro.group_agents([{"id": "old", "agent_id": "legacy", "status": "completed"}])
        self.assertEqual(agents[0]["provider"], "kiro")

    def test_model_http_endpoint_scopes_provider_and_rejects_unknown(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import watch_kiro
        panel = self.panel()
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch_kiro.make_handler(panel))
        panel.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"Cookie": panel.cookie_name + "=" + panel.cookie_value}
        conn.request("GET", "/api/models?provider=claude", headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["provider"], "claude")
        conn.request("GET", "/api/models?provider=unknown", headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("error", json.loads(response.read()))


if __name__ == "__main__":
    unittest.main()
