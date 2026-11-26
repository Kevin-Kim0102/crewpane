"""Actual endpoint catalogs, isolated credentials and no live model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_providers
import cli_connections


class ClaudeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.cwd = self.base / "project"
        self.cwd.mkdir()
        self.env = {"HOME": str(self.home), "CODER_AGENTS_CONFIG": str(self.base / "state/config.json")}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start(); self.addCleanup(patcher.stop)

    def settings(self, path, env):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": env}))

    def catalog(self, pages):
        requests = []
        def fetch(request, timeout=10):
            requests.append(request)
            result = pages.pop(0)
            if isinstance(result, Exception): raise result
            return result
        return agent_providers.ClaudeModels(fetch=fetch), requests

    def test_resolves_project_endpoint_and_auth_without_exposing_secret(self):
        import claude_catalog
        self.settings(self.home / ".claude/settings.json", {"ANTHROPIC_BASE_URL": "https://global.invalid", "ANTHROPIC_API_KEY": "global-secret"})
        self.settings(self.cwd / ".claude/settings.json", {"ANTHROPIC_BASE_URL": "https://project.invalid/api"})
        self.settings(self.cwd / ".claude/settings.local.json", {"ANTHROPIC_AUTH_TOKEN": "local-secret"})
        config = claude_catalog.resolve_config(self.cwd)
        self.assertEqual(config["base_url"], "https://project.invalid/api")
        self.assertEqual(config["headers"]["Authorization"], "Bearer local-secret")
        self.assertNotIn("x-api-key", config["headers"])
        self.assertNotIn("local-secret", config["cache_key"])

    def test_cli_settings_override_inherited_environment_and_are_cwd_scoped(self):
        import claude_catalog
        self.settings(self.home / ".claude/settings.json", {"ANTHROPIC_BASE_URL": "https://user.invalid", "ANTHROPIC_API_KEY": "settings-key"})
        self.settings(self.cwd / ".claude/settings.json", {"ANTHROPIC_BASE_URL": "https://project.invalid"})
        child = self.cwd / "child"; child.mkdir()
        (self.cwd / ".git").mkdir()
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://shell.invalid", "ANTHROPIC_API_KEY": "shell-key"}):
            self.assertEqual(claude_catalog.resolve_config(self.cwd)["base_url"], "https://project.invalid")
            config = claude_catalog.resolve_config(child)
            self.assertEqual(config["base_url"], "https://user.invalid")
            self.assertEqual(config["headers"]["x-api-key"], "settings-key")

    def test_nonforce_never_requests_and_force_paginates_current_endpoint(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://proxy.invalid/v1", "ANTHROPIC_API_KEY": "test-secret"}):
            models, requests = self.catalog([
                {"data": [{"id": "claude-sonnet-new", "display_name": "Sonnet New"}], "has_more": True, "last_id": "claude-sonnet-new"},
                {"data": [{"id": "haiku", "display_name": "Haiku"}], "has_more": False}])
            self.assertEqual(models.snapshot(cwd=self.cwd)["models"], [])
            self.assertEqual(requests, [])
            result = models.snapshot(force=True, cwd=self.cwd)
            self.assertEqual(result["status"], "ok")
            self.assertEqual([m["id"] for m in result["models"]], ["claude-sonnet-new", "haiku"])
            self.assertEqual(requests[0].full_url, "https://proxy.invalid/v1/models?limit=100")
            self.assertIn("after_id=claude-sonnet-new", requests[1].full_url)
            self.assertEqual(requests[0].get_header("X-api-key"), "test-secret")
            self.assertNotIn("test-secret", json.dumps(result))
            self.assertEqual(models.snapshot(cwd=self.cwd)["models"], result["models"])
            self.assertEqual(agent_providers.ClaudeModels().snapshot(cwd=self.cwd)["models"], result["models"])

    def test_endpoint_change_does_not_reuse_another_catalog_or_redirect_credentials(self):
        import claude_catalog
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://one.invalid", "ANTHROPIC_API_KEY": "key"}):
            models, requests = self.catalog([{"data": [{"id": "one-model"}]}])
            self.assertEqual(models.snapshot(force=True)["status"], "ok")
            with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://two.invalid"}):
                self.assertEqual(models.snapshot()["models"], [])
        redirect = claude_catalog.NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_errors_do_not_echo_server_or_secret_and_do_not_fallback_to_aliases(self):
        for failure in [HTTPError("https://secret.invalid/key", 401, "test-secret", {}, None),
                        ValueError("test-secret"), TimeoutError("test-secret")]:
            with self.subTest(error=failure), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-secret"}):
                models, _ = self.catalog([failure])
                result = models.snapshot(force=True)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["models"], [])
                self.assertNotIn("test-secret", json.dumps(result))
        models, requests = self.catalog([])
        self.assertEqual(models.snapshot(force=True)["status"], "error")
        self.assertEqual(requests, [])

    def test_untrusted_directory_cannot_echo_auth_in_model_fields(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-secret"}):
            for item in ({"id": "test-secret"}, {"id": "model", "display_name": "test-secret"}):
                models, _ = self.catalog([{"data": [item]}])
                result = models.snapshot(force=True)
                self.assertNotIn("test-secret", json.dumps(result))

    def test_empty_and_malformed_catalogs_and_pagination_loop_fail(self):
        for pages in [[{"data": []}], [{"models": ["made-up"]}],
                      [{"data": [{"id": "m"}], "has_more": True, "last_id": "m"}] * 2]:
            with self.subTest(pages=pages), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}):
                models, _ = self.catalog(pages)
                self.assertEqual(models.snapshot(force=True)["status"], "error")

    def test_current_proxy_ids_with_wildcards_and_namespaces_remain_exact(self):
        import agent_roles
        ids=['claude-opus-*','claude-sonnet-*','opencode-go/deepseek-v4.1-flash']
        with mock.patch.dict(os.environ,{'ANTHROPIC_API_KEY':'key'}):
            models,_=self.catalog([{'data':[{'id':name} for name in ids]}])
            result=models.snapshot(force=True)
        self.assertEqual(result['status'],'ok')
        self.assertEqual([row['id'] for row in result['models']],ids)
        for name in ids:
            self.assertEqual(models.validate(name,None),(name,None))
            role={**agent_roles.DEFAULTS[0],'model':name,'effort':None}
            self.assertEqual(agent_roles.validate_role(role)['model'],name)
        for name in ['--bad','bad\nmodel','bad model','bad\x00model']:
            with self.assertRaises(ValueError):models.validate(name,None)

    def test_failed_refresh_is_not_reported_as_a_successful_cached_read(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}):
            models, _ = self.catalog([{"data": [{"id": "model-one"}]}, TimeoutError()])
            self.assertEqual(models.snapshot(force=True)["status"], "ok")
            self.assertEqual(models.snapshot(force=True)["status"], "error")
            self.assertEqual(models.snapshot()["status"], "error")

    def test_invalid_environment_shape_is_safe_and_cache_failure_does_not_lose_models(self):
        self.settings(self.home / ".claude/settings.json", ["not-a-mapping"])
        models, _ = self.catalog([])
        self.assertEqual(models.snapshot(force=True)["status"], "error")
        self.settings(self.home / ".claude/settings.json", {"ANTHROPIC_API_KEY": "key"})
        models, _ = self.catalog([{"data": [{"id": "model-one"}]}])
        with mock.patch.object(models, "_save_cache", side_effect=PermissionError()):
            self.assertEqual(models.snapshot(force=True)["status"], "ok")

    def test_reconnect_keeps_saved_default_pair(self):
        from tests.test_connections import FAKE
        binary = self.base / "claude"
        binary.write_text(FAKE); binary.chmod(0o700)
        store = cli_connections.Connections()
        store.set_defaults("claude", "claude-sonnet-new", "high")
        self.assertTrue(store.connect("claude", str(binary))["ok"])
        store.disconnect("claude")
        self.assertTrue(store.connect("claude", str(binary))["ok"])
        self.assertEqual(store.defaults("claude"), {"model": "claude-sonnet-new", "effort": "high"})

    def test_catalog_does_not_break_existing_role_config_or_unset_default_pair(self):
        import agent_roles
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key", "XDG_CONFIG_HOME": str(self.base / "config")}):
            models, _ = self.catalog([{"data": [{"id": "claude-sonnet-new"}]}])
            models.snapshot(force=True)
            store = agent_roles.RoleStore()
            role = {**store.get("coder"), "provider": "claude", "model": "sonnet", "effort": "medium"}
            store.mutate({"action": "save", "role": role, "revision": store.snapshot()["revision"]})
            self.assertEqual(store.get("coder")["model"], "sonnet")
            self.assertEqual(models.validate("sonnet", "medium"), ("sonnet", "medium"))
            with self.assertRaises(ValueError): models.validate("unavailable", "high", check_catalog=True)

    def test_defaults_persist_and_survive_reconnect_without_modifying_cli_settings(self):
        store = cli_connections.Connections()
        store.save({"version": 1, "default_provider": "claude", "providers": {"claude": {"enabled": True, "path": "/missing"}}})
        self.assertEqual(store.defaults("claude"), {"model": "sonnet", "effort": "medium"})
        store.set_defaults("claude", "claude-sonnet-new", "high")
        self.assertEqual(store.defaults("claude"), {"model": "claude-sonnet-new", "effort": "high"})
        row = next(p for p in store.snapshot()["providers"] if p["id"] == "claude")
        self.assertEqual(row["defaults"]["effort"], "high")
        store.disconnect("claude")
        self.assertEqual(store.defaults("claude")["model"], "claude-sonnet-new")
        self.assertFalse((self.home / ".claude/settings.json").exists())
        with self.assertRaises(ValueError): store.set_defaults("claude", "--bad", "high")
        store.set_defaults("kiro", "a-kiro-model", "high")
        self.assertEqual(store.defaults("kiro"), {"model": "a-kiro-model", "effort": "high"})
        self.assertEqual(store.defaults("claude")["model"], "claude-sonnet-new")


if __name__ == "__main__": unittest.main()
