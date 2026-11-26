"""Backend tests for the read-only review API (kiro_review + Panel.review + HTTP routes).

Every fixture lives in a temporary directory in a throwaway system temp directory and is removed afterwards.
No model call is made; Git runs only inside those fixtures."""
import atexit
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import tmpdirs
import threading
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import kiro_review  # noqa: E402
import watch_kiro  # noqa: E402

HERE = Path(__file__).resolve().parent
TMP_BASE = Path(tmpdirs.mkdtemp("coder-agents-tests-"))
atexit.register(shutil.rmtree, TMP_BASE, ignore_errors=True)

GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=check, capture_output=True, text=True)


def write(path, text, mode="w"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as stream:
        stream.write(text)


class Fixture:
    """A repository with every kind of change, plus hostile configuration that must never run."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="fx-")).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.marker = self.base / "EXECUTED"
        r = self.repo
        git(r, "init", "-q", "-b", "main")
        for name, text in {"a.txt": "one\ntwo\nthree\n", "b.txt": "b1\nb2\n", "del.txt": "gone\nsoon\n", "d2.txt": "x\ny\n",
                           "old_name.txt": "rename me\nline2\nline3\nline4\n", "sub/c.txt": "c1\n", "sub/deep/inner.txt": "i1\n",
                           "other/o.txt": "o1\n", ".env": "SECRET=1\n", ".gitignore": "ignored.log\n"}.items():
            write(r / name, text)
        write(r / "bin.dat", b"\0\1\2\3".decode("latin1"), "w")
        (r / "bin.dat").write_bytes(b"\0\1\2\3")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "init")
        write(r / ".gitattributes", "* filter=evil\n*.txt diff=evil\n")
        git(r, "add", ".gitattributes")
        git(r, "commit", "-qm", "attrs")
        # staged
        write(r / "a.txt", "one\nTWO\nthree\nfour\n")
        write(r / "n.txt", "new1\nnew2\n")
        git(r, "add", "a.txt", "n.txt")
        git(r, "mv", "old_name.txt", "new_name.txt")
        git(r, "rm", "-q", "del.txt")
        write(r / "sub/c.txt", "c1\nc2\n")
        git(r, "add", "sub/c.txt")
        # unstaged on top of staged
        write(r / "a.txt", "one\nTWO\nthree\nfour\nfive\n")
        write(r / "b.txt", "b1\nB2\nb3\n")
        os.remove(r / "d2.txt")
        (r / "bin.dat").write_bytes(b"\0\1\2\3\4\5")
        write(r / "sub/deep/inner.txt", "i1\ni2\n")
        write(r / "other/o.txt", "o1\no2\n")
        write(r / ".env", "SECRET=2\n")
        # untracked
        write(r / "u.txt", "u1\nu2\nu3\n")
        write(r / "sub/u2.txt", "s1\n")
        write(r / "<img src=x onerror=alert(1)>.txt", "<script>alert(1)</script>\n")
        write(r / "ignored.log", "x\n")
        write(r / "big.txt", "x" * (kiro_review.MAX_FILE_BYTES + 10))
        (r / "bin.bin").write_bytes(b"\0\0\0\1")
        write(r / "secrets.json", "{}")
        write(r / "credentials", "k")
        (r / "latin.txt").write_bytes(b"caf\xe9\n")
        os.symlink("/etc/hostname", r / "link_out")
        os.symlink("a.txt", r / "link_in")
        os.symlink("../outside_dir", r / "sub/escape")
        (self.base / "outside_dir").mkdir()
        write(self.base / "outside_dir/secret.txt", "TOP SECRET\n")
        # hostile repository configuration, installed last: every command below leaves a marker if it ever runs
        marker = str(self.marker)
        write(r / ".git/info/attributes", "sub/* filter=evil\n")
        for key, value in {"filter.evil.clean": "sh -c 'echo clean >> %s; cat'" % marker, "filter.evil.smudge": "cat",
                           "filter.evil.process": "sh -c 'echo process >> %s'" % marker,
                           "diff.evil.textconv": "sh -c 'echo textconv >> %s; cat' x" % marker,
                           "diff.external": "sh -c 'echo ext >> %s' x" % marker,
                           "core.fsmonitor": "sh -c 'echo fsmonitor >> %s'" % marker,
                           "core.pager": "sh -c 'echo pager >> %s; cat'" % marker,
                           "core.hooksPath": str(self.base / "hooks")}.items():
            git(r, "config", key, value)
        write(self.base / "hooks/pre-commit", "#!/bin/sh\necho hook >> %s\n" % marker)
        self.marker.unlink(missing_ok=True)

    def close(self):
        shutil.rmtree(self.base, ignore_errors=True)


def by_path(area):
    return {e["path"]: e for e in area["entries"]}


class DirectReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.cwd = str(cls.fx.repo)
        cls.status = kiro_review.git_status(cls.cwd)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_status_separates_index_and_working_tree(self):
        s = self.status
        self.assertEqual(s["kind"], "git")
        self.assertEqual(s["repo"]["branch"], "main")
        self.assertFalse(s["repo"]["detached"])
        staged, unstaged, untracked = (by_path(s["areas"][k]) for k in ("staged", "unstaged", "untracked"))
        # a.txt: staged +2 -1 (TWO, four), unstaged +1 (five)
        self.assertEqual((staged["a.txt"]["added"], staged["a.txt"]["deleted"]), (2, 1))
        self.assertEqual((unstaged["a.txt"]["added"], unstaged["a.txt"]["deleted"]), (1, 0))
        self.assertEqual(staged["n.txt"]["code"], "A")
        self.assertEqual(staged["del.txt"]["code"], "D")
        self.assertEqual(staged["new_name.txt"]["code"], "R")
        self.assertEqual(staged["new_name.txt"]["old_path"], "old_name.txt")
        self.assertNotIn("b.txt", staged)
        self.assertEqual((unstaged["b.txt"]["added"], unstaged["b.txt"]["deleted"]), (2, 1))
        self.assertEqual(unstaged["d2.txt"]["code"], "D")
        self.assertEqual(unstaged["d2.txt"]["deleted"], 2)
        self.assertNotIn("n.txt", unstaged)

    def test_binary_has_no_invented_counts(self):
        entry = by_path(self.status["areas"]["unstaged"])["bin.dat"]
        self.assertTrue(entry["binary"])
        self.assertIsNone(entry["added"])
        self.assertIsNone(entry["deleted"])
        untracked = by_path(self.status["areas"]["untracked"])["bin.bin"]
        self.assertTrue(untracked["binary"])
        self.assertIsNone(untracked["added"])

    def test_untracked_lines_and_ignored(self):
        untracked = by_path(self.status["areas"]["untracked"])
        self.assertEqual(untracked["u.txt"]["added"], 3)
        self.assertEqual(untracked["sub/u2.txt"]["added"], 1)
        self.assertNotIn("ignored.log", untracked)
        self.assertIsNone(untracked["big.txt"]["added"])  # too large to count, and not invented
        self.assertTrue(untracked["big.txt"].get("note"))

    def test_sensitive_paths_hidden_everywhere(self):
        names = {e["path"] for k in self.status["areas"].values() for e in k["entries"]}
        for secret in (".env", "secrets.json", "credentials"):
            self.assertNotIn(secret, names)
        self.assertGreaterEqual(self.status["areas"]["unstaged"]["hidden"], 1)  # tracked .env modified
        self.assertGreaterEqual(self.status["areas"]["untracked"]["hidden"], 2)

    def test_hostile_repository_config_never_runs(self):
        kiro_review.git_status(self.cwd)
        for area, rel in (("staged", "a.txt"), ("unstaged", "a.txt"), ("unstaged", "sub/deep/inner.txt"), ("staged", "sub/c.txt")):
            result = kiro_review.git_diff(self.cwd, area, rel)
            self.assertEqual(result["state"], "ok", result)
            self.assertIn("@@", result["diff"])
        self.assertFalse(self.fx.marker.exists(), self.fx.marker.read_text() if self.fx.marker.exists() else "")

    def test_diffs_by_area(self):
        staged = kiro_review.git_diff(self.cwd, "staged", "a.txt")
        self.assertIn("+four", staged["diff"])
        self.assertNotIn("+five", staged["diff"])
        unstaged = kiro_review.git_diff(self.cwd, "unstaged", "a.txt")
        self.assertIn("+five", unstaged["diff"])
        self.assertNotIn("+four\n+five", staged["diff"])
        rename = kiro_review.git_diff(self.cwd, "staged", "new_name.txt")
        self.assertEqual(rename["code"], "R")
        self.assertIn("rename from old_name.txt", rename["diff"])
        self.assertEqual(rename["unchanged"], (self.fx.repo / "new_name.txt").read_text())
        deleted = kiro_review.git_diff(self.cwd, "staged", "del.txt")
        self.assertIn("-gone", deleted["diff"])
        untracked = kiro_review.git_diff(self.cwd, "untracked", "u.txt")
        self.assertEqual(untracked["added"], 3)
        self.assertIn("+u2", untracked["diff"])
        binary = kiro_review.git_diff(self.cwd, "unstaged", "bin.dat")
        self.assertTrue(binary["binary"])
        self.assertEqual(binary["diff"], "")

    def test_diff_keeps_the_entire_file_in_staged_and_unstaged_views(self):
        root = self.fx.base / "whole-diff"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        lines = ["context %04d\n" % i for i in range(7200)]
        write(root / "complete.txt", "".join(lines))
        git(root, "add", "complete.txt")
        git(root, "commit", "-qm", "complete", "--", "complete.txt")
        lines[3400] = "staged change\n"
        write(root / "complete.txt", "".join(lines))
        git(root, "add", "complete.txt")
        lines[3900] = "unstaged change\n"
        write(root / "complete.txt", "".join(lines))
        staged = kiro_review.git_diff(str(root), "staged", "complete.txt")
        unstaged = kiro_review.git_diff(str(root), "unstaged", "complete.txt")
        for result in (staged, unstaged):
            self.assertFalse(result["truncated"])
            self.assertIn("\n context 0000\n", result["diff"])
            self.assertTrue(result["diff"].endswith("\n context 7199\n"))
            self.assertEqual((result["added"], result["deleted"]), (1, 1))
        self.assertIn("\n+staged change\n", staged["diff"])
        self.assertNotIn("unstaged change", staged["diff"])
        self.assertIn("\n staged change\n", unstaged["diff"])
        self.assertIn("\n+unstaged change\n", unstaged["diff"])

    def test_diff_only_for_listed_files(self):
        self.assertEqual(kiro_review.git_diff(self.cwd, "staged", "b.txt")["state"], "missing")  # only unstaged
        self.assertEqual(kiro_review.git_diff(self.cwd, "untracked", "a.txt")["state"], "missing")
        with self.assertRaises(kiro_review.ReviewError):
            kiro_review.git_diff(self.cwd, "unstaged", "../outside_dir/secret.txt")
        with self.assertRaises(kiro_review.ReviewError):
            kiro_review.git_diff(self.cwd, "unstaged", ".env")
        with self.assertRaises(kiro_review.ReviewError):
            kiro_review.git_diff(self.cwd, "bogus", "a.txt")
        # a leading dash is just a (missing) file name, never an option
        self.assertEqual(kiro_review.git_diff(self.cwd, "unstaged", "--output=/tmp/x")["state"], "missing")
        # magic pathspecs are literal
        self.assertEqual(kiro_review.git_diff(self.cwd, "unstaged", ":(top)*")["state"], "missing")

    def test_subdirectory_scope_is_not_widened(self):
        sub = str(self.fx.repo / "sub")
        status = kiro_review.git_status(sub)
        self.assertEqual(status["repo"]["scope"], "sub")
        self.assertEqual(status["repo"]["root"], str(self.fx.repo))
        staged, unstaged, untracked = (by_path(status["areas"][k]) for k in ("staged", "unstaged", "untracked"))
        self.assertEqual(set(staged), {"c.txt"})
        self.assertEqual(set(unstaged), {"deep/inner.txt"})
        self.assertEqual(set(untracked), {"u2.txt", "escape"})  # "escape" is a symlink: listed by git, never read
        self.assertTrue(untracked["escape"].get("note"))
        self.assertNotIn("a.txt", staged)
        self.assertNotIn("../a.txt", staged)
        self.assertEqual(kiro_review.git_diff(sub, "staged", "c.txt")["state"], "ok")
        self.assertEqual(kiro_review.git_diff(sub, "staged", "n.txt")["state"], "missing")
        with self.assertRaises(kiro_review.ReviewError):
            kiro_review.clean_relative(sub, "../a.txt")
        with self.assertRaises(kiro_review.ReviewError):
            kiro_review.clean_relative(sub, str(self.fx.repo / "a.txt"))

    def test_read_file_states(self):
        cwd = self.cwd
        ok = kiro_review.read_text(cwd, "a.txt")
        self.assertEqual((ok["state"], ok["lines"]), ("ok", 5))
        self.assertEqual(kiro_review.read_text(cwd, "nope.txt")["state"], "missing")
        self.assertEqual(kiro_review.read_text(cwd, "d2.txt")["state"], "missing")  # deleted in the work tree
        self.assertEqual(kiro_review.read_text(cwd, "bin.bin")["state"], "binary")
        self.assertEqual(kiro_review.read_text(cwd, "latin.txt")["state"], "binary")
        self.assertEqual(kiro_review.read_text(cwd, "big.txt")["state"], "too_large")
        self.assertNotIn("text", kiro_review.read_text(cwd, "big.txt"))
        self.assertEqual(kiro_review.read_text(cwd, "sub")["state"], "directory")
        self.assertEqual(kiro_review.read_text(cwd, "link_out")["state"], "symlink")
        self.assertEqual(kiro_review.read_text(cwd, "link_in")["state"], "symlink")
        self.assertEqual(kiro_review.read_text(cwd, "sub/escape/secret.txt")["state"], "symlink")
        fifo = self.fx.repo / "fifo"
        os.mkfifo(fifo)
        self.assertEqual(kiro_review.read_text(cwd, "fifo")["state"], "not_file")

    def test_hostile_paths_refused(self):
        cwd = self.cwd
        for bad in ("../outside_dir/secret.txt", "sub/../../x", "/etc/passwd", "a\0b", "", ".git/config", ".git", "sub/.git/HEAD",
                    ".env", ".env.local", ".aws/credentials", ".ssh/id_rsa", ".kiro/settings/mcp.json", "x/.SSH/known_hosts",
                    "id_ed25519", "server.pem", "credentials.json", "terraform.tfstate", ".git-credentials", ".netrc"):
            with self.assertRaises(kiro_review.ReviewError, msg=bad):
                kiro_review.clean_relative(cwd, bad)
        self.assertEqual(kiro_review.clean_relative(cwd, "./sub//c.txt"), "sub/c.txt")
        self.assertEqual(kiro_review.clean_relative(cwd, cwd + "/sub/c.txt"), "sub/c.txt")
        self.assertEqual(kiro_review.clean_relative(cwd, "/logical/p/a.txt", logical="/logical/p"), "a.txt")
        with self.assertRaises(kiro_review.ReviewError):
            kiro_review.clean_relative(cwd, cwd + "/../outside_dir/secret.txt")

    def test_authorised_directory(self):
        for bad in (None, "relative", "/", "/nonexistent-dir-xyz", "/var/empty/x/.ssh", str(self.fx.repo / ".git")):
            with self.assertRaises(kiro_review.ReviewError, msg=bad):
                kiro_review.authorised_directory(bad)
        self.assertEqual(kiro_review.authorised_directory(str(self.fx.repo)), self.cwd)

    def test_xss_names_stay_data(self):
        names = by_path(self.status["areas"]["untracked"])
        hostile = "<img src=x onerror=alert(1)>.txt"
        self.assertIn(hostile, names)
        self.assertIn("<script>", kiro_review.read_text(self.cwd, hostile)["text"])  # returned raw as JSON data

    def test_output_cap_and_timeout(self):
        big = self.fx.base / "bigdiff"
        big.mkdir()
        git(big, "init", "-q", "-b", "main")
        write(big / "f.txt", "a\n")
        git(big, "add", "-A")
        git(big, "commit", "-qm", "i")
        write(big / "f.txt", "".join("line %d some padding text to make it long\n" % i for i in range(130000)))
        result = kiro_review.git_diff(str(big), "unstaged", "f.txt")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["diff"].encode()), kiro_review.MAX_DIFF_BYTES)
        # a hung git process is killed at the deadline and its group with it
        started = time.monotonic()
        result = kiro_review.run_git(self.cwd, ["hash-object", "--stdin-paths"], timeout=0.5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(result.timed_out or result.code is not None)

    def test_oversized_listing_is_partial_not_an_error(self):
        many = self.fx.base / "many"
        many.mkdir()
        git(many, "init", "-q", "-b", "main")
        for index in range(60):
            write(many / ("file-%03d.txt" % index), "x\n")
        original = kiro_review.MAX_LIST_BYTES
        kiro_review.MAX_LIST_BYTES = 300
        try:
            status = kiro_review.git_status(str(many))
        finally:
            kiro_review.MAX_LIST_BYTES = original
        untracked = status["areas"]["untracked"]
        self.assertTrue(untracked["truncated"])
        self.assertIsNone(untracked["error"])
        self.assertTrue(0 < len(untracked["entries"]) < 60)
        for entry in untracked["entries"]:
            self.assertRegex(entry["path"], r"^file-\d{3}\.txt$")  # no half-cut name

    def test_timeout_kills_process(self):
        script = self.fx.base / "fakegit.sh"
        write(script, "#!/bin/sh\nsleep 30\n")
        os.chmod(script, 0o755)
        original = kiro_review.GIT
        kiro_review.GIT = str(script)
        try:
            started = time.monotonic()
            result = kiro_review.run_git(self.cwd, ["status"], timeout=0.6)
        finally:
            kiro_review.GIT = original
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 5)

    def snapshot(self):
        root = self.fx.repo / ".git"
        return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file())

    def test_no_write_operations_changed_the_repository(self):
        before = self.snapshot()
        kiro_review.git_status(self.cwd)
        kiro_review.git_diff(self.cwd, "unstaged", "a.txt")
        kiro_review.git_diff(self.cwd, "staged", "new_name.txt")
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.fx.repo / ".git/index.lock").exists())

    def test_zz_control_plain_git_would_have_run_the_hostile_config(self):
        self.fx.marker.unlink(missing_ok=True)
        subprocess.run(["git", "diff", "--stat"], cwd=self.cwd, env=GIT_ENV, capture_output=True)
        self.assertTrue(self.fx.marker.exists(), "fixture is not hostile, the no-execution tests prove nothing")
        self.fx.marker.unlink()


class OtherRepositories(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="or-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_non_git_directory(self):
        write(self.base / "x.txt", "hello\n")
        status = kiro_review.git_status(str(self.base))
        self.assertEqual(status["kind"], "nogit")
        self.assertEqual(status["areas"], {})
        self.assertEqual(kiro_review.read_text(str(self.base), "x.txt")["state"], "ok")

    def test_detached_head_and_unborn(self):
        repo = self.base / "r"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        unborn = kiro_review.git_status(str(repo))
        self.assertTrue(unborn["repo"]["unborn"])
        self.assertEqual(unborn["repo"]["branch"], "main")
        write(repo / "f.txt", "1\n")
        git(repo, "add", "f.txt")
        unborn = kiro_review.git_status(str(repo))
        self.assertEqual(by_path(unborn["areas"]["staged"])["f.txt"]["added"], 1)
        git(repo, "commit", "-qm", "c")
        git(repo, "checkout", "-q", "--detach")
        detached = kiro_review.git_status(str(repo))["repo"]
        self.assertTrue(detached["detached"])
        self.assertEqual(detached["branch"], "")
        self.assertRegex(detached["head"], r"^[0-9a-f]{7,12}$")

    def test_core_worktree_redirect_refused(self):
        repo = self.base / "r"
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        write(elsewhere / "x.txt", "x\n")
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "core.worktree", str(elsewhere))
        status = kiro_review.git_status(str(repo / ".git"))
        self.assertNotEqual(status["kind"], "git")
        status = kiro_review.git_status(str(repo))
        self.assertNotEqual(status["kind"], "git")

    def test_git_dir_itself_refused(self):
        repo = self.base / "r"
        repo.mkdir()
        git(repo, "init", "-q")
        self.assertNotEqual(kiro_review.git_status(str(repo / ".git"))["kind"], "git")

    def test_nested_repository_is_not_followed(self):
        outer = self.base / "outer"
        outer.mkdir()
        git(outer, "init", "-q", "-b", "main")
        inner = outer / "inner"
        inner.mkdir()
        git(inner, "init", "-q")
        write(inner / "z.txt", "z\n")
        status = kiro_review.git_status(str(outer))
        self.assertEqual(status["areas"]["untracked"]["entries"], [])


# ---- Panel + HTTP ----------------------------------------------------------------------------------------
def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.base = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="panel-")).resolve()
        cls.second = cls.base / "second"
        cls.second.mkdir()
        write(cls.second / "only_here.txt", "second source\n")
        cls.logs = cls.base / "logs"
        cls.logs2 = cls.base / "logs2"
        cls.port = free_port()
        config = {"port": cls.port, "token": "t" * 32, "default_project": {"cwd": str(cls.fx.repo), "root": str(cls.logs)}}
        (cls.base / "cfg").mkdir()
        cls.panel = watch_kiro.Panel(config, cls.base / "cfg/config.json", codex_state=cls.base / "codex.json",
                                     daily_base=cls.base / "daily", models=object(), usage=object())
        cls.source = cls.panel.source_by_id(cls.panel.default_id)
        cls.panel.register({"cwd": str(cls.second), "root": str(cls.logs2)})
        cls.source2 = cls.panel.source_by_id(cls.panel.default_id)
        cls.make_run(cls.source, "20300101-000000-aaaaaaaa", "agent-one", str(cls.fx.repo))
        cls.make_run(cls.source, "20300101-000001-bbbbbbbb", "agent-sub", str(cls.fx.repo / "sub"))
        cls.make_run(cls.source, "20300101-000002-cccccccc", "agent-gone", str(cls.base / "vanished"))
        cls.make_run(cls.source2, "20300101-000003-dddddddd", "agent-one", str(cls.second))  # same agent ID, other source
        cls.make_run(cls.source, "20300101-000004-eeeeeeee", "agent-root", "/")
        handler = watch_kiro.make_handler(cls.panel)
        cls.server = watch_kiro.ThreadingHTTPServer(("127.0.0.1", cls.port), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def make_run(cls, source, run_id, agent, cwd):
        directory = source.root / run_id
        directory.mkdir(parents=True)
        write(directory / "state.json", json.dumps({"id": run_id, "agent_id": agent, "cwd": cwd, "status": "completed",
                                                    "started_at": "2030-01-01T00:00:00+00:00", "title": agent}))
        write(directory / "events.jsonl", "")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.fx.close()
        shutil.rmtree(cls.base, ignore_errors=True)

    def group(self, source):
        return self.panel.group_of(source, self.panel.catalog.snapshot())

    def ask(self, kind, source, agent, group=None, **extra):
        query = {"group": group or self.group(source), "source": source.id, "agent": agent, **extra}
        return self.panel.review(kind, query)

    def test_status_via_panel(self):
        code, body = self.ask("status", self.source, "agent-one")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["kind"], "git")
        self.assertEqual(body["agent"], "agent-one")
        self.assertEqual(body["source"], self.source.id)

    def test_conversation_cwd_in_subdirectory_limits_scope(self):
        code, body = self.ask("status", self.source, "agent-sub")
        self.assertEqual(code, 200)
        self.assertEqual(body["repo"]["scope"], "sub")
        self.assertEqual(set(by_path(body["areas"]["staged"])), {"c.txt"})
        code, body = self.ask("file", self.source, "agent-sub", path="a.txt")
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "missing")  # sub/a.txt does not exist; the repo-level a.txt is out of scope
        code, body = self.ask("file", self.source, "agent-sub", path=str(self.fx.repo / "a.txt"))
        self.assertEqual(code, 403)

    def test_same_agent_id_in_two_sources_is_not_mixed(self):
        _, one = self.ask("file", self.source, "agent-one", path="a.txt")
        self.assertEqual(one["state"], "ok")
        code, two = self.ask("file", self.source2, "agent-one", path="only_here.txt")
        self.assertEqual((code, two["state"], two["text"]), (200, "ok", "second source\n"))
        code, wrong = self.ask("file", self.source2, "agent-one", path="a.txt")
        self.assertEqual(wrong["state"], "missing")  # the other source's file is not reachable here

    def test_identity_triple_must_agree(self):
        self.assertEqual(self.ask("status", self.source, "agent-one", group="codex:other")[0], 409)
        self.assertEqual(self.ask("status", self.source, "agent-none")[0], 404)
        code, _ = self.panel.review("status", {"group": self.group(self.source), "source": "0" * 16, "agent": "agent-one"})
        self.assertEqual(code, 404)
        for query in ({"group": "", "source": self.source.id, "agent": "agent-one"},
                      {"group": "other", "source": "../x", "agent": "agent-one"},
                      {"group": "other", "source": self.source.id, "agent": "../../etc"},
                      {"group": "other", "source": self.source.id, "agent": ""}):
            self.assertEqual(self.panel.review("status", query)[0], 400, query)
        # an agent of another source is not reachable through this source
        self.assertEqual(self.ask("status", self.source2, "agent-sub")[0], 404)

    def test_missing_and_root_cwd_refused(self):
        self.assertEqual(self.ask("status", self.source, "agent-gone")[0], 404)
        self.assertEqual(self.ask("status", self.source, "agent-root")[0], 403)

    def test_client_cannot_name_cwd_root_or_ref(self):
        code, body = self.ask("status", self.source, "agent-one", cwd="/etc", root="/", ref="HEAD~5", cached="1")
        self.assertEqual(code, 200)
        self.assertEqual(body["cwd"], str(self.fx.repo))

    def test_credential_files_refused_via_panel(self):
        for rel in (".env", ".git/config", "../outside_dir/secret.txt", "sub/escape/secret.txt", "secrets.json"):
            code, body = self.ask("file", self.source, "agent-one", path=rel)
            self.assertIn(code, (200, 403), rel)
            self.assertNotIn("text", body, rel)
            self.assertNotIn("TOP SECRET", json.dumps(body), rel)
        code, body = self.ask("diff", self.source, "agent-one", area="unstaged", path=".env")
        self.assertEqual(code, 403)

    def test_error_messages_do_not_echo_git_stderr(self):
        original = kiro_review.run_git
        def boom(*args, **kwargs):
            raise OSError("fatal: could not read https://user:hunter2@example.com/x.git")
        kiro_review.run_git = boom
        try:
            code, body = self.ask("status", self.source, "agent-one")
        finally:
            kiro_review.run_git = original
        self.assertEqual(code, 500)
        self.assertNotIn("hunter2", json.dumps(body))

    def test_concurrency_limit(self):
        taken = [self.panel.review_slots.acquire(blocking=False) for _ in range(3)]
        try:
            self.assertEqual(self.ask("status", self.source, "agent-one")[0], 429)
        finally:
            for _ in taken:
                self.panel.review_slots.release()
        self.assertEqual(self.ask("status", self.source, "agent-one")[0], 200)

    # --- HTTP --------------------------------------------------------------------------------------------
    def request(self, path, host=None, cookie=True, origin=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        if cookie:
            headers["Cookie"] = "%s=%s" % (self.panel.cookie_name, self.panel.cookie_value)
        if origin:
            headers["Origin"] = origin
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_http_routes_keep_host_origin_cookie_rules(self):
        query = "?group=%s&source=%s&agent=agent-one" % (self.group(self.source), self.source.id)
        status, body = self.request("/api/review" + query)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["kind"], "git")
        self.assertEqual(self.request("/api/review" + query, cookie=False)[0], 401)
        self.assertEqual(self.request("/api/review" + query, host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(self.request("/api/review" + query, origin="http://evil.example")[0], 403)
        self.assertEqual(self.request("/api/review" + query, method="POST")[0], 404)
        status, body = self.request("/api/review/file" + query + "&path=a.txt")
        self.assertEqual(json.loads(body)["state"], "ok")
        status, body = self.request("/api/review/diff" + query + "&area=staged&path=a.txt")
        self.assertIn("+four", json.loads(body)["diff"])
        status, body = self.request("/api/review/file" + query + "&path=..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(status, 403)
        self.assertEqual(self.request("/api/review/unknown" + query)[0], 404)


class DirectoryAnchoredRead(unittest.TestCase):
    """read_text walks directory descriptors, so a parent swapped for a symlink cannot redirect the read."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="dr-")).resolve()
        self.cwd = self.base / "cwd"
        write(self.cwd / "sub/f.txt", "original\n")
        write(self.base / "evil/f.txt", "attacker\n")
        os.link(self.cwd / "sub/f.txt", self.base / "evil/hard.txt")   # hard link of the very same inode

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_parent_already_a_symlink_is_refused_even_with_hardlinked_target(self):
        os.rename(self.cwd / "sub", self.base / "moved")
        os.symlink(self.base / "evil", self.cwd / "sub")
        for name in ("f.txt", "hard.txt"):
            result = kiro_review.read_text(str(self.cwd), "sub/" + name)
            self.assertEqual(result["state"], "symlink", name)
            self.assertNotIn("text", result)

    def test_parent_swapped_during_the_walk_reads_only_the_opened_directory(self):
        real_open, swapped = os.open, []

        def hostile_open(path, flags, *args, **kwargs):
            if path == "f.txt" and not swapped and kwargs.get("dir_fd") is not None:
                swapped.append(True)   # the walk already holds `sub`; now replace it by a symlink to the hardlink directory
                os.rename(self.cwd / "sub", self.base / "moved")
                os.symlink(self.base / "evil", self.cwd / "sub")
            return real_open(path, flags, *args, **kwargs)
        with unittest.mock.patch.object(os, "open", hostile_open):
            result = kiro_review.read_text(str(self.cwd), "sub/f.txt")
        self.assertTrue(swapped)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["text"], "original\n")   # never the attacker's file

    def test_final_component_symlink_and_nested_swap(self):
        write(self.cwd / "a/b/c.txt", "deep\n")
        os.symlink(self.cwd / "a/b/c.txt", self.cwd / "a/b/link.txt")
        self.assertEqual(kiro_review.read_text(str(self.cwd), "a/b/link.txt")["state"], "symlink")
        self.assertEqual(kiro_review.read_text(str(self.cwd), "a/b/c.txt/x")["state"], "missing")
        self.assertEqual(kiro_review.read_text(str(self.cwd), "a/b/c.txt")["text"], "deep\n")
        self.assertEqual(kiro_review.read_text(str(self.cwd), "a/zz/c.txt")["state"], "missing")

    def test_read_is_bounded(self):
        write(self.cwd / "big.txt", "x" * (kiro_review.MAX_FILE_BYTES + 5))
        self.assertEqual(kiro_review.read_text(str(self.cwd), "big.txt")["state"], "too_large")


