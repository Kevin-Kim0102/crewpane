"""Connection probes use isolated fake CLIs; no network or user config changes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

FAKE = '''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
mode=Path(__file__).with_suffix('.mode')
mode=mode.read_text() if mode.exists() else 'ok'
if mode=='timeout': time.sleep(5)
if '--version' in sys.argv:
 print('Other Product 1.0' if mode=='wrong' else '2.1.78 (Claude Code)')
else:
 print(json.dumps({'loggedIn':mode!='logout','apiProvider':'firstParty','token':'must-never-save'}))
 sys.exit(1 if mode=='logout' else 0)
'''


class ConnectionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        environment = {"HOME": str(self.base), "PATH": os.environ.get("PATH", ""),
                       "CODER_AGENTS_CONFIG": str(self.base / "config.json"), "PYTHONDONTWRITEBYTECODE": "1"}
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start(); self.addCleanup(patcher.stop)
        network = mock.patch('claude_catalog.fetch_json', side_effect=AssertionError('unexpected external model catalog request'))
        network.start(); self.addCleanup(network.stop)
        self.binary = self.base / "claude"
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)

    def store(self):
        import cli_connections
        return cli_connections.Connections(self.base / "connections.json")


class ConnectionsTests(ConnectionFixture):

    def test_pause_retains_connection_default_and_path_but_blocks_execution(self):
        store = self.store()
        store.connect('claude', str(self.binary))
        with mock.patch('subprocess.run', side_effect=AssertionError('pause launches CLI')):
            store.set_paused('claude', True)
        row = next(p for p in store.snapshot()['providers'] if p['id']=='claude')
        self.assertTrue(row['connected'])
        self.assertTrue(row['paused'])
        self.assertFalse(row['available'])
        self.assertEqual(store.snapshot()['default_provider'],'claude')
        self.assertEqual(row['path'], str(self.binary))
        with self.assertRaisesRegex(ValueError,'暂停'): store.executable('claude')
        with self.assertRaises(ValueError): store.set_default('claude')
        store.set_paused('claude', False)
        self.assertEqual(store.executable('claude'), str(self.binary))
        store.disconnect('claude')
        with self.assertRaises(ValueError): store.set_paused('claude',False)

    def test_start_rejects_missing_executable_without_discarding_pause(self):
        store=self.store();store.connect('claude',str(self.binary));store.set_paused('claude',True)
        self.binary.unlink()
        with self.assertRaises(ValueError): store.set_paused('claude',False)
        self.assertTrue(store.read()['providers']['claude']['paused'])

    def test_dispatch_rejects_missing_role_before_launch(self):
        import run_agent
        with mock.patch('os.execv') as execute:
            for extra in ([], ['--provider','claude'], ['--provider','kiro']):
                with self.assertRaises(SystemExit):
                    run_agent.main(['--cwd', str(self.base), *extra])
        execute.assert_not_called()

    def test_paused_saved_provider_never_falls_back_to_path(self):
        store=self.store();store.connect('claude',str(self.binary));store.set_paused('claude',True)
        import cli_connections
        with mock.patch.dict(os.environ,{'CODER_AGENTS_CONFIG':str(self.base/'config.json')}), \
                mock.patch('shutil.which',side_effect=AssertionError('fallback')):
            with self.assertRaisesRegex(ValueError,'暂停'): cli_connections.runner_executable('claude')

    def test_macos_login_uses_official_command_with_quoted_path(self):
        import cli_connections
        import shlex
        binary = '/tmp/a path/claude $(touch unwanted)'
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch('subprocess.run') as run:
            cli_connections.launch_login(binary, 'claude')
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ['osascript', '-e'])
        self.assertIn(json.dumps(shlex.join([binary, 'auth', 'login'])), args[2])
        self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)

    def test_authenticated_connect_does_not_open_login_terminal(self):
        with mock.patch('cli_connections.launch_login', side_effect=AssertionError('unneeded login')):
            self.assertTrue(self.store().connect('claude', str(self.binary), allow_login=True)['ok'])

    def test_explicit_connect_can_login_then_recheck_before_saving(self):
        self.binary.with_suffix('.mode').write_text('logout')
        def authorize(binary, name):
            self.assertEqual((binary, name), (str(self.binary), 'claude'))
            self.binary.with_suffix('.mode').write_text('ok')
        with mock.patch('cli_connections.launch_login', side_effect=authorize) as launch:
            result = self.store().connect('claude', str(self.binary), allow_login=True)
        self.assertTrue(result['ok'], result)
        launch.assert_called_once()
        self.assertNotIn('must-never-save', self.store().path.read_text())

    def test_cancelled_login_does_not_save_or_expose_output(self):
        self.binary.with_suffix('.mode').write_text('logout')
        with mock.patch('cli_connections.launch_login', side_effect=ValueError('登录未完成')):
            result = self.store().connect('claude', str(self.binary), allow_login=True)
        self.assertFalse(result['ok'])
        self.assertFalse(self.store().path.exists())

    def test_login_wait_is_bounded_and_never_saves_failed_auth(self):
        self.binary.with_suffix('.mode').write_text('logout')
        with mock.patch('cli_connections.launch_login'), mock.patch('cli_connections.time.monotonic', side_effect=[0, 181]):
            result = self.store().connect('claude', str(self.binary), allow_login=True)
        self.assertFalse(result['ok'])
        self.assertIn('登录', result['message'])
        self.assertFalse(self.store().path.exists())

    def test_no_automatic_probe_or_path_discovery_on_read(self):
        store = self.store()
        with mock.patch("subprocess.run", side_effect=AssertionError("CLI ran")), \
                mock.patch("shutil.which", side_effect=AssertionError("PATH scanned")):
            view = store.snapshot()
        self.assertIsNone(view["default_provider"])
        self.assertFalse(any(p["available"] for p in view["providers"]))
        self.assertFalse(store.path.exists())

    def test_success_preserves_symlink_and_private_configuration(self):
        store = self.store()
        link = self.base / "selected-claude"
        link.symlink_to(self.binary)
        result = store.connect("claude", str(link))
        self.assertTrue(result["ok"])
        self.assertEqual(store.executable("claude"), str(link))
        saved = json.loads(store.path.read_text())
        self.assertEqual(saved["default_provider"], "claude")
        self.assertEqual(saved["providers"]["claude"]["path"], str(link))
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("must-never-save", store.path.read_text())
        with mock.patch("subprocess.run", side_effect=AssertionError("CLI ran")):
            self.assertTrue(self.store().snapshot()["providers"][1]["available"])

    def test_missing_wrong_product_logged_out_and_timeout_do_not_connect(self):
        for mode in ("wrong", "logout", "timeout"):
            self.binary.with_suffix('.mode').write_text(mode)
            with self.subTest(mode=mode):
                store = self.store()
                result = store.connect("claude", str(self.binary), timeout=.25)
                self.assertFalse(result["ok"])
                self.assertNotIn("must-never-save", json.dumps(result))
                self.assertFalse(store.path.exists())
        self.assertFalse(self.store().connect("claude", str(self.base / "missing"))["ok"])

    def test_failed_reconnect_retains_good_connection_and_invalid_path_never_falls_back(self):
        store = self.store()
        store.connect("claude", str(self.binary))
        before = store.path.read_bytes()
        self.assertFalse(store.connect("claude", str(self.base / "missing"))["ok"])
        self.assertEqual(store.path.read_bytes(), before)
        self.binary.unlink()
        with mock.patch("shutil.which", return_value="/another/claude"):
            self.assertFalse(store.snapshot()["providers"][1]["available"])
            with self.assertRaises(ValueError):
                store.executable("claude")

    def test_explicit_path_does_not_search_path_and_disconnect_removes_default(self):
        store = self.store()
        with mock.patch("shutil.which", side_effect=AssertionError("unexpected search")):
            self.assertTrue(store.connect("claude", str(self.binary))["ok"])
        store.set_default("claude")
        store.disconnect("claude")
        self.assertIsNone(store.snapshot()["default_provider"])
        self.assertFalse(store.snapshot()["providers"][1]["available"])
        with self.assertRaises(ValueError):
            store.set_default("kiro")

    def test_blank_path_discovers_only_on_explicit_connect(self):
        with mock.patch("shutil.which", return_value=str(self.binary)) as which:
            self.assertTrue(self.store().connect("claude", "")["ok"])
            which.assert_called_once_with("claude")

    def test_runner_uses_saved_path_even_with_empty_path_environment(self):
        store = self.store()
        store.connect("claude", str(self.binary))
        marker = self.base / "invoked"
        self.binary.write_text('#!/bin/sh\nprintf "saved-path" > "' + str(marker) + '"\n')
        done = subprocess.run([sys.executable, str(ROOT / "scripts/run_claude.py"), "--cwd", str(self.base),
                               "--run-dir", str(self.base / "run")], input="hello", text=True, capture_output=True,
                              env={**os.environ, "PATH": "", "CODER_AGENTS_CONFIG": str(self.base / "config.json")})
        self.assertTrue(marker.exists(), done.stderr)
        self.assertEqual(marker.read_text(), "saved-path")


class PanelConnectionTests(ConnectionFixture):

    def test_model_settings_validate_and_save_default_and_role_override_for_kiro(self):
        panel = self.panel()
        self.binary.write_text(FAKE.replace('2.1.78 (Claude Code)', 'kiro-cli 3.0.0'))
        panel.connections.connect("kiro", str(self.binary))
        models = {"status": "ok", "models": [
            {"id": "first", "effort": {"known": True, "supported": True, "levels": ["high"]}},
            {"id": "second", "effort": {"known": True, "supported": False, "levels": []}},
        ]}
        payload = {"action": "defaults", "provider": "kiro", "model": "first", "effort": "high",
                   "revision": panel.roles.snapshot()["revision"],
                   "overrides": {"reviewer": {"model": "second", "effort": None}},
                   "switch_roles": ["reviewer"]}
        with mock.patch.object(panel, "models_view", return_value=models):
            invalid = {**payload, "overrides": {"reviewer": {"model": "second", "effort": "high"}}}
            with self.assertRaisesRegex(ValueError, "思考强度"):
                panel.connection_action(invalid)
            self.assertFalse(panel.connections.defaults_configured("kiro"))
            self.assertFalse(panel.roles.path.exists())
            status, result = panel.connection_action(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["roles_config"]["roles"][-1]["model"], "second")
        with self.assertRaisesRegex(ValueError, "选择 CLI"):
            panel.roles.get("architect", executable=True)
        self.assertEqual(panel.roles.get("reviewer", executable=True)["model"], "second")

    def test_model_settings_switch_role_provider_only_when_explicit(self):
        panel = self.panel()
        panel.connections.connect("claude", str(self.binary))
        roles = panel.roles.snapshot()
        coder = {**panel.roles.get("coder"), "provider": "kiro", "model": "kiro-model", "effort": "high"}
        roles = panel.roles.mutate({"action": "save", "role": coder, "revision": roles["revision"]})
        catalog = {"status": "ok", "models": [
            {"id": "selected", "effort": {"known": True, "supported": True, "levels": ["low"]}},
        ]}
        payload = {"action": "defaults", "provider": "claude", "model": "selected", "effort": "low",
                   "revision": roles["revision"]}
        follow_default = {"coder": {"model": None, "effort": None}}
        with mock.patch.object(panel, "models_view", return_value=catalog):
            panel.connection_action(payload)
            self.assertEqual(panel.roles.get("coder"), coder)
            with self.assertRaisesRegex(ValueError, "CLI 已变化"):
                panel.connection_action({**payload, "overrides": follow_default})
            panel.connection_action({**payload, "overrides": follow_default, "switch_roles": ["coder"]})
        updated = panel.roles.get("coder", executable=True)
        self.assertEqual((updated["provider"], updated["model"], updated["effort"]), ("claude", "selected", "low"))

    def test_pause_start_api_gates_tasks_and_keeps_connection(self):
        panel=self.panel()
        panel.connection_action({'action':'connect','provider':'claude','path':str(self.binary)})
        status,result=panel.connection_action({'action':'pause','provider':'claude'})
        self.assertEqual(status,200)
        self.assertTrue(next(p for p in result['providers'] if p['id']=='claude')['paused'])
        self.assertEqual(panel.models_view(provider='claude')['models'],[])
        with self.assertRaisesRegex(ValueError,'暂停'):
            panel.connections.executable('claude')
        self.assertEqual(panel.connection_action({'action':'start','provider':'claude'})[0],200)
        self.assertEqual(panel.models_view(provider='claude')['models'], [])
        self.assertTrue(next(p for p in panel.connections.snapshot()['providers'] if p['id']=='claude')['available'])

    def test_kiro_connect_reads_catalog_and_retains_connection_on_catalog_failure(self):
        for catalog in ({'status':'ok','models':[{'id':'other'}]}, {'status':'error','error':'读取失败','models':[]}):
            with self.subTest(catalog=catalog):
                panel = self.panel()
                self.binary.write_text(FAKE.replace('2.1.78 (Claude Code)', 'kiro-cli 3.0.0'))
                with mock.patch.object(panel, 'models_view', return_value=catalog) as models:
                    status, result = panel.connection_action({'action':'connect','provider':'kiro','path':str(self.binary)})
                self.assertEqual(status, 200)
                self.assertTrue(result['ok'])
                models.assert_called_once_with(refresh=True, provider='kiro')
                self.assertEqual(result['catalog'],catalog)
                self.assertEqual(panel.connections.executable('kiro'),str(self.binary))
                if catalog['status']=='error': self.assertIn('重试',result['message'])
    def panel(self):
        import watch_kiro
        config = {"port": 47899, "token": "T" * 32, "daily_base": str(self.base / "daily"),
                  "auto_cleanup_enabled": False}
        path = self.base / "config.json"
        path.write_text(json.dumps(config))
        return watch_kiro.Panel(config, path, codex_state=self.base / "missing")

    def test_unconnected_panel_never_probes_cli_and_blocks_models_and_send(self):
        import watch_kiro
        with mock.patch("subprocess.run", side_effect=AssertionError("CLI invoked")), \
                mock.patch("shutil.which", side_effect=AssertionError("PATH scanned")):
            panel = self.panel()
            view = panel.view()
            self.assertIsNone(view["viewer"]["default_provider"])
            self.assertFalse(any(p["available"] for p in view["viewer"]["providers"]))
            self.assertEqual(panel.models_view(provider="kiro")["models"], [])
            with self.assertRaisesRegex(ValueError, "连接"):
                panel.send({"provider": "claude", "group": watch_kiro.OTHER_GROUP, "text": "hello"})

    def test_connect_default_disconnect_and_restart(self):
        panel = self.panel()
        status, result = panel.connection_action({"action": "connect", "provider": "claude", "path": str(self.binary)})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(panel.default_provider, "claude")
        self.assertEqual(self.panel().default_provider, "claude")
        with self.assertRaisesRegex(ValueError, "选择 CLI"):
            panel.send({"group": "other", "text": "hello"})
        self.assertEqual(panel.models_view(provider="claude")["provider"], "claude")
        self.assertEqual(panel.connection_action({"action": "default", "provider": "claude"})[0], 200)
        panel.connection_action({"action": "disconnect", "provider": "claude"})
        self.assertIsNone(panel.default_provider)
        self.assertEqual(panel.models_view(provider="claude")["models"], [])

    def test_connection_does_not_run_model_and_test_call_is_tool_free(self):
        panel = self.panel()
        status, result = panel.connection_action({"action": "connect", "provider": "claude", "path": str(self.binary)})
        self.assertEqual(status, 200)
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            status, result = panel.connection_action({"action": "test", "provider": "claude"})
            self.assertEqual(status, 200)
            args = popen.call_args[0][0]
            self.assertIn("--check", args)
            self.assertEqual(args[args.index("--cli-path")+1], str(self.binary))
            self.assertIn("--timeout", args)

    def test_disabled_kiro_metadata_and_busy_checks_never_start_cli(self):
        panel = self.panel()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("CLI started")), \
                mock.patch("subprocess.run", side_effect=AssertionError("CLI probed")):
            self.assertEqual(panel.account_view("kiro")["status"], "unsupported")
            self.assertEqual(panel.models_view(provider="kiro")["models"], [])
            panel.connection_slots.acquire()
            try:
                self.assertEqual(panel.connection_action({"action":"connect", "provider":"claude"})[0],409)
            finally:
                panel.connection_slots.release()

    def test_saved_kiro_connection_does_not_start_auth_on_page_load(self):
        panel = self.panel()
        with mock.patch("subprocess.run", side_effect=[subprocess.CompletedProcess([],0,"kiro-cli 3.0.0", ""),
                                                      subprocess.CompletedProcess([],0,"logged in", "")]):
            self.assertTrue(panel.connections.connect("kiro",str(self.binary))["ok"])
        panel.default_provider = "kiro"
        with mock.patch("subprocess.run", side_effect=AssertionError("CLI ran on page load")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("ACP started on page load")):
            self.assertEqual(panel.view()["viewer"]["default_provider"],"kiro")
            models=panel.models_view(provider="kiro")
            self.assertEqual(models["default"]["model"],"claude-sonnet-5.5")

    def test_connection_api_requires_cookie_origin_and_explicit_action(self):
        import http.client
        from http.server import ThreadingHTTPServer
        import threading
        import watch_kiro
        panel = self.panel()
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch_kiro.make_handler(panel))
        panel.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"Cookie": panel.cookie_name + "=" + panel.cookie_value, "Content-Type": "application/json",
                   "Origin": "http://127.0.0.1:" + str(panel.port)}
        body = json.dumps({"action": "connect", "provider": "claude", "path": str(self.binary)})
        conn.request("POST", "/api/connections", body, headers={"Content-Type": "application/json"})
        response = conn.getresponse(); response.read()
        self.assertEqual(response.status, 401)
        conn.request("POST", "/api/connections", body, headers={k:v for k,v in headers.items() if k!='Origin'})
        response = conn.getresponse(); response.read()
        self.assertEqual(response.status, 403)
        conn.request("POST", "/api/connections", body, headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())["ok"])
        conn.request("GET", "/api/connections", headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["default_provider"], "claude")


if __name__ == "__main__":
    unittest.main()
