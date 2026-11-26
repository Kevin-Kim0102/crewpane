"""Read-only review backend for the Kiro panel: file viewing and Git working-tree changes.

Everything here is confined to ONE directory (the working directory a conversation recorded) and never
writes: no `git add/commit/reset/checkout/init/fetch`, no hooks, no external diff/textconv/filter commands,
no shell. Callers pass an already authorised, symlink-resolved directory; browser input is only a relative path.
"""
import errno
import hashlib
import os
import posixpath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import nullcontext

MAX_FILE_BYTES = 1024 * 1024        # larger files are reported, never loaded
MAX_DIFF_BYTES = 4 * MAX_FILE_BYTES + 65_536  # two full text versions, line prefixes and patch headers
MAX_LIST_BYTES = 2_000_000          # raw listing output of one git command
MAX_ENTRIES = 400                   # files listed per category
MAX_UNTRACKED_BYTES = 8 * 1024 * 1024   # total bytes read to count untracked lines
GIT_TIMEOUT = 8.0
GIT = shutil.which("git")

SENSITIVE_DIRS = {".git", ".ssh", ".aws", ".kiro", ".gnupg", ".kube", ".docker", ".azure", ".gcloud", ".codex",
                  ".terraform", ".password-store", ".secrets"}
SENSITIVE_NAMES = re.compile(
    r"(?i)^(\.env($|\..*)|\.?netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.htpasswd|auth\.json|kubeconfig"
    r"|id_(rsa|dsa|ecdsa|ed25519).*|credentials?(\..*)?|service[-_]account.*\.json|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|gpg|tfstate)"
    r"|(secrets?|passwords?|tokens?)\.(json|ya?ml|txt|toml|ini|env|conf|cfg))$")

GIT_ERRORS = {
    "git_missing": "这台机器没有可用的 git，无法显示 Git 改动",
    "unsafe": "Git 拒绝读取这个仓库（目录所有者与当前用户不同）",
    "timeout": "Git 读取超时，已停止",
    "failed": "Git 读取失败",
    "too_large": "输出过大，已停止读取",
}


class ReviewError(Exception):
    """A refusal the browser may be told about verbatim (never contains git's own stderr)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def is_sensitive(parts):
    """True if any path component names a credential/secret location that must never be browsed."""
    for part in parts:
        if part.lower() in SENSITIVE_DIRS or SENSITIVE_NAMES.match(part):
            return True
    return False


def authorised_directory(path):
    """Symlink-resolved directory for a recorded working directory, or ReviewError."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ReviewError(404, "这个对话没有记录可用的工作目录")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise ReviewError(404, "这个对话原来的工作目录已不存在")
    if real == os.sep:
        raise ReviewError(403, "根目录不能作为审阅范围")
    if {".git", ".ssh", ".aws", ".gnupg"} & {p.lower() for p in real.split(os.sep)}:
        raise ReviewError(403, "敏感目录不提供浏览")
    return real


def clean_relative(cwd, value, logical=None):
    """Normalised POSIX path of `value` relative to `cwd` (a relative path, or an absolute one below cwd or its recorded, unresolved spelling `logical`).

    Raises ReviewError for traversal, NUL bytes, other directories and sensitive names. No filesystem access."""
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise ReviewError(400, "文件路径无效")
    if ".." in value.split("/"):
        raise ReviewError(403, "路径包含 .. ，已拒绝")
    if os.path.isabs(value):
        norm = posixpath.normpath(value)
        prefixes = [base.rstrip("/") + "/" for base in (cwd, logical) if base]
        prefix = next((p for p in prefixes if norm.startswith(p)), None)
        if prefix is None:
            raise ReviewError(403, "这个文件不在对话的工作目录内，面板不读取")
        value = norm[len(prefix):]
    parts = value.split("/")
    parts = [part for part in parts if part not in ("", ".")]
    if not parts:
        raise ReviewError(400, "文件路径无效")
    if is_sensitive(parts):
        raise ReviewError(403, "这是凭据或敏感目录中的文件，面板不提供浏览")
    return "/".join(parts)


def describe(rel, state, message, **extra):
    return {"path": rel, "name": rel.rsplit("/", 1)[-1], "state": state, "message": message, **extra}


def file_revision(info):
    fields = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(repr(fields).encode("ascii")).hexdigest()