class FilterFailClosed(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="ff-")).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo / "a.txt", "a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "i")
        write(self.repo / "a.txt", "a\nb\n")
        self.cwd = str(self.repo)
        self.calls = []
        self.real = kiro_review.run_git

        def spy(cwd, args, **kwargs):
            self.calls.append(list(args))
            return self.real(cwd, args, **kwargs)
        self.spy = spy

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def add_filters(self, count, start=0):
        lines = "".join("[filter \"f%d\"]\n\tclean = true\n" % i for i in range(start, start + count))
        with open(self.repo / ".git/config", "a") as stream:
            stream.write(lines)

    def ran_status_or_diff(self):
        return [c for c in self.calls if c and c[0] in ("diff", "ls-files")]

    def test_exactly_200_filters_work_and_201_are_refused_before_any_status_or_diff(self):
        self.add_filters(200)
        with unittest.mock.patch.object(kiro_review, "run_git", self.spy):
            ok = kiro_review.git_status(self.cwd)
        self.assertEqual(ok["kind"], "git")
        self.assertEqual(len(kiro_review.filter_overrides(self.cwd)), 200 * 8)
        self.add_filters(1, 200)
        self.calls.clear()
        with unittest.mock.patch.object(kiro_review, "run_git", self.spy):
            refused = kiro_review.git_status(self.cwd)
            with self.assertRaises(kiro_review.ReviewError) as caught:
                kiro_review.git_diff(self.cwd, "unstaged", "a.txt")
        self.assertEqual(refused["kind"], "failed")
        self.assertIn("201", refused["message"])
        self.assertIn("200", refused["message"])
        self.assertEqual(refused["areas"], {})
        self.assertIn("过滤器", caught.exception.message)
        self.assertEqual(self.ran_status_or_diff(), [])

    def test_config_failure_timeout_and_truncation_fail_closed(self):
        cases = {"error": kiro_review.GitResult(128, b"", err=b"fatal"), "timeout": kiro_review.GitResult(-9, b"", timed_out=True),
                 "truncated": kiro_review.GitResult(0, b"filter.f1.clean\0", truncated=True)}
        for name, bad in cases.items():
            self.calls.clear()

            def fake(cwd, args, _bad=bad, **kwargs):
                self.calls.append(list(args))
                return _bad if args[:1] == ["config"] else self.real(cwd, args, **kwargs)
            with unittest.mock.patch.object(kiro_review, "run_git", fake):
                status = kiro_review.git_status(self.cwd)
                with self.assertRaises(kiro_review.ReviewError):
                    kiro_review.git_diff(self.cwd, "unstaged", "a.txt")
            self.assertEqual(status["kind"], "failed", name)
            self.assertIn("已停止读取 Git 改动", status["message"], name)
            self.assertEqual(self.ran_status_or_diff(), [], name)

    def test_unsafe_filter_name_is_refused(self):
        with open(self.repo / ".git/config", "a") as stream:
            stream.write('[filter "a=b"]\n\tclean = true\n')
        self.assertEqual(kiro_review.git_status(self.cwd)["kind"], "failed")

    def test_no_filters_is_not_an_error(self):
        self.assertEqual(kiro_review.filter_overrides(self.cwd), [])
        self.assertEqual(kiro_review.git_status(self.cwd)["kind"], "git")


if __name__ == "__main__":
    unittest.main(verbosity=2)
