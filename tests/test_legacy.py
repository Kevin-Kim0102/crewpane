"""Legacy-install link/unlink, fail-closed task checks and wheel contents. All work happens in temporary directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tmpdirs
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coder_agents import legacy, service  # noqa: E402


def snapshot(base):
    """Relative path -> (kind, content or link target) for everything under base."""
    result = {}
    for path in sorted(Path(base).rglob("*")):
        rel = str(path.relative_to(base))
        if path.is_symlink():
            result[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            result[rel] = ("dir", None)
        else:
            result[rel] = ("file", path.read_bytes())
    return result


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.TemporaryDirectory(prefix="coder-agents-legacy-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.legacy = self.home / "skills/kiro-coding"
        for name, files in (("scripts", ["watch_kiro.py", "run_kiro.py"]), ("assets", ["viewer.html"])):
            (self.legacy / name).mkdir(parents=True)
            for f in files:
                (self.legacy / name / f).write_text(f"old {f}\n")
        (self.legacy / "SKILL.md").write_text("skill\n")
        (self.legacy / "settings.json").write_text("{}\n")
        self.state = self.home / "state"
        self.state.mkdir()
        self.config = self.state / "config.json"
        self.config.write_text(json.dumps({"port": 40000, "token": "T" * 32, "auto_cleanup_enabled": False}))
        (self.state / "projects.json").write_text('{"projects": []}')
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
                    "CODER_AGENTS_CONFIG": str(self.config)}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "coder_agents", *args], cwd=ROOT, env=self.env, capture_output=True,
                              text=True, timeout=60)

    def plan(self):
        return legacy.plan_link(self.legacy, self.config)

    def start_task(self, status="running"):
        root = self.home / "proj/work/kiro-runs"
        (root / "r1").mkdir(parents=True, exist_ok=True)
        (root / "r1/state.json").write_text(json.dumps({"status": status}))
        (self.state / "projects.json").write_text(json.dumps({"projects": [{"cwd": str(root.parent), "root": str(root)}]}))


class LinkTests(Base):
    def test_dry_run_changes_nothing(self):
        before = snapshot(self.home)
        done = self.cli("legacy", "link", "--legacy", str(self.legacy))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("预演", done.stdout)
        self.assertEqual(snapshot(self.home), before)

    def test_apply_links_backs_up_and_keeps_skill_files(self):
        before = snapshot(self.legacy)
        done = self.cli("legacy", "link", "--legacy", str(self.legacy), "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for name in ("scripts", "assets"):
            self.assertTrue((self.legacy / name).is_symlink())
            self.assertEqual(Path(os.path.realpath(self.legacy / name)), ROOT / name)
        backups = legacy.find_backups(self.legacy)
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "scripts/watch_kiro.py").read_text(), "old watch_kiro.py\n")
        self.assertEqual((backups[0] / "assets/viewer.html").read_text(), "old viewer.html\n")
        for keep in ("SKILL.md", "settings.json"):
            self.assertEqual(snapshot(self.legacy)[keep], before[keep])
        # the old ExecStart path now resolves to code that lives in this repository
        self.assertEqual(Path(os.path.realpath(self.legacy / "scripts/watch_kiro.py")), ROOT / "scripts/watch_kiro.py")

    def test_repeat_is_a_no_op(self):
        legacy.apply_link(self.plan())
        before = snapshot(self.legacy)
        plan = self.plan()
        self.assertTrue(plan["already_linked"])
        self.assertIsNone(legacy.apply_link(plan))
        self.assertEqual(snapshot(self.legacy), before)
        self.assertEqual(self.cli("legacy", "link", "--legacy", str(self.legacy), "--apply").returncode, 0)
        self.assertEqual(len(legacy.find_backups(self.legacy)), 1)

    def test_refuses_wrong_symlink_missing_dir_and_file(self):
        other = self.home / "elsewhere"
        other.mkdir()
        shutil.rmtree(self.legacy / "assets")
        (self.legacy / "assets").symlink_to(other)
        plan = self.plan()
        self.assertTrue(any("拒绝处理" in b for b in plan["blockers"]))
        before = snapshot(self.legacy)
        with self.assertRaises(PermissionError):
            legacy.apply_link(plan)
        self.assertEqual(snapshot(self.legacy), before)
        self.assertEqual(self.cli("legacy", "link", "--legacy", str(self.legacy), "--apply").returncode, 3)
        self.assertEqual(self.cli("legacy", "link", "--legacy", str(self.home / "nope")).returncode, 3)
        (self.legacy / "assets").unlink()
        (self.legacy / "assets").write_text("file")
        self.assertTrue(any("不是目录" in b for b in self.plan()["blockers"]))

    def test_refuses_active_task_at_plan_and_at_apply(self):
        plan = self.plan()
        self.assertEqual(plan["blockers"], [])
        self.start_task()  # appears after the plan was made
        before = snapshot(self.legacy)
        with self.assertRaises(PermissionError):
            legacy.apply_link(plan)
        self.assertEqual(snapshot(self.legacy), before)
        self.assertTrue(any("活跃任务" in b for b in self.plan()["blockers"]))
        self.assertEqual(self.cli("legacy", "link", "--legacy", str(self.legacy), "--apply").returncode, 3)
        self.assertEqual(snapshot(self.legacy), before)

    def test_unreadable_registry_fails_closed(self):
        (self.state / "projects.json").write_text("{not json")
        plan = self.plan()
        self.assertTrue(any("不能确认没有活跃任务" in b for b in plan["blockers"]))
        self.assertEqual(self.cli("legacy", "link", "--legacy", str(self.legacy), "--apply").returncode, 3)

    def test_failure_at_every_step_rolls_back_completely(self):
        before = snapshot(self.home)
        for fail_after in range(0, 4):
            with self.assertRaises(OSError) as ctx:
                legacy.apply_link(self.plan(), _fail_after=fail_after)
            self.assertIn("已回滚", str(ctx.exception))
            self.assertEqual(snapshot(self.home), before, f"fail_after={fail_after}")
        legacy.apply_link(self.plan())  # and it still works afterwards
        self.assertTrue((self.legacy / "scripts").is_symlink())

    def test_legacy_inside_repo_is_refused(self):
        self.assertTrue(legacy.plan_link(ROOT, self.config)["blockers"])


class UnlinkTests(Base):
    def test_unlink_restores_original_tree(self):
        before = snapshot(self.legacy)
        backup = legacy.apply_link(self.plan())
        self.assertTrue(backup.is_dir())
        done = self.cli("legacy", "unlink", "--legacy", str(self.legacy), "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        after = {k: v for k, v in snapshot(self.legacy).items() if not k.startswith(backup.name)}
        self.assertEqual(after, before)
        self.assertTrue((backup / "MANIFEST.restored.json").is_file())  # backup directory is kept, never deleted
        legacy.apply_link(self.plan())  # linking again works, and unlink then finds exactly the new backup
        self.assertEqual(len(legacy.find_backups(self.legacy)), 1)

    def test_unlink_refuses_foreign_link_and_active_task(self):
        legacy.apply_link(self.plan())
        link = self.legacy / "scripts"
        link.unlink()
        link.symlink_to(self.home)
        self.assertTrue(any("拒绝回滚" in b for b in legacy.plan_unlink(self.legacy, None, self.config)["blockers"]))
        link.unlink()
        link.symlink_to(ROOT / "scripts")
        self.start_task()
        self.assertEqual(self.cli("legacy", "unlink", "--legacy", str(self.legacy), "--apply").returncode, 3)
        self.assertTrue((self.legacy / "scripts").is_symlink())


class ServiceHardeningTests(Base):
    def setUp(self):
        super().setUp()
        from unittest import mock
        platform = mock.patch.object(service.sys, "platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

    def test_apply_install_rechecks_tasks_and_registry(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / "cfg")}):
            plan = service.plan_install(None, python="/usr/bin/python3", root=ROOT, listening=lambda p: False)
            self.assertEqual(plan["blockers"], [])
            self.start_task()
            calls = []
            with self.assertRaises(PermissionError):
                service.apply_install(plan, run=lambda *a, **k: calls.append(a))
            self.assertEqual(calls, [])
            self.assertFalse((self.home / "cfg/systemd").exists())
            (self.state / "projects.json").write_text("garbage")
            with self.assertRaises(PermissionError):
                service.apply_install(plan, run=lambda *a, **k: calls.append(a))
            plan2 = service.plan_uninstall()
            self.assertTrue(any("不能确认" in b for b in plan2["blockers"]))

    def test_apply_install_reports_invalid_source_config(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / "cfg")}):
            plan = service.plan_install(None, python="/usr/bin/python3", root=ROOT, listening=lambda p: False)
            self.config.write_text("{broken")
            with self.assertRaisesRegex(ValueError, "源配置.*无效"):
                service.apply_install(plan, run=lambda *a, **k: None)

    def test_unreadable_state_file_counts_as_active(self):
        self.start_task()
        (self.home / "proj/work/kiro-runs/r1/state.json").write_text("{bad")
        active, _ = service.find_active_tasks(self.config)
        self.assertEqual(len(active), 1)


class CliErrorTests(Base):
    def test_settings_permission_problem_is_friendly(self):
        blocker = self.home / "blocker"
        blocker.write_text("x")  # a file where the config directory should be: mkdir fails with an OSError
        env = {**self.env, "CODER_AGENTS_SETTINGS": str(blocker / "sub/settings.json")}
        done = subprocess.run([sys.executable, "-m", "coder_agents", "settings", "init", "--apply"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("设置操作失败", done.stderr)

    def test_serve_port_validated_before_any_config_is_created(self):
        fresh = self.home / "fresh/config.json"
        env = {**self.env, "CODER_AGENTS_CONFIG": str(fresh)}
        for port in ("80", "70000"):
            done = subprocess.run([sys.executable, "-m", "coder_agents", "serve", "--port", port], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 2, done.stderr)
        self.assertFalse(fresh.parent.exists())


class PackagingTests(unittest.TestCase):
    """A wheel must carry scripts/assets and run after installation; a tree missing them must not build."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tmpdirs.TemporaryDirectory(prefix="coder-agents-wheel-")
        cls.src = Path(cls.tmp.name) / "src"
        cls.src.mkdir()
        for name in ("coder_agents", "crewpane", "scripts", "assets", "examples"):
            shutil.copytree(ROOT / name, cls.src / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "docs", cls.src / "docs")
        for name in ("setup.py", "pyproject.toml", "README.md", "MANIFEST.in", "THIRD_PARTY_NOTICES.md", ".env.example"):
            shutil.copy2(ROOT / name, cls.src / name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, src, out):
        return subprocess.run([sys.executable, "setup.py", "-q", "bdist_wheel", "-d", str(out)], cwd=src, capture_output=True,
                              text=True, timeout=300)

    def test_wheel_contains_runtime_and_runs_after_install(self):
        out = Path(self.tmp.name) / "dist"
        done = self.build(self.src, out)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)  # missing setuptools/wheel is a failure, not a skip
        wheel = next(out.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            for rel in ("coder_agents/_runtime/scripts/watch_kiro.py", "coder_agents/_runtime/assets/viewer.html",
                        "coder_agents/_runtime/assets/vendor/purify.min.js", "coder_agents/_runtime/assets/vendor/marked.LICENSE.txt",
                        "coder_agents/_runtime/examples/settings.example.json", "crewpane/__main__.py"):
                self.assertIn(rel, names)
            from coder_agents import MODULES
            self.assertEqual(len([n for n in names if n.startswith("coder_agents/_runtime/scripts/") and n.endswith(".py")]), len(MODULES))
            mode = archive.getinfo("coder_agents/_runtime/scripts/kiro_acp_relay.py").external_attr >> 16
            self.assertTrue(mode & 0o111, "relay must stay executable")
            target = Path(self.tmp.name) / "site"
            archive.extractall(target)
            for info in archive.infolist():  # what pip does: restore the recorded executable bit
                if info.external_attr >> 16 & 0o111:
                    os.chmod(target / info.filename, 0o755)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(target), "PYTHONDONTWRITEBYTECODE": "1", "HOME": self.tmp.name}
        check = subprocess.run([sys.executable, "-m", "crewpane", "selfcheck"], cwd=self.tmp.name, env=env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(str(target), check.stdout)  # it used the installed copy, not the checkout

    def test_wheel_built_from_unpacked_sdist_runs_selfcheck(self):
        """pip builds wheels from the sdist, so the sdist itself must carry every runtime file and no personal/run data."""
        tree = Path(self.tmp.name) / "sdist-src"
        shutil.copytree(self.src, tree)
        for rel in ("work/task.log", "settings.json", "config.json", ".env", "node_modules/x/index.js", "scripts/__pycache__/a.pyc"):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text("secret")
        dist = Path(self.tmp.name) / "sdist-dist"
        done = subprocess.run([sys.executable, "setup.py", "-q", "sdist", "-d", str(dist)], cwd=tree, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        archive_path = next(dist.glob("*.tar.gz"))
        with tarfile.open(archive_path) as archive:
            names = [n.split("/", 1)[1] for n in archive.getnames() if "/" in n]
            archive.extractall(Path(self.tmp.name) / "sdist-unpacked")
        for rel in ("scripts/watch_kiro.py", "scripts/kiro_acp_relay.py", "assets/viewer.html", "assets/vendor/marked.umd.js",
                    "assets/vendor/purify.min.js", "assets/vendor/marked.LICENSE.txt", "assets/vendor/dompurify.LICENSE-Apache-2.0.txt",
                    "assets/vendor/dompurify.LICENSE-MPL-2.0.txt", "assets/vendor/README.md", "examples/settings.example.json",
                    "README.md", "THIRD_PARTY_NOTICES.md", "docs/architecture.md", "coder_agents/cli.py", "crewpane/__main__.py", "MANIFEST.in"):
            self.assertIn(rel, names)
        from coder_agents import MODULES
        self.assertEqual(len([n for n in names if n.startswith("scripts/") and n.endswith(".py")]), len(MODULES))
        for name in names:
            base = name.rsplit("/", 1)[-1]
            self.assertFalse(name.startswith(("work/", "node_modules/", "tests/")) or "__pycache__" in name or base in
                             ("settings.json", "config.json", ".env") or base.endswith((".log", ".pyc")), name)
        unpacked = next((Path(self.tmp.name) / "sdist-unpacked").iterdir())
        out = Path(self.tmp.name) / "sdist-wheel"
        built = self.build(unpacked, out)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        target = Path(self.tmp.name) / "sdist-site"
        with zipfile.ZipFile(next(out.glob("*.whl"))) as wheel:
            wheel.extractall(target)
            for info in wheel.infolist():
                if info.external_attr >> 16 & 0o111:
                    os.chmod(target / info.filename, 0o755)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(target), "PYTHONDONTWRITEBYTECODE": "1", "HOME": self.tmp.name}
        check = subprocess.run([sys.executable, "-m", "coder_agents", "selfcheck"], cwd=self.tmp.name, env=env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(str(target), check.stdout)

    def test_build_refuses_when_runtime_files_are_missing(self):
        broken = Path(self.tmp.name) / "broken"
        shutil.copytree(self.src, broken)
        shutil.rmtree(broken / "scripts")
        done = self.build(broken, Path(self.tmp.name) / "dist2")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Refusing to build", done.stdout + done.stderr)
        self.assertEqual(list((Path(self.tmp.name) / "dist2").glob("*.whl")) if (Path(self.tmp.name) / "dist2").exists() else [], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