def read_text(cwd, rel, *, revision=False, root_fd=None):
    """Read one regular text file below cwd. Always returns a dict with a `state`; never raises for file problems.

    The path is walked with directory file descriptors: the trusted `cwd` is opened once, then every component is
    opened relative to the previous descriptor with O_NOFOLLOW (and O_DIRECTORY for directories). A parent that is
    swapped for a symlink after the walk started cannot redirect the read, because later steps only use descriptors
    that were already opened. The final file is checked with fstat on the opened descriptor itself.

    states: ok, missing, directory, symlink, not_file, binary, too_large, unreadable"""
    parts = rel.split("/")
    base_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    held = []

    def refuse(error, name, directory):
        if error.errno == errno.ELOOP:
            return describe(rel, "symlink", "路径包含符号链接，面板不跟随读取")
        if error.errno == errno.ENOENT:
            return describe(rel, "missing", "文件不存在（可能已删除或尚未创建）")
        if error.errno in (errno.ENOTDIR, errno.ENXIO):
            try:  # O_DIRECTORY|O_NOFOLLOW reports a symlink as ENOTDIR on Linux; tell the two apart
                if stat.S_ISLNK(os.lstat(name, dir_fd=held[-1]).st_mode):
                    return describe(rel, "symlink", "路径包含符号链接，面板不跟随读取")
            except OSError:
                pass
            if directory:
                return describe(rel, "missing", "文件不存在")
            return describe(rel, "not_file", "不是普通文件（设备、管道等不读取）")
        return describe(rel, "unreadable", "无法读取这个路径")

    try:
        try:
            held.append(os.dup(root_fd) if root_fd is not None else os.open(cwd, base_flags | os.O_DIRECTORY))
        except OSError:
            return describe(rel, "unreadable", "无法读取这个路径")
        for part in parts[:-1]:
            try:
                held.append(os.open(part, base_flags | os.O_DIRECTORY, dir_fd=held[-1]))
            except OSError as error:
                return refuse(error, part, True)
        try:
            descriptor = os.open(parts[-1], base_flags | os.O_NONBLOCK, dir_fd=held[-1])
        except OSError as error:
            return refuse(error, parts[-1], False)
        held.append(descriptor)
        try:
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                return describe(rel, "directory", "这是目录，不是文件")
            if not stat.S_ISREG(info.st_mode):
                return describe(rel, "not_file", "不是普通文件（设备、管道等不读取）")
            if info.st_size > MAX_FILE_BYTES:
                return describe(rel, "too_large", "文件过大（%d 字节），超过 %d 字节上限，不加载" % (info.st_size, MAX_FILE_BYTES), size=info.st_size)
            data = b""
            while len(data) <= MAX_FILE_BYTES:
                chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if revision and file_revision(os.fstat(descriptor)) != file_revision(info):
                return describe(rel, "changed", "读取期间文件已变化，请重新打开")
        except OSError:
            return describe(rel, "unreadable", "无法读取这个文件")
    finally:
        for item in held:
            try:
                os.close(item)
            except OSError:
                pass
    if len(data) > MAX_FILE_BYTES:
        return describe(rel, "too_large", "文件过大，超过 %d 字节上限，不加载" % MAX_FILE_BYTES, size=len(data))
    if b"\0" in data:
        return describe(rel, "binary", "二进制文件，不显示内容", size=len(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return describe(rel, "binary", "不是 UTF-8 文本，不显示内容", size=len(data))
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return describe(rel, "ok", "", text=text, lines=lines, size=len(data),
                    **({"revision": file_revision(info)} if revision else {}))


# ---- Git ---------------------------------------------------------------------------------------------
def git_environment():
    """A clean environment: no user/system config, no pager, no prompts, no lazy network fetch, no locks."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C", "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
            "GIT_NO_LAZY_FETCH": "1", "GIT_ASKPASS": "/bin/false", "GIT_SSH_COMMAND": "/bin/false"}


class GitResult:
    def __init__(self, code, out, truncated=False, timed_out=False, err=b""):
        self.code, self.out, self.truncated, self.timed_out, self.err = code, out, truncated, timed_out, err


def run_git(cwd, args, cap=None, timeout=GIT_TIMEOUT, extra=(), *, stdin=None):
    """Run git with an argument array (never a shell). Output is capped and the whole process group is killed on overrun."""
    if GIT is None:
        raise ReviewError(409, GIT_ERRORS["git_missing"])
    cap = MAX_LIST_BYTES if cap is None else cap
    argv = [GIT, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
            "-c", "protocol.allow=never", "-c", "diff.external=", *extra, *args]
    with (tempfile.TemporaryFile() if stdin is not None else nullcontext(subprocess.DEVNULL)) as source:
        if stdin is not None:
            source.write(stdin)
            source.seek(0)
        process = subprocess.Popen(argv, cwd=cwd, env=git_environment(), stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    out, err, truncated, timed_out = bytearray(), bytearray(), False, False
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == "out":
                    out += chunk
                    if len(out) > cap:
                        truncated = True
                        del out[cap:]
                        break
                elif len(err) < 4096:
                    err += chunk
            if truncated:
                break
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return GitResult(process.returncode, bytes(out), truncated, timed_out, bytes(err))


MAX_FILTERS = 200   # more filter drivers than this cannot all be neutralised: Git is not run at all


def filter_overrides(cwd):
    """`-c filter.<name>.clean=` for every configured filter driver, so status/diff cannot launch repo commands.

    Fails closed: if the configuration cannot be read completely (error, timeout, truncated output), has more than
    MAX_FILTERS drivers, or has a driver name that cannot be passed safely through `-c`, a ReviewError is raised and the
    caller must not run any further Git command for this request."""
    result = run_git(cwd, ["config", "-z", "--name-only", "--get-regexp", r"^filter\."], cap=65536)
    if result.timed_out:
        raise ReviewError(409, "读取仓库的过滤器配置超时，为避免执行未覆盖的过滤器，已停止读取 Git 改动")
    if result.truncated:
        raise ReviewError(409, "仓库的过滤器配置过长，无法完整读取，为避免执行未覆盖的过滤器，已停止读取 Git 改动")
    if result.code not in (0, 1):   # 1 only means "no filter configured"
        raise ReviewError(409, "读取仓库的过滤器配置失败，为避免执行未覆盖的过滤器，已停止读取 Git 改动")
    names = set()
    for key in result.out.decode("utf-8", "replace").split("\0"):
        match = re.fullmatch(r"filter\.(.+)\.(clean|smudge|process|required)", key, re.I)
        if match:
            names.add(match.group(1))
    if len(names) > MAX_FILTERS:
        raise ReviewError(409, "仓库配置了 %d 个 Git 过滤器，超过 %d 个的上限，为避免执行未覆盖的过滤器，已拒绝读取 Git 改动" % (len(names), MAX_FILTERS))
    extra = []
    for name in sorted(names):
        if "=" in name or "\n" in name or "\ufffd" in name:
            raise ReviewError(409, "仓库有无法安全覆盖的过滤器名称，已拒绝读取 Git 改动")
        for option, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            extra += ["-c", "filter.%s.%s=%s" % (name, option, value)]
    return extra


def decode(data):
    return data.decode("utf-8", "replace")


def fail_state(result):
    if result.timed_out:
        return "timeout"
    if result.truncated:
        return "too_large"
    text = result.err.decode("utf-8", "replace").lower()
    if "dubious ownership" in text:
        return "unsafe"
    return "failed"


def inside(root, cwd):
    return cwd == root or cwd.startswith(root.rstrip(os.sep) + os.sep)


def repo_info(cwd):
    """(info dict, None) for a usable work tree containing cwd, else (None, (state, message))."""
    probe = run_git(cwd, ["rev-parse", "--is-inside-git-dir", "--is-bare-repository", "--is-inside-work-tree",
                          "--show-toplevel", "--show-prefix"], cap=65536)
    if probe.code != 0:
        text = probe.err.decode("utf-8", "replace").lower()
        if probe.timed_out or probe.truncated or "dubious ownership" in text:
            state = fail_state(probe)
            return None, (state, GIT_ERRORS[state])
        return None, ("nogit", "这个目录不在 Git 仓库中")
    lines = decode(probe.out).split("\n")
    if len(lines) < 5:
        return None, ("failed", GIT_ERRORS["failed"])
    inside_git, bare, work, top, prefix = lines[:5]
    if inside_git == "true":
        return None, ("nogit", "工作目录位于 .git 内部，不提供浏览")
    if bare == "true" or work != "true" or not top:
        return None, ("nogit", "这个目录没有 Git 工作区")
    root = os.path.realpath(top)
    if not inside(root, cwd):
        return None, ("failed", "仓库工作区与对话目录不一致，已拒绝")
    branch = run_git(cwd, ["symbolic-ref", "-q", "--short", "HEAD"], cap=4096)
    head = run_git(cwd, ["rev-parse", "--verify", "-q", "--short=12", "HEAD"], cap=4096)
    name = decode(branch.out).strip() if branch.code == 0 else ""
    short = decode(head.out).strip() if head.code == 0 else ""
    return {"root": root, "scope": prefix.strip().rstrip("/"), "branch": name, "detached": not name and bool(short),
            "unborn": head.code != 0, "head": short}, None


NUMSTAT = re.compile(rb"^(\d+|-)\t(\d+|-)\t(.*)$", re.S)


def parse_diff(out):
    """Entries from `git diff --raw --numstat -z`: raw records first, then numstat records."""
    tokens = out.split(b"\0")
    entries, order, index = {}, [], 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith(b":"):
            fields = token[1:].split(b" ")
            status = fields[-1].decode("ascii", "replace")[:1] if fields else "?"
            paths = 2 if status in "RC" else 1
            names = [decode(item) for item in tokens[index:index + paths]]
            index += paths
            if len(names) < paths:
                break
            path = names[-1]
            entries[path] = {"path": path, "old_path": names[0] if paths == 2 else None, "code": status,
                             "added": 0, "deleted": 0, "binary": False}
            order.append(path)
            continue
        match = NUMSTAT.match(token)
        if not match:
            continue
        added, deleted, tail = match.groups()
        if tail == b"":
            names = [decode(item) for item in tokens[index:index + 2]]
            index += 2
            path = names[-1] if names else ""
        else:
            path = decode(tail)
        entry = entries.get(path)
        if entry is None:
            continue
        if added == b"-" or deleted == b"-":
            entry["binary"], entry["added"], entry["deleted"] = True, None, None
        else:
            entry["added"], entry["deleted"] = int(added), int(deleted)
    return [entries[path] for path in order]


DIFF_FLAGS = ["--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--no-color", "--relative", "-M"]


def visible(entry, counters):
    names = [entry["path"]] + ([entry["old_path"]] if entry.get("old_path") else [])
    if any(is_sensitive(name.split("/")) for name in names):
        counters["hidden"] += 1
        return False
    return True


def tracked_area(cwd, cached, extra):
    args = ["diff"] + (["--cached"] if cached else []) + ["--raw", "--numstat", "-z", "--no-abbrev"] + DIFF_FLAGS + ["--", "."]
    result = run_git(cwd, args, extra=extra)
    area = {"entries": [], "truncated": False, "hidden": 0, "error": None}
    if result.code != 0 or result.truncated or result.timed_out:
        state = fail_state(result)
        area["error"] = GIT_ERRORS[state]
        return area
    counters = {"hidden": 0}
    entries = [entry for entry in parse_diff(result.out) if visible(entry, counters)]
    area["hidden"] = counters["hidden"]
    area["truncated"] = len(entries) > MAX_ENTRIES
    area["entries"] = sorted(entries, key=lambda item: item["path"])[:MAX_ENTRIES]
    return area


def untracked_area(cwd, extra):
    result = run_git(cwd, ["ls-files", "--others", "--exclude-standard", "-z", "--", "."], extra=extra)
    area = {"entries": [], "truncated": False, "hidden": 0, "error": None}
    if result.timed_out or (result.code != 0 and not result.truncated):
        area["error"] = GIT_ERRORS[fail_state(result)]
        return area
    items = result.out.split(b"\0")
    if result.truncated:
        items = items[:-1]  # the last record may be cut in half
    paths = [decode(item) for item in items if item and not item.endswith(b"/")]
    hidden = [p for p in paths if is_sensitive(p.split("/"))]
    paths = sorted(p for p in paths if p not in set(hidden))
    area["hidden"] = len(hidden)
    area["truncated"] = result.truncated or len(paths) > MAX_ENTRIES
    budget = MAX_UNTRACKED_BYTES
    for path in paths[:MAX_ENTRIES]:
        entry = {"path": path, "old_path": None, "code": "?", "added": None, "deleted": 0, "binary": False}
        info = read_text(cwd, path) if budget > 0 else None
        if info is None:
            entry["note"] = "未统计"
        elif info["state"] == "ok":
            entry["added"] = info["lines"]
            budget -= info["size"]
        elif info["state"] == "binary":
            entry["binary"], entry["added"], entry["deleted"] = True, None, None
        else:
            entry["note"] = info["message"]
        area["entries"].append(entry)
    return area


def git_status(cwd):
    """Working-tree changes below `cwd`, with index and working tree kept apart. Read-only."""
    base = {"ok": True, "cwd": cwd, "kind": "git", "message": "", "repo": None, "areas": {}}
    if GIT is None:
        return {**base, "kind": "unavailable", "message": GIT_ERRORS["git_missing"]}
    info, problem = repo_info(cwd)
    if problem:
        return {**base, "kind": problem[0], "message": problem[1]}
    try:
        extra = filter_overrides(cwd)
    except ReviewError as error:
        return {**base, "kind": "failed", "message": error.message}
    base["repo"] = info
    base["areas"] = {"staged": tracked_area(cwd, True, extra), "unstaged": tracked_area(cwd, False, extra),
                     "untracked": untracked_area(cwd, extra)}
    return base


def synthetic_added(rel, text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = ["--- /dev/null", "+++ b/" + rel]
    if lines:
        body.append("@@ -0,0 +1,%d @@" % len(lines))
        body += ["+" + line for line in lines]
    return "\n".join(["diff --git a/%s b/%s" % (rel, rel), "new file (untracked)"] + body) + "\n"


def unchanged_diff_text(cwd, rel):
    """Read the regular index blob for a rename or mode change with no text changes."""
    listing = run_git(cwd, ["ls-files", "--stage", "-z", "--", rel], cap=16_384)
    if listing.code != 0 or listing.truncated or listing.timed_out:
        return None
    header, _, path = listing.out.partition(b"\t")
    match = re.fullmatch(rb"100(?:644|755) ([0-9a-f]{40}|[0-9a-f]{64}) 0", header)
    if not match or path != os.fsencode(rel) + b"\0":
        return None
    blob = run_git(cwd, ["cat-file", "blob", match[1].decode("ascii")], cap=MAX_FILE_BYTES)
    if blob.code != 0 or blob.truncated or blob.timed_out or b"\0" in blob.out:
        return None
    try:
        return blob.out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def git_diff(cwd, area, rel):
    """Unified diff of one listed file in one area. `rel` must be present in that area's own fresh listing."""
    if area not in ("staged", "unstaged", "untracked"):
        raise ReviewError(400, "不支持的改动类别")
    rel = clean_relative(cwd, rel)
    if GIT is None:
        raise ReviewError(409, GIT_ERRORS["git_missing"])
    info, problem = repo_info(cwd)
    if problem:
        raise ReviewError(409, problem[1])
    extra = filter_overrides(cwd)
    if area == "untracked":
        listing = untracked_area(cwd, extra)
        if rel not in {e["path"] for e in listing["entries"]}:
            return {"area": area, "path": rel, "state": "missing", "message": "该文件已不在未跟踪列表中，请刷新"}
        file = read_text(cwd, rel)
        if file["state"] != "ok":
            return {"area": area, "path": rel, "state": file["state"], "message": file["message"]}
        return {"area": area, "path": rel, "state": "ok", "message": "", "diff": synthetic_added(rel, file["text"]),
                "added": file["lines"], "deleted": 0, "truncated": False}
    listing = tracked_area(cwd, area == "staged", extra)
    entry = next((e for e in listing["entries"] if e["path"] == rel), None)
    if entry is None:
        return {"area": area, "path": rel, "state": "missing", "message": "该文件已不在这个类别的改动中，请刷新"}
    paths = [entry["path"]] + ([entry["old_path"]] if entry.get("old_path") else [])
    args = ["diff"] + (["--cached"] if area == "staged" else []) + DIFF_FLAGS + [
        "--unified=%d" % MAX_DIFF_BYTES, "--"] + paths
    result = run_git(cwd, args, cap=MAX_DIFF_BYTES, extra=[*extra, "-c", "diff.suppressBlankEmpty=false"])
    if result.timed_out or (result.code != 0 and not result.truncated):
        return {"area": area, "path": rel, "state": "error", "message": GIT_ERRORS[fail_state(result)]}
    text = decode(result.out)
    binary = bool(entry["binary"]) or bool(re.search(r"^Binary files .* differ$|^GIT binary patch", text, re.M))
    body = {"area": area, "path": rel, "old_path": entry.get("old_path"), "code": entry["code"], "state": "ok",
            "message": "", "diff": "" if binary else text, "binary": binary, "truncated": result.truncated,
            "added": entry["added"], "deleted": entry["deleted"]}
    if (not binary and not result.truncated and entry["code"] in ("M", "R", "C")
            and entry["added"] == entry["deleted"] == 0 and not re.search(r"^@@ ", text, re.M)):
        unchanged = unchanged_diff_text(cwd, rel)
        if unchanged is not None:
            body["unchanged"] = unchanged
    return body
