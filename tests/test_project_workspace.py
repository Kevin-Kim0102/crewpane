"""Backend tests for the project workspace (tree/file/write endpoints and project-scoped review).

Every path lives in a throwaway system temp directory. The recycle-bin tests point XDG_DATA_HOME and HOME at
that same temp directory, so neither a real project nor the user's system trash is ever touched. No model call
is made; Git runs only inside the fixture."""
import atexit
from contextlib import contextmanager
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
import unittest
import unittest.mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import watch_kiro  # noqa: E402

TMP_BASE = Path(tmpdirs.mkdtemp("coder-agents-workspace-"))
atexit.register(shutil.rmtree, TMP_BASE, ignore_errors=True)

GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

HAS_XDG_TRASH = sys.platform.startswith("linux")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=check, capture_output=True, text=True)


def write(path, text, mode="w"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as stream:
        stream.write(text)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Fixture:
    """A catalog project whose main root is a Git repository with text, binary, sensitive and symlinked paths."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="ws-")).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.other = self.base / "other"       # a second root of the same catalog project
        self.other.mkdir()
        self.outside = self.base / "outside"   # never part of any root
        self.outside.mkdir()
        write(self.outside / "secret.txt", "TOP SECRET\n")
        write(self.root / "readme.txt", "hello\nworld\n")
        write(self.root / "tracked.txt", "line one\n")
        write(self.root / "sub/note.txt", "note\n")
        write(self.root / "sub/deep/inner.txt", "inner\n")
        (self.root / "emptydir").mkdir()
        write(self.root / ".env", "SECRET=1\n")
        write(self.root / "credentials", "token\n")
        (self.root / "bin.dat").write_bytes(b"\0\1\2")
        os.symlink(str(self.outside / "secret.txt"), self.root / "link_out")
        os.symlink("readme.txt", self.root / "link_in")
        os.symlink(str(self.outside), self.root / "linkdir")
        os.symlink(str(self.outside), self.root / "sub/escape")
        write(self.other / "other.txt", "other root\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "tracked.txt")
        git(self.root, "commit", "-qm", "init")
        self.state = self.base / "codex.json"
        self.group = "codex:proj-a"
        write(self.state, json.dumps({"local-projects": {"proj-a": {
            "id": "proj-a", "name": "Project A", "rootPaths": [str(self.root), str(self.other)]}}}))

    def close(self):
        shutil.rmtree(self.base, ignore_errors=True)


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.cfg = self.fx.base / "cfg"
        self.cfg.mkdir()
        self.panel = watch_kiro.Panel({"port": free_port(), "token": "t" * 32}, self.cfg / "config.json",
                                      codex_state=self.fx.state, daily_base=self.fx.base / "daily",
                                      models=object(), usage=object())
        self.group = self.fx.group
        self.root = str(self.fx.root)

    def call(self, kind, **params):
        params.setdefault("group", self.group)
        return self.panel.workspace(kind, params)

    def tree(self, path=""):
        return self.call("tree", path=path)

    def read(self, path):
        return self.call("file", path=path)

    def mutate(self, action, path, **extra):
        return self.call("write", action=action, path=path, **extra)

    def ok(self, result):
        status, body = result
        self.assertEqual(status, 200, body)
        return body

    def denied(self, result, *secrets):
        status, body = result
        self.assertGreaterEqual(status, 400, body)
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else json.dumps(body)
        for secret in secrets:
            self.assertNotIn(secret, text)
        return status, body

    def root_revision(self):
        return self.ok(self.tree())["root_revision"]

    @contextmanager
    def isolated_trash(self):
        """XDG_DATA_HOME and HOME both point into the fixture: the real system trash can never be reached."""
        home = self.fx.base / "home"
        home.mkdir(exist_ok=True)
        environment = {"XDG_DATA_HOME": str(self.fx.base / "xdg"), "HOME": str(home)}
        with unittest.mock.patch.dict(os.environ, environment):
            yield Path(environment["XDG_DATA_HOME"]) / "Trash"


class ProjectIdentification(WorkspaceCase):
    def test_tree_of_catalog_root_needs_no_registered_conversation(self):
        self.assertEqual(self.panel.registered_sources(), [])
        body = self.ok(self.tree())
        self.assertEqual(body["cwd"], self.root)
        self.assertEqual(body["group"], self.group)
        self.assertEqual(body["path"], "")
        self.assertFalse(body["truncated"])
        self.assertIsInstance(body["root_revision"], str)
        self.assertTrue(body["root_revision"])
        entries = {entry["name"]: entry for entry in body["entries"]}
        self.assertEqual(entries["sub"]["kind"], "directory")
        self.assertEqual(entries["emptydir"]["kind"], "directory")
        self.assertNotIn("revision", entries["sub"])
        self.assertEqual(entries["readme.txt"]["kind"], "file")
        self.assertTrue(entries["readme.txt"]["revision"])
        self.assertEqual(entries["bin.dat"]["kind"], "file")
        for hidden in (".env", "credentials", ".git", "link_out", "link_in", "linkdir"):
            self.assertNotIn(hidden, entries)

    def test_tree_subdirectory_keeps_scope_and_root_revision(self):
        root_body = self.ok(self.tree())
        sub = self.ok(self.tree("sub"))
        self.assertEqual(sub["path"], "sub")
        self.assertEqual(sub["cwd"], self.root)
        self.assertEqual(sub["root_revision"], root_body["root_revision"])
        names = {entry["name"] for entry in sub["entries"]}
        self.assertEqual(names, {"note.txt", "deep"})  # "escape" is a symlink: never listed
        self.assertNotIn("readme.txt", names)

    def test_tree_marks_git_ignored_files_and_directories(self):
        root = self.fx.root
        write(root / ".gitignore", "emptydir/\n*.log\n!keep.log\ntracked.txt\n")
        write(root / ".git/info/exclude", "local.cache\n")
        write(root / "sub/.gitignore", "!keep.log\n")
        for name in (":build.log", "line\nbreak.log", "keep.log", "local.cache", "sub/skip.log", "sub/keep.log"):
            write(root / name, "example\n")

        def ignored(path=""):
            return {entry["name"] for entry in self.ok(self.tree(path))["entries"] if entry.get("ignored")}

        self.assertEqual(ignored(), {"emptydir", ":build.log", "line\nbreak.log", "local.cache"})
        self.assertEqual(ignored("sub"), {"skip.log"})

    def test_unknown_group_is_not_found(self):
        for group in ("", "codex:nope", "nope", "other"):
            status, body = self.panel.workspace("tree", {"group": group})
            self.assertEqual(status, 404, (group, body))

    def test_second_root_and_outside_are_unreachable(self):
        for path in (self.root + "/readme.txt", str(self.fx.other / "other.txt"), str(self.fx.outside / "secret.txt"),
                     "../other/other.txt", "../outside/secret.txt", "sub/../../outside/secret.txt"):
            self.denied(self.read(path), "TOP SECRET", "other root")
        self.denied(self.tree(".."))
        self.denied(self.tree("../other"))
        self.denied(self.tree(str(self.fx.other)))

    def test_symlinks_are_not_followed(self):
        status, body = self.read("linkdir/secret.txt")
        self.assertNotIn("text", body)
        self.assertNotIn("TOP SECRET", json.dumps(body))
        if status == 200:  # the read API reports a refused symlink as a state, never as content
            self.assertEqual(body["state"], "symlink")
        self.denied(self.tree("linkdir"), "TOP SECRET")


class FileRead(WorkspaceCase):
    def test_read_ok_reuses_review_file_state_with_revisions(self):
        body = self.ok(self.read("readme.txt"))
        self.assertEqual(body["state"], "ok")
        self.assertEqual(body["text"], "hello\nworld\n")
        self.assertEqual(body["path"], "readme.txt")
        self.assertEqual(body["cwd"], self.root)
        self.assertEqual(body["group"], self.group)
        self.assertIsInstance(body["revision"], str)
        self.assertIsInstance(body["root_revision"], str)
        tree_entry = {entry["name"]: entry for entry in self.ok(self.tree())["entries"]}["readme.txt"]
        self.assertEqual(body["revision"], tree_entry["revision"])

    def test_read_reports_states_instead_of_failing(self):
        cases = {"missing.txt": "missing", "sub": "directory", "bin.dat": "binary", "link_in": "symlink"}
        for path, state in cases.items():
            status, body = self.read(path)
            self.assertEqual(status, 200, (path, body))
            self.assertEqual(body["state"], state, (path, body))
            self.assertNotIn("text", body, path)
        self.assertEqual(self.ok(self.read("sub/note.txt"))["text"], "note\n")

    def test_sensitive_traversal_and_absolute_paths_are_refused(self):
        for path in (".env", ".env.local", ".git/config", "credentials", "sub/../.env", "../outside/secret.txt",
                     str(self.fx.root / ".env")):
            body = self.denied(self.read(path), "SECRET", "TOP SECRET")[1]
            self.assertNotIn("text", body, path)

    def test_external_rewrite_changes_the_returned_revision(self):
        before = self.ok(self.read("readme.txt"))
        write(self.fx.root / "readme.txt", "rewritten elsewhere\n")
        after = self.ok(self.read("readme.txt"))
        self.assertEqual(after["text"], "rewritten elsewhere\n")
        self.assertNotEqual(after["revision"], before["revision"])


class WriteLifecycle(WorkspaceCase):
    def test_new_file_and_directory_lifecycle(self):
        revision = self.root_revision()
        body = self.ok(self.mutate("new_file", "fresh.txt", root_revision=revision))
        self.assertTrue(body["ok"])
        self.assertEqual((self.fx.root / "fresh.txt").read_bytes(), b"")
        self.ok(self.mutate("new_directory", "docs", root_revision=revision))
        self.assertTrue((self.fx.root / "docs").is_dir())
        self.ok(self.mutate("new_file", "docs/page.md", root_revision=revision))
        self.assertTrue((self.fx.root / "docs/page.md").is_file())
        names = {entry["name"] for entry in self.ok(self.tree())["entries"]}
        self.assertIn("fresh.txt", names)
        self.assertIn("docs", names)
        names = {entry["name"] for entry in self.ok(self.tree("docs"))["entries"]}
        self.assertEqual(names, {"page.md"})

    def test_duplicate_and_missing_parent(self):
        revision = self.root_revision()
        self.ok(self.mutate("new_file", "dup.txt", root_revision=revision))
        status, body = self.mutate("new_file", "dup.txt", root_revision=revision)
        self.assertEqual(status, 409, body)
        status, body = self.mutate("new_file", "nodir/x.txt", root_revision=revision)
        self.assertEqual(status, 404, body)

    def test_write_requires_the_current_root_revision(self):
        for extra in ({"root_revision": "0" * 64}, {}):
            status, body = self.mutate("new_file", "stale.txt", **extra)
            self.assertEqual(status, 409, body)
        self.assertFalse((self.fx.root / "stale.txt").exists())

    def test_write_paths_are_confined(self):
        revision = self.root_revision()
        for path in ("../escape.txt", "/tmp/coder-agents-escape.txt", ".git/hooks/pre-commit", "sub/../x.txt",
                     "..", ".coder-agents-save-x", "sub/.coder-agents-save-y"):
            self.denied(self.mutate("new_file", path, root_revision=revision))
            self.denied(self.mutate("new_directory", path, root_revision=revision))
        self.denied(self.mutate("save", ".env", root_revision=revision, revision="0" * 64, text="x"))
        self.assertFalse((self.fx.base / "escape.txt").exists())
        self.assertEqual((self.fx.root / ".env").read_text(), "SECRET=1\n")

    def test_unknown_or_incomplete_action_is_refused(self):
        revision = self.root_revision()
        for params in ({"action": "delete", "path": "readme.txt", "root_revision": revision},
                       {"path": "readme.txt", "root_revision": revision},
                       {"action": "new_file", "root_revision": revision}):
            status, body = self.panel.workspace("write", {"group": self.group, **params})
            self.assertEqual(status, 400, (params, body))


class Save(WorkspaceCase):
    def test_save_round_trip_returns_the_new_revision(self):
        before = self.ok(self.read("readme.txt"))
        revision = self.root_revision()
        body = self.ok(self.mutate("save", "readme.txt", root_revision=revision,
                                   revision=before["revision"], text="saved\n"))
        self.assertTrue(body["ok"])
        self.assertNotEqual(body["revision"], before["revision"])
        self.assertEqual((self.fx.root / "readme.txt").read_text(), "saved\n")
        after = self.ok(self.read("readme.txt"))
        self.assertEqual(after["text"], "saved\n")
        self.assertEqual(after["revision"], body["revision"])

    def test_stale_revision_is_a_conflict_and_never_overwrites(self):
        before = self.ok(self.read("readme.txt"))
        write(self.fx.root / "readme.txt", "external change\n")  # another program rewrote the file
        status, body = self.mutate("save", "readme.txt", root_revision=self.root_revision(),
                                   revision=before["revision"], text="panel wins\n")
        self.assertEqual(status, 409, body)
        self.assertEqual((self.fx.root / "readme.txt").read_text(), "external change\n")
        # the refreshed revision succeeds
        current = self.ok(self.read("readme.txt"))
        self.ok(self.mutate("save", "readme.txt", root_revision=self.root_revision(),
                            revision=current["revision"], text="panel wins\n"))
        self.assertEqual((self.fx.root / "readme.txt").read_text(), "panel wins\n")

    def test_missing_revision_or_file_is_a_conflict(self):
        for path, extra in (("readme.txt", {}), ("readme.txt", {"revision": "0" * 64}), ("ghost.txt", {"revision": "0" * 64})):
            status, body = self.mutate("save", path, root_revision=self.root_revision(), text="x", **extra)
            self.assertEqual(status, 409, (path, extra, body))
        self.assertEqual((self.fx.root / "readme.txt").read_text(), "hello\nworld\n")

    def test_save_rejects_non_text_and_leaves_no_temporary_files(self):
        revision = self.root_revision()
        current = self.ok(self.read("readme.txt"))
        for text in (123, None, "a\0b"):
            payload = {"group": self.group, "action": "save", "path": "readme.txt", "root_revision": revision,
                       "revision": current["revision"], "text": text}
            if text is None:
                payload.pop("text")
            status, body = self.panel.workspace("write", payload)
            self.assertEqual(status, 400, (text, body))
        self.ok(self.mutate("save", "readme.txt", root_revision=revision,
                            revision=current["revision"], text="clean\n"))
        leftovers = [name for name in os.listdir(self.fx.root) if name.startswith(".coder-agents-save-")]
        self.assertEqual(leftovers, [])


@unittest.skipUnless(HAS_XDG_TRASH, "the recycle bin uses the Linux XDG trash")
class RecycleBin(WorkspaceCase):
    def test_trash_moves_the_file_into_the_isolated_bin(self):
        current = self.ok(self.read("readme.txt"))
        with self.isolated_trash() as trash:
            body = self.ok(self.mutate("trash", "readme.txt", root_revision=self.root_revision(),
                                       revision=current["revision"]))
            self.assertTrue(body["ok"])
            self.assertFalse((self.fx.root / "readme.txt").exists())
            files = list((trash / "files").iterdir())
            info = list((trash / "info").iterdir())
            self.assertEqual(len(files), 1, files)
            self.assertEqual(len(info), 1, info)
            self.assertEqual(files[0].read_text(), "hello\nworld\n")
            self.assertEqual(info[0].suffix, ".trashinfo")
            self.assertEqual(info[0].stem, files[0].name)
            record = info[0].read_text()
            self.assertIn("[Trash Info]", record)
            self.assertIn("Path=", record)
            self.assertIn("readme.txt", record)
            for created in files + info:
                self.assertTrue(str(created.resolve()).startswith(str(self.fx.base)))
        names = {entry["name"] for entry in self.ok(self.tree())["entries"]}
        self.assertNotIn("readme.txt", names)

    def test_trash_with_a_stale_revision_changes_nothing(self):
        current = self.ok(self.read("readme.txt"))
        write(self.fx.root / "readme.txt", "external change\n")
        with self.isolated_trash() as trash:
            status, body = self.mutate("trash", "readme.txt", root_revision=self.root_revision(),
                                       revision=current["revision"])
            self.assertEqual(status, 409, body)
            self.assertEqual((self.fx.root / "readme.txt").read_text(), "external change\n")
            self.assertFalse((trash / "files").exists() and any((trash / "files").iterdir()))

    def test_trash_refuses_missing_paths_and_directories(self):
        with self.isolated_trash():
            for path, extra in (("ghost.txt", {"revision": "0" * 64}), ("sub", {}), ("sub", {"revision": "0" * 64})):
                status, body = self.mutate("trash", path, root_revision=self.root_revision(), **extra)
                self.assertEqual(status, 409, (path, extra, body))
        self.assertTrue((self.fx.root / "sub").is_dir())
        self.assertTrue((self.fx.root / "sub/note.txt").is_file())


class ProjectGitScope(WorkspaceCase):
    def setUp(self):
        super().setUp()
        write(self.fx.root / "tracked.txt", "line one\nline two\n")
        write(self.fx.root / "untracked-project.txt", "u\n")

    def test_project_scope_status_needs_no_conversation(self):
        status, body = self.panel.review("status", {"scope": "project", "group": self.group})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "git")
        self.assertEqual(body["cwd"], self.root)
        self.assertEqual(body["group"], self.group)
        unstaged = {entry["path"]: entry for entry in body["areas"]["unstaged"]["entries"]}
        self.assertEqual(unstaged["tracked.txt"]["added"], 1)
        untracked = {entry["path"] for entry in body["areas"]["untracked"]["entries"]}
        self.assertIn("untracked-project.txt", untracked)

    def test_project_scope_file_and_diff(self):
        status, body = self.panel.review("file", {"scope": "project", "group": self.group, "path": "tracked.txt"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "ok")
        self.assertEqual(body["text"], "line one\nline two\n")
        self.assertEqual(body["cwd"], self.root)
        status, body = self.panel.review("diff", {"scope": "project", "group": self.group,
                                                  "area": "unstaged", "path": "tracked.txt"})
        self.assertEqual(status, 200, body)
        self.assertIn("+line two", body["diff"])

    def test_project_scope_refuses_unknown_group_and_hostile_paths(self):
        self.assertEqual(self.panel.review("status", {"scope": "project", "group": "codex:nope"})[0], 404)
        for path in ("../outside/secret.txt", ".env", str(self.fx.other / "other.txt"), "sub/escape/secret.txt"):
            status, body = self.panel.review("file", {"scope": "project", "group": self.group, "path": path})
            self.denied((status, body), "TOP SECRET", "SECRET=1")
        status, body = self.panel.review("diff", {"scope": "project", "group": self.group,
                                                  "area": "unstaged", "path": ".env"})
        self.denied((status, body))

    def test_review_without_project_scope_still_requires_the_conversation_identity(self):
        self.assertEqual(self.panel.review("status", {"group": self.group})[0], 400)
        self.assertEqual(self.panel.review("file", {"group": self.group, "path": "tracked.txt"})[0], 400)


class HTTPWorkspace(WorkspaceCase):
    def setUp(self):
        super().setUp()
        handler = watch_kiro.make_handler(self.panel)
        self.server = watch_kiro.ThreadingHTTPServer(("127.0.0.1", self.panel.port), handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = "http://127.0.0.1:%d" % self.panel.port

    def request(self, path, method="GET", payload=None, cookie=True, host=None, origin=None,
                content_type=None, fetch_site=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.panel.port, timeout=10)
        headers = {"Host": host or "127.0.0.1:%d" % self.panel.port}
        if cookie:
            headers["Cookie"] = "%s=%s" % (self.panel.cookie_name, self.panel.cookie_value)
        if origin:
            headers["Origin"] = origin
        if content_type:
            headers["Content-Type"] = content_type
        if fetch_site:
            headers["Sec-Fetch-Site"] = fetch_site
        body = None
        if payload is not None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def api(self, name, **query):
        return "/api/workspace/%s%s" % (name, "?" + urlencode(query) if query else "")

    def post(self, payload, **extra):
        options = {"method": "POST", "payload": payload, "origin": self.origin,
                   "content_type": "application/json"}
        options.update(extra)
        return self.request("/api/workspace", **options)

    # --- read routes ----------------------------------------------------------------------------------
    def test_http_tree_and_file(self):
        status, body = self.request(self.api("tree", group=self.group))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cwd"], self.root)
        self.assertEqual(body["group"], self.group)
        self.assertIn("readme.txt", {entry["name"] for entry in body["entries"]})
        revision = body["root_revision"]
        status, body = self.request(self.api("file", group=self.group, path="readme.txt"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "ok")
        self.assertEqual(body["text"], "hello\nworld\n")
        self.assertEqual(body["cwd"], self.root)
        self.assertEqual(body["group"], self.group)
        self.assertTrue(body["revision"])
        self.assertEqual(body["root_revision"], revision)
        status, body = self.request(self.api("file", group=self.group, path="../outside/secret.txt"))
        self.denied((status, body), "TOP SECRET")

    def test_http_read_routes_keep_cookie_host_and_origin_rules(self):
        url = self.api("tree", group=self.group)
        self.assertEqual(self.request(url, cookie=False)[0], 401)
        self.assertEqual(self.request(url, host="evil.example:%d" % self.panel.port)[0], 403)
        self.assertEqual(self.request(url, origin="http://evil.example")[0], 403)
        self.assertEqual(self.request(url, method="POST")[0], 404)
        self.assertEqual(self.request(self.api("file", group=self.group, path=".env"))[0], 403)
        self.assertEqual(self.request(self.api("file", group=self.group, path="nope.txt"))[0], 200)
        self.assertEqual(self.request(self.api("unknown", group=self.group))[0], 404)

    # --- write route ----------------------------------------------------------------------------------
    def test_http_access_protection_for_writes(self):
        revision = self.request(self.api("tree", group=self.group))[1]["root_revision"]
        payload = {"group": self.group, "action": "new_file", "path": "x.txt", "root_revision": revision}
        self.assertEqual(self.post(payload, cookie=False)[0], 401)
        self.assertEqual(self.post(payload, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post(payload, origin=None)[0], 403)
        self.assertEqual(self.post(payload, content_type="text/plain")[0], 415)
        self.assertEqual(self.post(payload, fetch_site="cross-site")[0], 403)
        self.assertEqual(self.post(b"{")[0], 400)
        self.assertEqual(self.post({"group": self.group, "action": "new_file", "path": "../escape.txt",
                                    "root_revision": revision})[0], 403)
        self.assertFalse((self.fx.root / "x.txt").exists())
        self.assertFalse((self.fx.base / "escape.txt").exists())

    def test_http_write_save_and_trash_lifecycle(self):
        revision = self.request(self.api("tree", group=self.group))[1]["root_revision"]
        status, body = self.post({"group": self.group, "action": "new_file", "path": "http.txt",
                                  "root_revision": revision})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        status, body = self.request(self.api("file", group=self.group, path="http.txt"))
        self.assertEqual((status, body["state"], body["text"]), (200, "ok", ""))
        status, saved = self.post({"group": self.group, "action": "save", "path": "http.txt",
                                   "root_revision": revision, "revision": body["revision"], "text": "via http\n"})
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["revision"])
        status, body = self.request(self.api("file", group=self.group, path="http.txt"))
        self.assertEqual(body["text"], "via http\n")
        self.assertEqual(body["revision"], saved["revision"])
        # an external rewrite makes the old revision a conflict
        write(self.fx.root / "http.txt", "external\n")
        status, body = self.post({"group": self.group, "action": "save", "path": "http.txt",
                                  "root_revision": revision, "revision": saved["revision"], "text": "overwrite\n"})
        self.assertEqual(status, 409, body)
        self.assertEqual((self.fx.root / "http.txt").read_text(), "external\n")
        if HAS_XDG_TRASH:
            status, current = self.request(self.api("file", group=self.group, path="http.txt"))
            with self.isolated_trash() as trash:
                status, body = self.post({"group": self.group, "action": "trash", "path": "http.txt",
                                          "root_revision": revision, "revision": current["revision"]})
                self.assertEqual(status, 200, body)
                self.assertFalse((self.fx.root / "http.txt").exists())
                self.assertEqual(len(list((trash / "files").iterdir())), 1)
                self.assertEqual(len(list((trash / "info").iterdir())), 1)
        # duplicate names and stale root revisions are conflicts
        self.assertEqual(self.post({"group": self.group, "action": "new_file", "path": "dup.txt",
                                    "root_revision": revision})[0], 200)
        self.assertEqual(self.post({"group": self.group, "action": "new_file", "path": "dup.txt",
                                    "root_revision": revision})[0], 409)
        self.assertEqual(self.post({"group": self.group, "action": "new_file", "path": "stale.txt",
                                    "root_revision": "0" * 64})[0], 409)
        self.assertFalse((self.fx.root / "stale.txt").exists())

    # --- project-scoped Git routes --------------------------------------------------------------------
    def test_http_project_scope_review(self):
        write(self.fx.root / "tracked.txt", "line one\nline two\n")
        status, body = self.request("/api/review?" + urlencode({"scope": "project", "group": self.group}))
        self.assertEqual(status, 200, body)
        self.assertEqual((body["kind"], body["cwd"]), ("git", self.root))
        status, body = self.request("/api/review/file?" + urlencode(
            {"scope": "project", "group": self.group, "path": "tracked.txt"}))
        self.assertEqual((status, body["state"], body["text"]), (200, "ok", "line one\nline two\n"))
        status, body = self.request("/api/review/diff?" + urlencode(
            {"scope": "project", "group": self.group, "area": "unstaged", "path": "tracked.txt"}))
        self.assertEqual(status, 200, body)
        self.assertIn("+line two", body["diff"])
        status, body = self.request("/api/review?" + urlencode({"scope": "project", "group": "codex:nope"}))
        self.assertEqual(status, 404, body)
        self.assertEqual(self.request("/api/review?scope=project&group=" + self.group, cookie=False)[0], 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
