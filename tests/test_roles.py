"""Global role config and delegation, isolated from personal config and real model calls."""
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
import agent_roles
import run_agent


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="coder-roles-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = {"HOME": str(self.base), "XDG_CONFIG_HOME": str(self.base / "config"),
                    "PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start(); self.addCleanup(patcher.stop)
        self.store = agent_roles.RoleStore()

    def custom(self):
        return {**self.store.get("coder"), "id": "docs", "name": "文档", "description": "维护文档"}

    def choose_cli(self, role_id, cli="claude"):
        self.store.mutate({"action": "save", "role": {**self.store.get(role_id), "provider": cli},
                           "revision": self.store.snapshot()["revision"]})

    def test_continuation_uses_latest_role_and_refuses_disabled_or_changed_provider(self):
        role = self.store.get('coder')
        role.update(name='新版编码', instructions='仅修改授权文件', provider='claude', model='opus', effort='high')
        self.store.mutate({'action': 'save', 'role': role, 'revision': self.store.snapshot()['revision']})
        actual, prompt = agent_roles.continuation('coder', 'claude', '修复问题', store=self.store)
        self.assertEqual(actual['model'], 'opus')
        self.assertIn('仅修改授权文件', prompt)
        with self.assertRaisesRegex(ValueError, '新建'):
            agent_roles.continuation('coder', 'kiro', '修复问题', store=self.store)
        role['enabled'] = False
        self.store.mutate({'action': 'save', 'role': role, 'revision': self.store.snapshot()['revision']})
        with self.assertRaisesRegex(ValueError, '停用'):
            agent_roles.continuation('coder', 'claude', '修复问题', store=self.store)

    def test_dispatch_sets_identity_only_and_direct_calls_clear_inherited_identity(self):
        run_agent.Connections().set_defaults('claude', 'default-model', 'medium')
        self.choose_cli('coder')
        class Executed(Exception): pass
        captured = []
        def execute(*args):
            captured.append((os.environ.get('CODER_AGENTS_ROLE_ID'), os.environ.get('CODER_AGENTS_ROLE_NAME')))
            raise Executed
        with mock.patch.object(run_agent.sys, 'stdin', mock.Mock(read=lambda: 'task')), \
                mock.patch.object(run_agent.os, 'dup2'), mock.patch.object(run_agent.os, 'execv', side_effect=execute):
            with self.assertRaises(Executed): run_agent.main(['--role', 'coder', '--cwd', str(self.base)])
            self.assertEqual(captured[-1], ('coder', '编码'))
            with self.assertRaises(SystemExit): run_agent.main(['--provider', 'claude', '--cwd', str(self.base)])
            with self.assertRaises(Executed): run_agent.main(['--provider', 'claude', '--cwd', str(self.base), '--check'])
            self.assertEqual(captured[-1], (None, None))

    def test_inheritance_resolves_current_cli_defaults_but_preserves_overrides(self):
        store = run_agent.Connections()
        store.set_defaults('claude', 'default-model', 'high')
        self.choose_cli('architect')
        provider, args, _ = run_agent.role_invocation('architect', [], 'task')
        self.assertEqual((provider, args), ('claude', ['--model', 'default-model', '--effort', 'high']))
        self.store.mutate({'action':'models', 'provider':'claude', 'revision':self.store.snapshot()['revision'],
                           'switch_roles':['reviewer'],
                           'overrides':{'reviewer': {'model':'override-model', 'effort':None}}}, model_settings=True)
        store.set_defaults('claude', 'next-model', 'low')
        self.assertEqual(run_agent.role_invocation('architect', [], 'task')[1], ['--model', 'next-model', '--effort', 'low'])
        self.assertEqual(run_agent.role_invocation('reviewer', [], 'task')[1], ['--model', 'override-model', '--no-effort'])

    def test_roles_require_a_saved_cli_default_or_an_explicit_model(self):
        for provider in ('claude', 'kiro'):
            for role_id in agent_roles.BUILTINS:
                role = {**self.store.get(role_id), 'provider': provider}
                self.store.mutate({'action': 'save', 'role': role, 'revision': self.store.snapshot()['revision']})
                with self.assertRaisesRegex(ValueError, '默认模型'):
                    self.store.get(role_id, executable=True)
                role.update(model='selected-model', effort=None)
                self.store.mutate({'action': 'save', 'role': role, 'revision': self.store.snapshot()['revision']})
                self.assertEqual(self.store.get(role_id, executable=True)['model'], 'selected-model')
                role.update(model=None)
                self.store.mutate({'action': 'save', 'role': role, 'revision': self.store.snapshot()['revision']})

    def test_legacy_roles_are_preserved_and_new_builtins_are_inserted_in_order(self):
        existing = [{**self.store.get('coder'), 'provider':'claude', 'model':'old-model', 'effort':'high'},
                    {**self.store.get('tester'), 'name':'我的测试'}, self.custom()]
        self.store.path.parent.mkdir(parents=True)
        raw = json.dumps({'version':1,'roles':existing}).encode()
        self.store.path.write_bytes(raw)
        data = self.store.snapshot()
        self.assertEqual([r['id'] for r in data['roles']], ['architect','coder','tester','reviewer','docs'])
        self.assertEqual([data['roles'][i] for i in (1,2,4)], existing)
        self.assertEqual(self.store.path.read_bytes(), raw)
        for role_id in ('architect', 'reviewer'):
            with self.assertRaisesRegex(ValueError, '内置'):
                self.store.mutate({'action':'delete', 'id':role_id, 'revision':data['revision']})

    def test_default_read_is_global_and_has_no_write_or_process_side_effect(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("process")):
            data = self.store.snapshot()
        self.assertEqual(self.store.path, self.base / "config/coder-agents/roles.json")
        self.assertEqual([r["id"] for r in data["roles"]], ["architect", "coder", "tester", "reviewer"])
        for role in data["roles"]:
            self.assertIsNone(role["provider"])
            with self.assertRaisesRegex(ValueError, "选择 CLI"):
                self.store.get(role["id"], executable=True)
        self.assertFalse(self.store.path.exists())
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(agent_roles.roles_path(), self.base / ".config/coder-agents/roles.json")

    def test_crud_atomic_private_and_disabled_role_is_not_executable(self):
        first = self.store.snapshot()
        saved = self.store.mutate({"action": "save", "role": self.custom(), "revision": first["revision"]})
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(agent_roles.RoleStore().get("docs")["name"], "文档")
        role = self.store.get("docs"); role["enabled"] = False
        saved = self.store.mutate({"action": "save", "role": role, "revision": saved["revision"]})
        with self.assertRaisesRegex(ValueError, "停用"):
            self.store.get("docs", executable=True)
        self.store.mutate({"action": "delete", "id": "docs", "revision": saved["revision"]})
        self.assertEqual(len(self.store.snapshot()["roles"]), 4)
        self.assertEqual(set(json.loads(self.store.path.read_text())), {"version", "roles"})

    def test_invalid_and_stale_updates_never_replace_saved_data(self):
        first = self.store.snapshot()
        self.store.mutate({"action": "save", "role": self.custom(), "revision": first["revision"]})
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "更新"):
            self.store.mutate({"action": "save", "role": self.custom(), "revision": first["revision"]})
        for changed in ({"id": "../escape"}, {"provider": "unknown"}, {"enabled": "yes"},
                        {"model": "--evil"}, {"effort": "unknown"}, {"name": ""},
                        {"permissions": {"bypass": True}}, {"constraints": "not-array"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.store.mutate({"action": "save", "role": {**self.custom(), **changed},
                                   "revision": self.store.snapshot()["revision"]})
        self.assertEqual(before, self.store.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "内置"):
            self.store.mutate({"action": "delete", "id": "coder", "revision": self.store.snapshot()["revision"]})

    def test_corrupt_config_is_not_reset(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text('{broken')
        with self.assertRaises(ValueError): self.store.snapshot()
        with self.assertRaises(ValueError):
            self.store.mutate({"action": "save", "role": {"id": "x"}, "revision": "missing"})
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_cli_json_is_latest_and_disabled_hidden_by_default(self):
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "coder_agents", "roles", *args],
                                  cwd=ROOT, capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual([r["id"] for r in json.loads(cli("list").stdout)["roles"]], ["architect", "coder", "tester", "reviewer"])
        role = self.store.get("tester"); role["enabled"] = False
        self.store.mutate({"action": "save", "role": role, "revision": self.store.snapshot()["revision"]})
        self.assertEqual(len(json.loads(cli("list").stdout)["roles"]), 3)
        self.assertEqual(len(json.loads(cli("list", "--all").stdout)["roles"]), 4)
        self.assertFalse(json.loads(cli("show", "tester").stdout)["enabled"])
        self.assertNotEqual(cli("show", "missing").returncode, 0)

    def test_role_dispatch_applies_provider_model_and_prompt(self):
        role = self.custom(); role.update(provider="kiro", model="claude-sonnet-5.5", effort=None)
        self.store.mutate({"action": "save", "role": role, "revision": self.store.snapshot()["revision"]})
        provider, args, prompt = run_agent.role_invocation("docs", ["--cwd", "/project"], "本次任务")
        self.assertEqual(provider, "kiro")
        self.assertEqual(args, ["--cwd", "/project", "--model", "claude-sonnet-5.5", "--no-effort"])
        self.assertIn("维护文档", prompt); self.assertIn("本次任务", prompt)
        for args in (["--model=sonnet"], ["--effort", "high"], ["--check"], ["--cli-path", "/other"]):
            with self.assertRaises(ValueError): run_agent.role_invocation("coder", args, "task")
        with self.assertRaises(ValueError): run_agent.role_invocation("missing", [], "task")
        with self.assertRaises(ValueError): run_agent.role_invocation("coder", [], " ")

    def test_role_dispatch_reaches_real_runner_with_fake_cli(self):
        from tests.test_claude import FAKE
        binary = self.base / "claude"; binary.write_text(FAKE); binary.chmod(0o700)
        capture = self.base / "capture.json"
        env = {**self.env, "PATH": str(self.base) + os.pathsep + self.env["PATH"], "CAPTURE": str(capture),
               "CODER_AGENTS_CONFIG": str(self.base / "state/config.json")}
        with mock.patch.dict(os.environ, env):
            connections = run_agent.Connections()
            connections.save({"version": 1, "default_provider": "claude",
                              "providers": {"claude": {"enabled": True, "path": str(binary)}}})
            connections.set_defaults("claude", "sonnet", "medium")
        self.choose_cli("tester")
        directory = self.base / "run"
        done = subprocess.run([sys.executable, "-m", "coder_agents", "run", "--role", "tester",
                               "--cwd", str(self.base), "--run-dir", str(directory)], input="准备阶段：编写登录测试，不运行。",
                              capture_output=True, text=True, env=env, cwd=ROOT, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        actual = json.loads(capture.read_text())
        self.assertIn("准备阶段：编写登录测试，不运行。", actual["prompt"])
        self.assertIn("不修改业务实现", actual["prompt"])
        self.assertEqual(actual["args"][actual["args"].index("--model") + 1], "sonnet")
        self.assertEqual(json.loads((directory / "state.json").read_text())["status"], "completed")
        self.assertFalse(any("snapshot" in p.name or "role" in p.name for p in directory.iterdir()))

    def test_role_api_requires_auth_and_same_origin_and_preserves_store_on_failure(self):
        import http.client
        from http.server import ThreadingHTTPServer
        from types import SimpleNamespace
        import threading
        import watch_kiro
        panel = SimpleNamespace(port=0, prefix="/secret/", last_seen=0, cookie_name="panel", cookie_value="value", roles=self.store)
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch_kiro.make_handler(panel))
        panel.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5); self.addCleanup(conn.close)
        headers = {"Cookie": "panel=value", "Origin": "http://127.0.0.1:" + str(panel.port), "Content-Type": "application/json"}
        def request(method, body=None, hdrs=None):
            conn.request(method, "/api/roles", json.dumps(body) if body else None, headers=hdrs or {})
            response = conn.getresponse(); raw = response.read()
            return response.status, json.loads(raw) if raw.startswith(b'{') else None
        self.assertEqual(request("GET")[0], 401)
        status, data = request("GET", hdrs=headers)
        self.assertEqual(status, 200); self.assertEqual(len(data["roles"]), 4)
        payload = {"action": "save", "role": self.custom(), "revision": data["revision"]}
        self.assertEqual(request("POST", payload)[0], 401)
        self.assertEqual(request("POST", payload, {"Cookie": "panel=value", "Content-Type": "application/json"})[0], 403)
        self.assertFalse(self.store.path.exists())
        status, data = request("POST", payload, headers)
        self.assertEqual(status, 200); self.assertEqual(len(data["roles"]), 5)
        before = self.store.path.read_bytes()
        self.assertEqual(request("POST", payload, headers)[0], 400)
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
