"""Filesystem operations for the project browser, scoped to a project root descriptor."""

from contextlib import contextmanager
from datetime import datetime
import errno
import hashlib
import os
import stat
import sys
from urllib.parse import quote_from_bytes
from uuid import uuid4

from kiro_review import MAX_FILE_BYTES, ReviewError, clean_relative, file_revision, is_sensitive, read_text, run_git


DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
TREE_LIMIT = 1000


def project_path(root, value):
    if not isinstance(value, str) or value.startswith("/"):
        raise ReviewError(400, "需要项目内的相对路径")
    path = clean_relative(root, value)
    if path != value or any(part.startswith(".coder-agents-save-") for part in path.split("/")):
        raise ReviewError(400, "路径无效")
    return path


@contextmanager
def opened_dir(base, parts=()):
    fd = os.dup(base) if isinstance(base, int) else os.open(base, DIR)
    try:
        for part in parts:
            child = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def opened_root(root):
    return opened_dir(os.sep, (part for part in root.split(os.sep) if part))


def root_revision(root, fd):
    info = os.fstat(fd)
    return hashlib.sha256(os.fsencode(root) + (":%s:%s" % (info.st_dev, info.st_ino)).encode()).hexdigest()


def mark_ignored(root, path, entries):
    if not entries:
        return
    prefix = "./" + (path + "/" if path else "")
    # The leading ./ prevents a filename like :name from being read as pathspec magic.
    # A trailing slash lets Git apply directory-only rules, even in an empty folder.
    paths = {os.fsencode(prefix + entry["name"] + ("/" if entry["kind"] == "directory" else "")): entry
             for entry in entries}
    data = b"\0".join(paths) + b"\0"
    try:
        result = run_git(root, ["--no-literal-pathspecs", "check-ignore", "-z", "--stdin"],
                         cap=len(data), timeout=2.0, stdin=data)
    except (ReviewError, OSError):
        return  # Git decorations are optional; preserve file browsing without Git.
    if result.code == 0 and not result.timed_out and not result.truncated:
        for name in result.out.split(b"\0"):
            if name in paths:
                paths[name]["ignored"] = True


def read(root, kind, query):
    value = query.get("path", "")
    path = "" if kind == "tree" and value == "" else project_path(root, value)
    with opened_root(root) as base:
        scope = root_revision(root, base)
        if kind == "file":
            return {**read_text(root, path, revision=True, root_fd=base), "root_revision": scope}
        entries = []
        truncated = False
        with opened_dir(base, path.split("/") if path else ()) as directory, os.scandir(directory) as listing:
            for index, entry in enumerate(listing):
                if index >= TREE_LIMIT * 10 or len(entries) >= TREE_LIMIT:
                    truncated = True
                    break
                name = entry.name
                if (is_sensitive([name]) or name.startswith(".coder-agents-save-")
                        or any(0xD800 <= ord(char) <= 0xDFFF for char in name)):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    entries.append({"name": name, "kind": "directory"})
                elif stat.S_ISREG(info.st_mode):
                    entries.append({"name": name, "kind": "file", "revision": file_revision(info)})
        mark_ignored(root, path, entries)
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"]))
        return {"path": path, "entries": entries, "truncated": truncated, "root_revision": scope}


def unchanged(parent, name, expected):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        raise ReviewError(409, "文件已移走或删除，请刷新")
    if not stat.S_ISREG(info.st_mode) or file_revision(info) != expected:
        raise ReviewError(409, "文件已被其他程序修改，请检查磁盘版本后再操作")
    return info


def save(parent, name, payload):
    text = payload.get("text")
    if not isinstance(text, str) or "\0" in text:
        raise ReviewError(400, "只能保存 UTF-8 文本")
    data = text.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ReviewError(413, "文件超过 1 MiB")
    expected = payload.get("revision")
    unchanged(parent, name, expected)
    original = os.open(name, os.O_RDWR | FILE, dir_fd=parent)
    try:
        info = os.fstat(original)
        if not stat.S_ISREG(info.st_mode) or file_revision(info) != expected:
            raise ReviewError(409, "文件已被其他程序修改，请检查磁盘版本后再操作")
        temp = ".coder-agents-save-" + uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
                os.fsync(stream.fileno())
                unchanged(parent, name, expected)
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                return {"revision": file_revision(os.fstat(stream.fileno()))}
        finally:
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(original)


def trash_base():
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")


def trash_file(root, rel, parent, name, expected):
    if not sys.platform.startswith("linux"):
        raise ReviewError(409, "目前仅支持 Linux 系统回收站")
    unchanged(parent, name, expected)
    trash = trash_base()
    if not os.path.isabs(trash):
        raise ReviewError(409, "系统回收站不可用")
    os.makedirs(trash, mode=0o700, exist_ok=True)
    with opened_dir(trash) as base:
        for part in ("files", "info"):
            try:
                os.mkdir(part, 0o700, dir_fd=base)
            except FileExistsError:
                pass
        with opened_dir(base, ("files",)) as files, opened_dir(base, ("info",)) as info:
            for fd in (base, files, info):
                stat_info = os.fstat(fd)
                if stat_info.st_uid != os.getuid() or stat_info.st_mode & 0o022:
                    raise ReviewError(409, "系统回收站目录的权限不安全")
            label = uuid4().hex
            record = label + ".trashinfo"
            fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE, 0o600, dir_fd=info)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (
                        quote_from_bytes(os.fsencode(os.path.join(root, rel)), safe="/"),
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
                unchanged(parent, name, expected)
                os.rename(name, label, src_dir_fd=parent, dst_dir_fd=files)
            except BaseException:
                os.unlink(record, dir_fd=info)
                raise


def mutate(root, payload):
    action = payload.get("action")
    if action not in ("new_file", "new_directory", "save", "trash"):
        raise ReviewError(400, "不支持的文件操作")
    rel = project_path(root, payload.get("path"))
    *parts, name = rel.split("/")
    with opened_root(root) as base:
        if payload.get("root_revision") != root_revision(root, base):
            raise ReviewError(409, "项目目录已变化，请重新打开")
        with opened_dir(base, parts) as parent:
            if action == "new_file":
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE, 0o666, dir_fd=parent)
                os.close(fd)
            elif action == "new_directory":
                os.mkdir(name, dir_fd=parent)
            elif action == "save":
                return save(parent, name, payload)
            else:
                trash_file(root, rel, parent, name, payload.get("revision"))
    return {}


def os_error(error):
    return {
        errno.ENOENT: (404, "目录或文件不存在，请刷新"),
        errno.EEXIST: (409, "同名文件或文件夹已存在"),
        errno.EXDEV: (409, "项目与系统回收站在不同磁盘，无法移到回收站"),
        errno.EACCES: (403, "没有权限执行此操作"),
        errno.EPERM: (403, "没有权限执行此操作"),
        errno.EROFS: (403, "磁盘为只读"),
        errno.ENOTDIR: (403, "目录不可用或包含符号链接"),
        errno.ELOOP: (403, "不支持访问符号链接"),
    }.get(error.errno, (400, "无法完成文件操作"))
