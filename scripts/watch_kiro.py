#!/usr/bin/env python3
"""A fixed local Kiro panel, activated on demand by a systemd user socket."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kiro_catalog
import kiro_review
import project_workspace
import project_git
import github_auth
import kiro_temp
import kiro_usage
import agent_providers
import cli_connections
import agent_roles
import agent_tasks
import agent_diagnostics
import claude_usage
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = 8
RETENTION_DAYS = 7
ACTIVE_STATUSES = {"checking", "running"}
# kiro_temp refusals that mean "not eligible / cannot be proven owned or idle" rather than a failure.
TEMP_SKIP_CODES = {"active", "not_owned", "no_temp", "no_state", "no_run", "missing", "not_accepted", "processes_alive",
                  "namespace_unknown", "namespace_mismatch"}


def temp_turn_state(state, record):
    """pending (eligible), running, needs_review (failed/interrupted and not accepted), unknown, or cleaned. Read-only."""
    if record.get("status") == "cleaned":
        return "cleaned"
    status = state.get("status")
    if status in ACTIVE_STATUSES:
        return "running"
    if status not in kiro_temp.TERMINAL:
        return "unknown"
    if record.get("status") == "accepted" or (status == "completed" and state.get("exit_code") == 0):
        return "pending"
    return "needs_review"
START_WAIT = 25.0  # seconds a send waits for Kiro to accept the model/effort before answering "still starting"
# State location stays compatible with existing Kiro Panel installs; CODER_AGENTS_CONFIG overrides it.
CONFIG = Path(os.environ.get("CODER_AGENTS_CONFIG") or Path.home() / ".local/state/kiro-panel/config.json")
CODEX_STATE = Path.home() / ".codex/.codex-global-state.json"
CODEX_STATE_LIMIT = 32 * 1024 * 1024
# New conversations of 其他对话 run in <DAILY_BASE>/<YYYY-MM-DD>, the date being today's in Hong Kong.
DAILY_BASE = Path.home() / "Documents/Codex"
DAILY_ZONE = "Asia/Hong_Kong"


def daily_zone():
    try:
        return ZoneInfo(DAILY_ZONE)
    except Exception:  # no tz database: Hong Kong has been a fixed UTC+8 since 1979
        return timezone(timedelta(hours=8))


def utc_now():
    return datetime.now(timezone.utc)
SKILL = Path(__file__).resolve().parents[1]
LOCAL_HTTP = build_opener(ProxyHandler({}))
# Static files that may be served under <project>/vendor/. Requests are matched against this exact
# table; a name that is not a key is a 404, so no request text ever reaches the filesystem.
JS_TYPE = "application/javascript; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"
VENDOR_ASSETS = {
    "vendor/marked.umd.js": ("assets/vendor/marked.umd.js", JS_TYPE),
    "vendor/purify.min.js": ("assets/vendor/purify.min.js", JS_TYPE),
    "vendor/monaco-editor.js": ("assets/vendor/monaco-editor.js", JS_TYPE),
    "vendor/monaco-editor.css": ("assets/vendor/monaco-editor.css", "text/css; charset=utf-8"),
    "vendor/monaco-worker.js": ("assets/vendor/monaco-worker.js", JS_TYPE),
    "vendor/monaco-json-worker.js": ("assets/vendor/monaco-json-worker.js", JS_TYPE),
    "vendor/codicon.ttf": ("assets/vendor/codicon.ttf", "font/ttf"),
    "vendor/marked.LICENSE.txt": ("assets/vendor/marked.LICENSE.txt", TEXT_TYPE),
    "vendor/dompurify.LICENSE-Apache-2.0.txt": ("assets/vendor/dompurify.LICENSE-Apache-2.0.txt", TEXT_TYPE),
    "vendor/dompurify.LICENSE-MPL-2.0.txt": ("assets/vendor/dompurify.LICENSE-MPL-2.0.txt", TEXT_TYPE),
}
# The page loads only its own scripts and styles, makes only same-origin requests, and loads no
# images, so untrusted Markdown cannot cause automatic external requests even if sanitizing failed.
PAGE_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "worker-src 'self'; font-src 'self'; connect-src 'self'; img-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def kiro_acp_scrub(text):
    import kiro_acp
    return kiro_acp.scrub(text)


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def read_local(directory, name, limit=2_000_000):
    path = directory / name
    if path.resolve().parent != directory or not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read().decode("utf-8", errors="replace")


def memory_mb():
    try:
        return round(int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None


def timestamp(value):
    """Epoch seconds of an ISO timestamp, or None. Naive values are taken as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def run_activity(run, now):
    """Latest known activity of one run; an active run counts as happening now."""
    if run.get("status") in ACTIVE_STATUSES:
        return now
    return max((t for t in (timestamp(run.get("finished_at")), timestamp(run.get("started_at"))) if t is not None), default=0.0)


def safe_diagnostics(text, state=None):
    state = state if isinstance(state, dict) else {}
    secrets = []
    if state.get("provider") == "claude":
        try:
            from claude_catalog import resolve_config
            for name, value in resolve_config(state.get("cwd")).get("headers", {}).items():
                if name.lower() in {"authorization", "x-api-key"}:
                    secrets.extend((value, value.removeprefix("Bearer ")))
        except (ValueError, OSError):
            pass
    clean = agent_diagnostics.redact(text, secrets=secrets)
    return agent_diagnostics.diagnose(clean, state)


def permission_selection(payload, provider):
    mode = payload.get("permission_mode")
    if mode is not None and (provider != "claude" or mode not in {"dontAsk", "acceptEdits", "plan"}):
        raise ValueError("不支持的工具权限设置；不能绕过 CLI 权限")
    return mode


def group_agents(runs, now=None):
    """Conversations of one run source, most recently ACTIVE first (a completion counts as activity)."""
    now = time.time() if now is None else now
    groups = {}
    for run in runs:
        agent_id = run["agent_id"]
        agent = groups.setdefault(agent_id, {"id": agent_id, "title": run.get("title") or run["id"],
                                             "created_at": run.get("started_at"), "turn_count": 0, "activity": 0.0})
        agent.update(status=run.get("status"), updated_at=run.get("started_at", ""),
                     model=run.get("model"), effort=run.get("effort"), cwd=run.get("cwd"),
                     provider=run.get("provider", "kiro"), role_id=run.get("role_id"), role_name=run.get("role_name"),
                     role_execution_override=run.get("role_execution_override") is True)
        agent["turn_count"] += 1
        agent["activity"] = max(agent["activity"], run_activity(run, now))
        if run.get("session_id"):
            agent["session_id"] = run["session_id"]
    return sorted(groups.values(), key=lambda item: (item["activity"], item.get("updated_at", "")), reverse=True)


def last_activity(runs):
    timestamps = []
    for run in runs:
        value = run.get("finished_at") or run.get("started_at")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            timestamps.append(parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp())
        except (ValueError, OverflowError):
            return None
    return max(timestamps) if timestamps else None


# ---- Per-turn summary, derived from the recorded Kiro stream and run state -------------------------
SUMMARY_MARKERS = ("turn_start", "turn_end", "turn_completion", "turn_usage", "context_usage", "runFinished")
TOKEN_KEYS = {"input": ("inputTokens", "input_tokens"), "output": ("outputTokens", "output_tokens"),
              "total": ("totalTokens", "total_tokens")}


MAX_COUNT = 10 ** 15  # far above any real token count, exactly representable in JSON numbers


def count(value):
    """A reported whole number in [0, MAX_COUNT]; booleans, NaN, infinities and anything else are not counts."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_COUNT else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and 0 <= value <= MAX_COUNT:
        return int(value)
    return None


def finite_amount(value):
    """A finite, non-negative, bounded number (credits, percentages), else None. Cannot overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_COUNT else None
    return value if math.isfinite(value) and 0 <= value <= MAX_COUNT else None


def kiro_meta(event):
    data = event.get("data") if isinstance(event, dict) else None
    update = data.get("update") if isinstance(data, dict) else None
    meta = update.get("_meta") if isinstance(update, dict) else None
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    return kiro if isinstance(kiro, dict) else {}


def reported_tokens(*sources):
    """Token counts a Kiro event explicitly reports for one turn, or None. Nothing is estimated."""
    found = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for holder in (source, source.get("tokenUsage"), source.get("tokens"), source.get("usage")):
            if not isinstance(holder, dict):
                continue
            for name, keys in TOKEN_KEYS.items():
                for key in keys:
                    if count(holder.get(key)) is not None:
                        found[name] = count(holder[key])
    if "total" not in found and "input" in found and "output" in found:
        found["total"] = found["input"] + found["output"]
    return {name: found.get(name) for name in TOKEN_KEYS} if found else None


def reported_credits(*lists):
    for items in lists:
        if not isinstance(items, list):
            continue
        usages = []
        for item in items:
            if not isinstance(item, dict) or finite_amount(item.get("usage")) is None:
                continue
            unit = item.get("unit") if isinstance(item.get("unit"), str) and item.get("unit") else "credit"
            usages.append((item["usage"], unit[:32]))
        if usages:
            total = sum(usage for usage, unit in usages if unit == usages[0][1])
            if math.isfinite(total):
                return {"usage": total, "unit": usages[0][1]}
    return None


def seconds_between(start, end):
    try:
        first, last = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end))
        first, last = (v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v for v in (first, last))
    except (AttributeError, ValueError):
        return None
    elapsed = (last - first).total_seconds()
    return round(elapsed, 3) if elapsed >= 0 else None


def turn_summary(state, events_text):
    """Compact result of one finished turn (one run), or None while it is still running.

    Task duration is the wall-clock time of the whole run (started_at to finished_at), including model
    checks and Kiro startup. Kiro's own turn time comes from its turn_completion event. Credits and
    tokens are taken only from per-turn events of this run, after its last turn_start: the latest
    snapshot of a turn replaces earlier ones, and cumulative session_usage events are never read.
    Context occupancy is a percentage of the context window, not tokens consumed.
    """
    status = state.get("status")
    if status in ACTIVE_STATUSES or status not in {"completed", "failed", "cancelled"}:
        return None
    events = []
    for line in events_text.splitlines():
        if any(marker in line for marker in SUMMARY_MARKERS):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    boundary = max((i for i, e in enumerate(events) if kiro_meta(e).get("kind") == "turn_start"), default=-1)
    if boundary >= 0:
        turn_id = kiro_meta(events[boundary]).get("turnId")
        fresh = [e for e in events[boundary:] if kiro_meta(e).get("turnId") in (None, turn_id)]
    elif state.get("resume_id"):
        fresh = [e for e in events if e.get("type") == "runFinished"]  # earlier turns may be replayed
    else:
        fresh = events
    elapsed_ms = stop_reason = run_status = context = credits = tokens = None
    for event in fresh:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event.get("type") == "runFinished":
            run_status = data.get("status") if isinstance(data.get("status"), str) else run_status
            stop_reason = data.get("stopReason") if isinstance(data.get("stopReason"), str) else stop_reason
            tokens = reported_tokens(data.get("usage"), data) or tokens
            continue
        meta = kiro_meta(event)
        kind = meta.get("kind")
        if kind == "turn_completion":
            elapsed_ms = count(meta.get("elapsedTime")) if count(meta.get("elapsedTime")) is not None else elapsed_ms
            credits = reported_credits(meta.get("promptTurnSummaries")) or credits
            tokens = reported_tokens(meta) or tokens
        elif kind == "turn_usage":
            credits = reported_credits(meta.get("credits"), meta.get("promptTurnSummaries")) or credits
            tokens = reported_tokens(meta) or tokens
        elif kind == "turn_end":
            nested = meta.get("turnEnd")
            reason = meta.get("stopReason") or (nested.get("stopReason") if isinstance(nested, dict) else None)
            stop_reason = reason if isinstance(reason, str) else stop_reason
        elif kind == "context_usage":
            usage = meta.get("contextUsage")
            percent = usage.get("usagePercentage") if isinstance(usage, dict) else meta.get("usagePercentage")
            if finite_amount(percent) is not None and percent <= 1000:
                context = round(float(percent), 1)
    result = status
    reported = (run_status or "").lower()
    if result == "completed":
        if reported in {"cancelled", "canceled", "interrupted"} or stop_reason == "cancelled":
            result = "cancelled"
        elif reported not in {"", "success", "completed", "ok"}:
            result = "failed"
    exit_code = state.get("exit_code")
    exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) and -1000 <= exit_code <= 1000 else None
    denials = state.get("permission_denials")
    return {"result": result, "exit_code": exit_code, "stop_reason": stop_reason,
            "run_status": run_status, "task_seconds": seconds_between(state.get("started_at"), state.get("finished_at")),
            "kiro_elapsed_ms": elapsed_ms, "tokens": tokens, "credits": credits, "context_percent": context,
            "provider": state.get("provider", "kiro"),
            "permission_denial_count": len(denials) if isinstance(denials, list) else 0,
            "cost_usd": finite_amount(state.get("cost_usd")) if state.get("provider") == "claude" else None}


def last_context_percent(events_text):
    """Latest valid context-window occupancy (percent) a run's event stream reports, or None.

    Only the events after the run's last turn_start are considered when the stream has one (a resumed
    stream may replay earlier turns before it); with none, the whole stream counts."""
    events = []
    for line in events_text.splitlines():
        if "context_usage" in line or "turn_start" in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    boundary = max((i for i, e in enumerate(events) if kiro_meta(e).get("kind") == "turn_start"), default=-1)
    percent = None
    for event in events[boundary + 1:] if boundary >= 0 else events:
        meta = kiro_meta(event)
        if meta.get("kind") != "context_usage":
            continue
        usage = meta.get("contextUsage")
        value = usage.get("usagePercentage") if isinstance(usage, dict) else meta.get("usagePercentage")
        if finite_amount(value) is not None and value <= 1000:
            percent = round(float(value), 1)
    return percent


def conversation_context(entries):
    """Context occupancy of ONE conversation: the newest turn that reported a valid value wins.

    `entries` are (run_state, events_text, events_mtime) of this conversation's turns only, oldest first. A new
    turn that has not reported yet falls back to the previous turns of the same conversation."""
    newest = entries[-1][0].get("id") if entries else None
    for state, events, mtime in reversed(entries):
        percent = last_context_percent(events)
        if percent is not None:
            reported = datetime.fromtimestamp(mtime, timezone.utc).isoformat() if mtime else None
            return {"percent": percent, "reported_at": reported, "run_id": state.get("id"),
                    "from_previous_turn": state.get("id") != newest}
    return {"percent": None, "reported_at": None, "run_id": None, "from_previous_turn": False}


# ---- Codex project catalog ------------------------------------------------------------------------
# The Codex desktop app keeps its saved local projects in a private state file. That layout is an
# implementation detail of Codex, not a public API: it is read as data, only the `local-projects`
# field is looked at, and a shape that is not recognised degrades visibly instead of guessing.
PROJECT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]+")
SOURCE_ID = re.compile(r"[0-9a-f]{16}")
OTHER_GROUP = "other"
GROUP_PREFIX = "codex:"
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class CodexProject:
    """A saved Codex project: stable ID, display name and real root directories (canonical paths)."""
    __slots__ = ("id", "name", "roots")

    def __init__(self, project_id, name, roots):
        self.id, self.name, self.roots = project_id, name, tuple(roots)

    @property
    def group_id(self):
        return GROUP_PREFIX + self.id

    def signature(self):
        return (self.id, self.name, self.roots)


class CatalogSnapshot:
    """Immutable view of the catalog. `version` changes only when the project list itself changes."""
    __slots__ = ("projects", "status", "message", "version", "by_group")

    def __init__(self, projects, status, message, version):
        self.projects, self.status, self.message, self.version = tuple(projects), status, message, version
        self.by_group = {p.group_id: p for p in self.projects}


class Catalog:
    """Reads Codex's saved local projects, re-reading only when the file changes.

    A missing, unreadable or malformed file never empties a catalog that was read successfully before:
    the last good list stays in use and the status says it is stale. A readable file whose
    `local-projects` is an empty object is a genuine "no projects" and does empty the list.
    """

    def __init__(self, path):
        self.path = Path(path).expanduser() if path else None
        self.lock = threading.Lock()
        self.projects, self.version = (), 0
        self.status, self.message = "unavailable", "尚未读取 Codex 项目列表"
        self.good_key = None

    def snapshot(self):
        return CatalogSnapshot(self.projects, self.status, self.message, self.version)

    def refresh(self):
        with self.lock:
            if self.path is None:
                return self.fail("unavailable", "未配置 Codex 项目数据文件")
            try:
                info = self.path.stat()
            except FileNotFoundError:
                return self.fail("missing", "没有找到 Codex 的本地项目数据")
            except OSError:
                return self.fail("unreadable", "无法访问 Codex 本地项目数据")
            key = (info.st_mtime_ns, info.st_size, info.st_ino)
            if key == self.good_key and self.status == "ok":
                return self.snapshot()
            if info.st_size > CODEX_STATE_LIMIT:
                return self.fail("unreadable", "Codex 本地项目数据过大，已忽略")
            try:
                data = json.loads(self.path.read_bytes())
            except (OSError, ValueError, RecursionError):
                return self.fail("unreadable", "Codex 本地项目数据暂时无法读取或格式无效")
            local = data.get("local-projects") if isinstance(data, dict) else None
            if not isinstance(local, dict):
                return self.fail("unsupported", "Codex 本地项目数据的格式与预期不同")
            projects, skipped, seen = [], 0, set()
            for key_name, item in local.items():
                project = self.parse(key_name, item)
                if project is None or project.id in seen:
                    skipped += 1
                    continue
                seen.add(project.id)
                projects.append(project)
            if [p.signature() for p in projects] != [p.signature() for p in self.projects]:
                self.version += 1
            self.projects = tuple(projects)
            self.good_key = key
            self.status = "ok"
            self.message = f"已忽略 {skipped} 个无法识别的 Codex 项目条目" if skipped else ""
            return self.snapshot()

    @staticmethod
    def parse(key, item):
        if not isinstance(item, dict):
            return None
        project_id = item.get("id", key)
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            return None
        roots = []
        raw_roots = item.get("rootPaths")
        for raw in raw_roots if isinstance(raw_roots, list) else []:
            if isinstance(raw, str) and raw and "\0" not in raw and os.path.isabs(raw):
                real = Path(os.path.realpath(raw))
                if real not in roots:
                    roots.append(real)
        name = item.get("name")
        name = CONTROL.sub(" ", name).strip()[:120] if isinstance(name, str) else ""
        return CodexProject(project_id, name or (roots[0].name if roots else project_id), roots)

    def fail(self, status, message):
        if self.projects:
            self.status, self.message = "stale", message + "；暂时沿用上次读取的项目列表"
        else:
            self.status, self.message = status, message
        return self.snapshot()


def read_small(path, limit=4096):
    with open(path, "rb") as stream:
        return stream.read(limit).decode("utf-8", errors="replace").strip()


def linked_git_dir(marker):
    """Common git directory for a `.git` *file* (linked worktree or submodule), parsed read-only."""
    text = read_small(marker)
    if not text.startswith("gitdir:"):
        return None
    git_dir = Path(os.path.realpath(Path(marker).parent / text[len("gitdir:"):].strip()))
    commondir = git_dir / "commondir"
    if not commondir.is_file():
        return git_dir  # a submodule or separate git dir is its own repository
    common = Path(os.path.realpath(git_dir / read_small(commondir)))
    # The repository must acknowledge this worktree, so a copied or forged `.git` file proves nothing.
    back = common / "worktrees" / git_dir.name / "gitdir"
    if git_dir.parent != common / "worktrees" or not back.is_file():
        return None
    if os.path.realpath(read_small(back)) != os.path.realpath(marker):
        return None
    return common


def git_common_dir(path, ancestors=True):
    """The common git directory of the repository or worktree containing `path`, else None.

    Pure file parsing (no git subprocess), so it is cheap and cannot run hooks or alter anything.
    With ancestors=False only `path` itself may be the top of a working tree.
    """
    current = Path(path)
    for _ in range(40 if ancestors else 1):
        marker = current / ".git"
        try:
            if marker.is_dir():
                return Path(os.path.realpath(marker))
            if marker.is_file():
                return linked_git_dir(marker)
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


class Source:
    """One directory of run logs plus the default working directory it was registered with.

    A source is either registered through the CLI (`--root`/`--cwd`) or discovered as
    `<Codex root>/work/kiro-runs`. Its ID is opaque and used only to route actions.
    """

    def __init__(self, cwd, root, create=False, strict=True):
        self.cwd = Path(cwd).expanduser().resolve(strict=strict)
        if strict and not self.cwd.is_dir():
            raise ValueError("项目路径必须是目录")
        self.root = Path(root).expanduser().resolve()
        if create:
            self.ensure_root()
        self.id = hashlib.sha256((str(self.cwd) + "\0" + str(self.root)).encode()).hexdigest()[:16]
        self.legacy_sessions = {}
        self.summaries = {}
        self.lock = threading.Lock()

    def ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def delete_conversation(self, agent_id):
        if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
            raise ValueError("Invalid agent ID")
        runs = [run for run in self.list_runs() if run["agent_id"] == agent_id]
        if not runs:
            return 404, {"error": "这条对话已不存在"}
        if any(run.get("status") in ACTIVE_STATUSES for run in runs):
            return 409, {"error": "这个对话有正在执行的任务，请等待任务结束后再删除"}
        directories = []
        for run in runs:
            directory = self.root / run["id"]
            if directory.is_symlink() or directory.resolve().parent != self.root:
                raise ValueError("对话记录路径无效，未删除")
            directories.append(directory)
        metadata = self.root / "conversations.json"
        saved = json.loads(metadata.read_text()) if metadata.is_file() else None
        if saved is not None and not isinstance(saved, dict):
            raise ValueError("对话索引格式无效，未删除")
        for directory in directories:
            shutil.rmtree(directory)
            launch = self.root / (".launch-" + directory.name + ".log")
            launch.unlink(missing_ok=True)
            self.legacy_sessions.pop(directory.name, None)
            self.summaries.pop(directory.name, None)
        if saved is not None and agent_id in saved:
            saved.pop(agent_id)
            write_json(metadata, saved)
        return 200, {"deleted": True, "agent_id": agent_id, "deleted_runs": len(directories)}

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        cutoff = now - RETENTION_DAYS * 24 * 60 * 60
        groups = {}
        for run in self.list_runs():
            groups.setdefault(run["agent_id"], []).append(run)
        deleted = deleted_runs = skipped_active = skipped_unknown = 0
        for agent_id, runs in groups.items():
            if any(run.get("status") in ACTIVE_STATUSES for run in runs):
                skipped_active += 1
                continue
            updated_at = last_activity(runs)
            if updated_at is None:
                skipped_unknown += 1
                continue
            if updated_at < cutoff:
                status, result = self.delete_conversation(agent_id)
                if status == 200:
                    deleted += 1
                    deleted_runs += result["deleted_runs"]
        return {"deleted_conversations": deleted, "deleted_runs": deleted_runs,
                "skipped_active": skipped_active, "skipped_unknown_dates": skipped_unknown}

    def list_runs(self):
        runs = []
        if not self.root.is_dir():
            return runs
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.resolve().parent != self.root:
                continue
            try:
                state = json.loads(read_local(directory.resolve(), "state.json"))
                state = agent_tasks.recover(directory, state)
                state["id"] = directory.name
                state["agent_id"] = state.get("agent_id") or directory.name
                if not isinstance(state["agent_id"], str) or not AGENT_ID.fullmatch(state["agent_id"]):
                    continue
                if not state.get("session_id") and state.get("status") not in {"checking", "running"}:
                    if directory.name not in self.legacy_sessions:
                        self.legacy_sessions[directory.name] = None
                        for line in reversed(read_local(directory.resolve(), "events.jsonl", 65536).splitlines()):
                            try:
                                session = json.loads(line).get("data", {}).get("sessionId")
                                if session:
                                    self.legacy_sessions[directory.name] = session
                                    break
                            except (ValueError, AttributeError):
                                pass
                    if self.legacy_sessions[directory.name]:
                        state["session_id"] = self.legacy_sessions[directory.name]
                runs.append(state)
            except (OSError, ValueError, AttributeError):
                continue
        return sorted(runs, key=lambda item: item.get("started_at", "") if isinstance(item.get("started_at"), str) else "")

    def summary_for(self, state, events):
        key = (state.get("status"), state.get("finished_at"), state.get("exit_code"), len(events))
        cached = self.summaries.get(state["id"])
        if cached and cached[0] == key:
            return cached[1]
        try:
            summary = turn_summary(state, events)
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            summary = None  # optional metrics must never break the API response
        self.summaries[state["id"]] = (key, summary)
        return summary

    @staticmethod
    def recorded_selection(runs, agent_id):
        """(model, effort) the conversation used last, or None. effort None means the model has no effort."""
        mine = [r for r in runs if r.get("agent_id") == agent_id and isinstance(r.get("model"), str)]
        if not mine:
            return None
        last = mine[-1]
        effort = last.get("effort")
        return last["model"], effort if isinstance(effort, str) and effort else None

    def await_start(self, run_id, timeout, interval=0.1):
        """Wait until Kiro has accepted (and received) the prompt, or the run ended without it.

        Returns ("forwarded" | "refused" | "pending", state). `refused` means the run ended and the prompt
        was never forwarded, so nothing ran and the user's draft can be kept."""
        path = self.root / run_id / "state.json"
        deadline = time.monotonic() + timeout
        state = {}
        while True:
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = state or {}
            if state.get("prompt_forwarded"):
                return "forwarded", state
            if state.get("status") in {"failed", "cancelled", "completed"}:
                return "refused", state
            if time.monotonic() >= deadline:
                return "pending", state
            time.sleep(interval)

    def start_turn(self, text, agent_id=None, cwd=None, selection=None, provider="kiro", cli_path=None, permission_mode=None, config_path=None, role=None, role_override=False):
        """Start one CLI turn. A follow-up always runs in the conversation's recorded directory;
        a new conversation runs in `cwd`, which the panel chose from its own catalog, never from the browser."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("请输入要发送给 subagent 的消息")
        runs = self.list_runs()
        agent = None
        if agent_id is not None:
            agent = next((item for item in group_agents(runs) if item["id"] == agent_id), None)
            if not agent:
                raise ValueError("所选 subagent 不存在")
            if agent.get("provider", "kiro") != provider:
                raise ValueError("已有对话不能更换后端")
            if any(run["agent_id"] == agent_id and run.get("status") in ACTIVE_STATUSES for run in runs):
                return 409, {"error": "这个 subagent 正在执行任务，请等待本轮结束后再发送"}
            if not agent.get("session_id"):
                raise ValueError("这个 subagent 尚未建立 CLI 会话，请新建 subagent 并重新发送原始任务")
            cwd = agent.get("cwd")
            if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
                raise ValueError("这个对话原来的工作目录已不存在，无法在原位置继续")
        else:
            if not isinstance(cwd, (str, Path)) or not os.path.isdir(cwd):
                raise ValueError("没有可用的项目目录")
            agent_id = "agent-" + uuid.uuid4().hex[:12]
        cwd = str(cwd)
        if role is not None:
            text = agent_roles.task_prompt(role, text)
        elif agent and agent.get("role_id"):
            role, text = agent_roles.continuation(agent["role_id"], provider, text,
                                                  connections=cli_connections.Connections(config_path.parent / "connections.json") if config_path else None)
            selection = {"model": role["model"], "effort": role["effort"]}
        self.ensure_root()  # lazily, and only because the user explicitly sent a message
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        directory = self.root / run_id
        title = agent["title"] if agent else text.strip().splitlines()[0][:80]
        runner = agent_providers.provider(provider)["runner"]
        if not cli_path and not shutil.which(agent_providers.provider(provider)["binary"]):
            raise ValueError("找不到后端 CLI：" + provider)
        command = [sys.executable, str(SKILL / "scripts" / runner), "--cwd", cwd,
                   "--run-dir", str(directory), "--agent-id", agent_id, "--title", title]
        if cli_path:
            command += ["--cli-path", cli_path]
        if permission_mode is not None:
            command += ["--permission-mode", permission_mode]
        if agent:
            command += ["--resume-id", agent["session_id"]]
        if selection:  # explicit, backend-validated; without it the runner uses its Sonnet 5.5 / medium default
            command += ["--model", selection["model"]]
            command += ["--effort", selection["effort"]] if selection["effort"] else ["--no-effort"]
        launch_path = self.root / (".launch-" + run_id + ".log")
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as task, launch_path.open("w") as output:
            launch_path.chmod(0o600)
            task.write(text)
            task.seek(0)
            environment = dict(os.environ)
            if config_path:
                environment["CODER_AGENTS_CONFIG"] = str(config_path)
            for key in ("CODER_AGENTS_ROLE_ID", "CODER_AGENTS_ROLE_NAME", "CODER_AGENTS_ROLE_OVERRIDE"):
                environment.pop(key, None)
            if role:
                environment.update(CODER_AGENTS_ROLE_ID=role["id"], CODER_AGENTS_ROLE_NAME=role["name"])
                if role_override:
                    environment["CODER_AGENTS_ROLE_OVERRIDE"] = "1"
            process = subprocess.Popen(command, cwd=cwd, stdin=task, stdout=output, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
        # Reap children even after the HTTP request has returned.
        threading.Thread(target=process.wait, daemon=True).start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (directory / "state.json").exists():
                return 201, {"agent_id": agent_id, "run_id": run_id, "source": self.id}
            if process.poll() is not None:
                raise ValueError("CLI 未能启动，请检查本地启动日志 " + str(launch_path))
            time.sleep(.05)
        return 202, {"agent_id": agent_id, "run_id": run_id, "source": self.id}


class Panel:
    def __init__(self, config, config_path, codex_state=None, daily_base=None, clock=None, models=None, usage=None,
                 start_wait=None):
        self.config = config
        self.start_wait = START_WAIT if start_wait is None else start_wait
        self.port = int(config["port"])
        if not 1024 <= self.port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{24,}", config["token"]):
            raise ValueError("Invalid panel configuration")
        self.prefix = "/" + config["token"] + "/"
        self.address = f"http://127.0.0.1:{self.port}"
        # Browsers get an HttpOnly cookie derived from the private token; the token itself is never sent.
        self.cookie_name = f"kiro_panel_{self.port}"  # cookies ignore ports, so the name carries it
        self.cookie_value = hmac.new(config["token"].encode(), b"kiro-panel-browser-session-v1", hashlib.sha256).hexdigest()
        self.config_path = config_path
        # Background-only settings (never from the browser). `clock` returns the current aware datetime.
        configured_base = daily_base or config.get("daily_base")
        self.daily_base = Path(configured_base).expanduser() if configured_base else DAILY_BASE
        if not self.daily_base.is_absolute():
            raise ValueError("每日目录的基础路径必须是绝对路径")
        self.clock = clock or utc_now
        self.daily_ids = set()  # sources created by 其他对话; they never belong to a Codex project
        self.daily_pending = {}  # today's source before its first successful send; not in the registry
        self.registry = config_path.parent / "projects.json"
        self.catalog = Catalog(codex_state or config.get("codex_state") or CODEX_STATE)
        self.fixture = config.get("fixture") if isinstance(config.get("fixture"), dict) else None
        self.connections = cli_connections.Connections(config_path.parent / "connections.json")
        self.github = github_auth.GitHubAuth()
        self.roles = agent_roles.RoleStore(connections=self.connections)
        self.default_provider = self.connections.snapshot()["default_provider"]
        self.connection_slots = threading.BoundedSemaphore(1)
        self.injected_services = self.fixture is not None or models is not None
        if self.injected_services and self.default_provider is None:
            self.default_provider = "kiro"
        self.claude_models = agent_providers.ClaudeModels()
        self.claude_usage = claude_usage.UsageService()
        self.models, self.usage = models, usage
        self.provider_lock = threading.Lock()
        self.sources = {}
        self.aliases = {}
        self.retained = set()  # IDs of sources found by discovery or created by a send, kept in the registry
        self.discovered_at = (None, 0.0)
        self.resolved = {}
        self.default_id = None
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.last_seen = time.monotonic()
        self.cleanup_lock = threading.Lock()
        self.temp_lock = threading.Lock()  # one managed-temp cleanup (single or bulk) at a time
        self.review_slots = threading.BoundedSemaphore(3)  # read-only file/Git reviews running at once
        self.workspace_writer = threading.Lock()
        self.cleanup_path = config_path.parent / "cleanup.json"
        self.last_cleanup = {}
        if self.cleanup_path.is_file():
            try:
                self.last_cleanup = json.loads(self.cleanup_path.read_text())
            except (OSError, ValueError):
                pass
        if self.registry.exists():
            saved = json.loads(self.registry.read_text())
            for item in saved.get("projects", []):
                try:
                    # A source whose directory has vanished still shows its history.
                    self.add_source(Source(item["cwd"], item["root"], strict=False))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            for item in saved.get("discovered", []):
                try:
                    source = self.add_source(Source(item["cwd"], item["root"], strict=False))
                    self.retained.add(source.id)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            for item in saved.get("daily", []):
                try:
                    source = self.add_source(Source(item["cwd"], item["root"], strict=False))
                    self.daily_ids.add(source.id)
                    self.retained.discard(source.id)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            self.default_id = saved.get("default")
        if not self.registered_sources() and isinstance(config.get("default_project"), dict):
            self.register(config["default_project"])
        self.default_id = self.aliases.get(self.default_id, self.default_id)
        if self.default_id not in self.sources or self.default_id in self.daily_ids:
            registered = self.registered_sources()  # empty on a fresh install: only 其他对话 is available
            self.default_id = registered[0].id if registered else None

    def registered_sources(self):
        return [s for s in self.sources.values() if s.id not in self.daily_ids]

    def add_source(self, source):
        """Keep one source per log root, however the path was spelled; the later registration wins."""
        with self.lock:
            for old in [s for s in self.sources.values() if s.root == source.root and s.id != source.id]:
                del self.sources[old.id]
                self.retained.discard(old.id)
                if old.id in self.daily_ids:  # the marker follows the log root to its replacement
                    self.daily_ids.discard(old.id)
                    self.daily_ids.add(source.id)
                for key, target in self.aliases.items():
                    if target == old.id:
                        self.aliases[key] = source.id  # keep chains of replaced IDs pointing at the live source
                self.aliases[old.id] = source.id
            self.aliases.pop(source.id, None)  # an ID that is live again must not redirect elsewhere
            return self.sources.setdefault(source.id, source)

    def save_registry(self):
        """Persist registered and retained sources in the panel's own state (never inside a project)."""
        entry = lambda s: {"cwd": str(s.cwd), "root": str(s.root)}
        write_json(self.registry, {"default": self.default_id,
                                   "projects": [entry(s) for s in self.sources.values() if s.id not in self.retained | self.daily_ids],
                                   "discovered": [entry(s) for s in self.sources.values() if s.id in self.retained],
                                   "daily": [entry(s) for s in self.sources.values() if s.id in self.daily_ids]})

    def retain(self, source):
        """Remember a discovered or newly created log directory so its history outlives its Codex project."""
        with self.lock:
            if source.id in self.sources:
                return
            source = self.add_source(source)
            self.retained.add(source.id)
            try:
                self.save_registry()
            except OSError:
                pass  # kept in memory; the next successful write persists it

    def register(self, payload):
        if not isinstance(payload.get("cwd"), str) or not isinstance(payload.get("root"), str):
            raise ValueError("cwd and root are required")
        if not Path(payload["cwd"]).is_absolute() or not Path(payload["root"]).is_absolute():
            raise ValueError("项目和日志路径必须是绝对路径")
        source = Source(payload["cwd"], payload["root"], create=True)
        with self.lock:
            source = self.sources.pop(source.id, None) or source  # re-registering moves it to the end: most recent
            self.retained.discard(source.id)  # an explicit registration takes over from a retained entry
            source = self.add_source(source)
            if source.id not in self.daily_ids:
                self.default_id = source.id
            self.save_registry()
            write_json(source.root / "viewer.json", {"url": self.address + "/", "entry_url": self.address + "/",
                       "live_template": True, "viewer_version": VERSION})
        return source

    # -- Catalog association -------------------------------------------------------------------------
    def discover(self, snapshot):
        """Find default log directories `<project root>/work/kiro-runs` that no registration covers yet.

        Finding is read-only for the project. A found directory is retained in the panel's own registry,
        so its history stays reachable (under 其他对话) after the Codex project is removed or the project
        list cannot be read after a restart. Scans repeat at most every five seconds unless the list changes.
        """
        with self.lock:
            try:
                registered = agent_tasks.registered_sources()
            except (OSError, ValueError):
                registered = []
            for entry in registered:
                try:
                    source = Source(entry["cwd"], entry["root"], strict=False)
                    if source.id not in self.sources:
                        self.add_source(source)
                except (OSError, ValueError, KeyError):
                    continue
            now = time.monotonic()
            if self.discovered_at[0] == snapshot.version and now - self.discovered_at[1] < 5:
                return
            self.discovered_at = (snapshot.version, now)
            known = {s.root for s in self.sources.values()}
            for project in snapshot.projects:
                for root in project.roots:
                    logs = root / "work" / "kiro-runs"
                    try:
                        if logs in known or not logs.is_dir() or Path(os.path.realpath(logs)) != logs:
                            continue  # a symlinked `work` or `kiro-runs` could point outside the project
                        self.retain(Source(root, logs, strict=False))
                    except OSError:
                        continue

    def all_sources(self, snapshot):
        self.discover(snapshot)
        with self.lock:
            return list(self.sources.values())

    def source_by_id(self, source_id, snapshot=None):
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            return None
        with self.lock:
            source_id = self.aliases.get(source_id, source_id)
            if source_id in self.sources:
                return self.sources[source_id]
        return next((s for s in self.all_sources(snapshot or self.catalog.snapshot()) if s.id == source_id), None)

    def group_for(self, cwd, snapshot):
        """Codex group ID for a working directory, or None. Never matches on a folder name.

        1. Real (symlink-resolved) containment in a Codex root; the deepest root wins. Two projects
           claiming the same deepest root is ambiguous and stays unassociated.
        2. Otherwise, a git worktree whose repository is the same as that of exactly one Codex root.
        """
        canonical = Path(os.path.realpath(cwd))
        key = (canonical, snapshot.version)
        now = time.monotonic()
        cached = self.resolved.get(key)
        if cached and (cached[1] or now - cached[0] < 30):
            return cached[2]
        best, depth, tied = None, -1, False
        for project in snapshot.projects:
            for root in project.roots:
                if canonical == root or root in canonical.parents:
                    if len(root.parts) > depth:
                        best, depth, tied = project, len(root.parts), False
                    elif len(root.parts) == depth and project is not best:
                        tied = True
        explicit = best is not None
        result = None if tied else best.group_id if explicit else None
        if not explicit:
            common = git_common_dir(canonical)
            if common is not None:
                owners = {p.group_id for p in snapshot.projects for root in p.roots if git_common_dir(root, ancestors=False) == common}
                result = next(iter(owners)) if len(owners) == 1 else None
        if len(self.resolved) > 2000:
            self.resolved.clear()
        self.resolved[key] = (now, explicit, result)
        return result

    # -- Views and actions ---------------------------------------------------------------------------
    def group_of(self, source, snapshot):
        """Group of a source: 其他对话 for the panel's own daily sources (explicitly marked, whatever Codex roots
        contain their path), otherwise by the working directory's association with a Codex project."""
        if source.id in self.daily_ids:
            return OTHER_GROUP
        return self.group_for(source.cwd, snapshot) or OTHER_GROUP

    def daily_paths(self):
        """(date, cwd, log root) of today's 其他对话 directory. Pure computation: touches no file.

        The date is the Hong Kong calendar date of the panel's own clock, never anything the browser sends."""
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        day = now.astimezone(daily_zone()).strftime("%Y-%m-%d")
        cwd = Path(os.path.realpath(self.daily_base)) / day
        return day, cwd, cwd / "work" / "kiro-other-runs"  # never a project's own work/kiro-runs

    def prepare_daily(self, cwd, root):
        """Create (mode 0700) and verify the daily directory right before the first message is launched.

        Every component must be a real directory below the base: a symlink or a plain file in the way is an
        error, never silently replaced and never a reason to fall back to another working directory."""
        try:
            if self.daily_base.is_symlink():
                raise ValueError("每日目录的基础路径是符号链接，已拒绝，未启动 Kiro：" + str(self.daily_base))
            self.daily_base.mkdir(parents=True, exist_ok=True)
            if Path(os.path.realpath(self.daily_base)) != cwd.parent:
                raise ValueError("每日目录的基础路径已变化，请重试")
            for path, mode in ((cwd, 0o700), (cwd / "work", 0o700), (root, 0o700)):
                if path.is_symlink():
                    raise ValueError("拒绝使用符号链接目录，未启动 Kiro：" + str(path))
                if path.exists() and not path.is_dir():
                    raise ValueError("同名文件挡住了所需目录，未启动 Kiro：" + str(path))
                path.mkdir(mode=mode, exist_ok=True)
                if Path(os.path.realpath(path)) != path:
                    raise ValueError("目录越出预期位置，未启动 Kiro：" + str(path))
            if not os.access(root, os.W_OK | os.X_OK) or not os.access(cwd, os.W_OK | os.X_OK):
                raise ValueError("没有写入权限，未启动 Kiro：" + str(cwd))
        except OSError as error:
            raise ValueError("无法创建今天的对话目录 " + str(cwd) + "：" + (error.strerror or str(error))) from error

    def daily_source(self, cwd, root):
        """The one Source object for a daily directory, so concurrent sends of that day share its lock."""
        candidate = Source(cwd, root, strict=False)
        with self.lock:
            return self.sources.get(candidate.id) or self.daily_pending.setdefault(candidate.id, candidate)

    def retain_daily(self, source):
        """Register a daily source in the panel's own registry, marked as 其他对话 (not in any Codex project list)."""
        with self.lock:
            self.daily_pending.pop(source.id, None)
            source = self.add_source(source)
            self.retained.discard(source.id)
            self.daily_ids.add(source.id)
            try:
                self.save_registry()
            except OSError:
                pass  # kept in memory; the next successful write persists it

    def view(self, group_id="", source_id="", agent_id="", draft_provider=None):
        snapshot = self.catalog.refresh()
        sources = self.all_sources(snapshot)
        now = time.time()
        members = {p.group_id: [] for p in snapshot.projects}
        members[OTHER_GROUP] = []
        for source in sources:
            members[self.group_of(source, snapshot)].append(source)
        runs_of, groups, active = {}, [], 0
        for project in snapshot.projects:
            main = project.roots[0] if project.roots else None
            exists = bool(main and main.is_dir())
            groups.append({"id": project.group_id, "kind": "codex", "name": project.name,
                           "path": str(main) if main else "", "roots": [str(r) for r in project.roots],
                           "exists": exists, "can_create": exists,
                           "note": "" if exists else "项目目录不存在或无法访问，无法新建对话"})
        day, daily_cwd, _ = self.daily_paths()  # read-only: nothing is created until a message is sent
        groups.append({"id": OTHER_GROUP, "kind": "other", "name": "其他对话", "path": str(daily_cwd), "daily_date": day,
                       "roots": [], "exists": True, "can_create": True, "note": ""})
        for group in groups:
            conversations = []
            for source in members[group["id"]]:
                with source.lock:
                    runs = source.list_runs()
                runs_of[source.id] = runs
                active += sum(run.get("status") in ACTIVE_STATUSES for run in runs)
                for agent in group_agents(runs, now):
                    busy = any(r["agent_id"] == agent["id"] and r.get("status") in ACTIVE_STATUSES for r in runs)
                    conversations.append({"key": source.id + ":" + agent["id"], "source": source.id, "id": agent["id"],
                                          "title": agent["title"], "status": agent["status"], "created_at": agent["created_at"],
                                          "activity": agent["activity"], "cwd": agent.get("cwd"), "turn_count": agent["turn_count"],
                                          "deletable": not busy, "provider": agent["provider"],
                                          "role_id": agent.get("role_id"), "role_name": agent.get("role_name")})
            conversations.sort(key=lambda item: (item["activity"], item["key"]), reverse=True)
            group["conversations"] = conversations
        groups = [g for g in groups if g["kind"] == "codex" or g["conversations"] or g["can_create"]]
        group_of_source = {s.id: gid for gid, ss in members.items() for s in ss}
        selected, turns, log_root = None, [], ""
        source = self.source_by_id(source_id, snapshot) if source_id else None
        if source and source.id in runs_of and agent_id and AGENT_ID.fullmatch(agent_id):
            runs = runs_of[source.id]
            agent = next((a for a in group_agents(runs, now) if a["id"] == agent_id), None)
            if agent:
                agent["deletable"] = not any(r["agent_id"] == agent_id and r.get("status") in ACTIVE_STATUSES for r in runs)
                agent.update(source=source.id, group=group_of_source[source.id], key=source.id + ":" + agent_id)
                selected, log_root = agent, str(source.root)
                context_entries = []
                for state in runs:
                    if state["agent_id"] != agent_id or not re.fullmatch(r"[A-Za-z0-9_-]+", state["id"]):
                        continue
                    directory = (source.root / state["id"]).resolve()
                    events = read_local(directory, "events.jsonl")
                    try:
                        mtime = (directory / "events.jsonl").stat().st_mtime
                    except OSError:
                        mtime = None
                    context_entries.append((state, events, mtime))
                    shown = dict(state)
                    if isinstance(shown.get("temp"), dict):  # the ownership hash stays server-side
                        shown["temp"] = {k: v for k, v in shown["temp"].items() if k != "token_sha256"}
                    diagnostics = safe_diagnostics(read_local(directory, "stderr.log"), state) if state.get("status") in {"failed", "cancelled"} else None
                    turns.append({"state": shown, "task": read_local(directory, "task.txt"), "events": events,
                                  "summary": source.summary_for(state, events), "temp": kiro_temp.summarize(state),
                                  "diagnostic": diagnostics, "diagnostics": safe_diagnostics(read_local(directory, "stderr.log"), state)["detail"]})
                recorded = source.recorded_selection(runs, agent_id)
                selected["config"] = ({"model": recorded[0], "effort": recorded[1], "source": "conversation"} if recorded else None)
                if agent.get("role_id"):
                    try:
                        if agent["role_execution_override"]:
                            self.enabled_role(agent["role_id"])
                            selected["role_error"] = "" if recorded else "没有记录可继续使用的模型，请新建对话"
                        else:
                            role = self.roles.get(agent["role_id"], executable=True, connections=self.connections)
                            selected["role_error"] = "角色已更换 CLI，请新建对话" if role["provider"] != agent["provider"] else ""
                            selected["config"] = {"model": role["model"], "effort": role["effort"], "source": "role"}
                    except ValueError as error:
                        selected["role_error"] = str(error)
                selected["context"] = conversation_context(context_entries)
        default_source = self.sources.get(self.default_id)
        try:  # read-only: a missing or unreadable setting is reported as unknown (None), never inferred
            flag = json.loads(self.config_path.read_text()).get("auto_cleanup_enabled")
            auto_cleanup = flag if isinstance(flag, bool) else None
        except (OSError, ValueError, AttributeError):
            auto_cleanup = None
        composer_roles = []
        if not agent_id:
            try:
                composer_roles = [{key: role[key] for key in ("id", "name", "provider", "model", "effort")}
                                  for role in self.roles.snapshot()["roles"] if role["enabled"]]
            except ValueError:
                pass  # role settings report configuration errors; ordinary conversations remain available
        viewer = {"version": VERSION, "cleanup": self.last_cleanup, "memory_mb": memory_mb(),
                  "auto_cleanup_enabled": auto_cleanup, "retention_days": RETENTION_DAYS,
                  "idle_timeout_seconds": self.config.get("idle_timeout", 120), "active_tasks": active,
                  "preview": self.fixture.get("label") if self.fixture else None,
                  "sends_disabled": bool(self.fixture and self.fixture.get("sends_disabled")),
                  "providers": self.connections.snapshot()["providers"], "default_provider": self.default_provider,
                  "connections_required": not self.injected_services, "composer_roles": composer_roles,
                  "account": self.account_view(selected.get("provider") if selected else (draft_provider if draft_provider in {"claude", "kiro"} else None),
                                               cwd=self.context_cwd(group_id, source_id, strict=False))}
        return {"groups": groups, "selected": selected, "turns": turns, "root": log_root,
                "source_groups": group_of_source,
                "default": {"group": group_of_source.get(default_source.id, OTHER_GROUP) if default_source else "",
                            "source": default_source.id if default_source else ""},
                "catalog": {"status": snapshot.status, "message": snapshot.message}, "viewer": viewer}

    # -- Project workspace and read-only conversation review ----------------------------------------
    def workspace_root(self, group, *, write=False):
        snapshot = self.catalog.refresh()
        if write and snapshot.status != "ok":
            raise kiro_review.ReviewError(409, "项目列表暂不可用，无法确认写入位置")
        project = snapshot.by_group.get(group) if isinstance(group, str) else None
        if not project or not project.roots:
            raise kiro_review.ReviewError(404, "找不到项目目录，请刷新页面")
        main = str(project.roots[0])
        root = kiro_review.authorised_directory(main)
        if root != main or kiro_review.is_sensitive(root.split(os.sep)):
            raise kiro_review.ReviewError(403, "项目根目录不可用")
        return root

    def workspace(self, kind, params):
        group = params.get("group", "")
        if not self.review_slots.acquire(blocking=False):
            return 429, {"error": "文件请求过多，请稍后重试"}
        try:
            if kind in ("write", "git"):
                with self.workspace_writer:
                    root = self.workspace_root(group, write=True)
                    if kind == "git":
                        if self.fixture and params.get("action") in ("fetch", "pull", "push"):
                            raise kiro_review.ReviewError(409, "预览模式无法同步远端")
                        result = project_git.mutate(root, params)
                    else:
                        result = project_workspace.mutate(root, params)
            else:
                root = self.workspace_root(group)
                result = project_workspace.read(root, kind, params)
            return 200, {"ok": True, "group": group, "cwd": root, **result}
        except kiro_review.ReviewError as error:
            return error.status, {"error": error.message}
        except OSError as error:
            status, message = project_workspace.os_error(error)
            return status, {"error": message}
        except ValueError:
            return 400, {"error": "Git 操作参数无效" if kind == "git" else "文件名称或内容无效"}
        finally:
            self.review_slots.release()

    def review_target(self, group_id, source_id, agent_id):
        """(real working directory, recorded spelling) of one conversation, validated entirely by the panel.

        The browser only names group + source + agent; the directory is what that conversation's own runs recorded,
        and the source/agent/group triple must agree with the panel's catalog. Anything else is refused."""
        if not (isinstance(group_id, str) and isinstance(source_id, str) and isinstance(agent_id, str)
                and group_id and SOURCE_ID.fullmatch(source_id) and AGENT_ID.fullmatch(agent_id)):
            raise kiro_review.ReviewError(400, "对话标识无效")
        snapshot = self.catalog.snapshot()
        source = self.source_by_id(source_id, snapshot)
        if source is None:
            raise kiro_review.ReviewError(404, "找不到这个对话的来源，请刷新页面")
        if self.group_of(source, snapshot) != group_id:
            raise kiro_review.ReviewError(409, "对话所属项目已变化，请刷新页面")
        with source.lock:
            runs = source.list_runs()
        agent = next((a for a in group_agents(runs) if a["id"] == agent_id), None)
        if agent is None:
            raise kiro_review.ReviewError(404, "这条对话已不存在")
        recorded = agent.get("cwd")
        return kiro_review.authorised_directory(recorded), (recorded if isinstance(recorded, str) else None)

    def review(self, kind, query):
        """Answer one read-only review request: (HTTP status, JSON body). Never writes anything."""
        echo = {"group": query.get("group", ""), "source": query.get("source", ""), "agent": query.get("agent", "")}
        if not self.review_slots.acquire(blocking=False):
            return 429, {"error": "审阅请求过多，请稍后重试", **echo}
        try:
            project_scope = query.get("scope") == "project"
            if project_scope:
                cwd, logical = self.workspace_root(echo["group"]), None
            else:
                cwd, logical = self.review_target(echo["group"], echo["source"], echo["agent"])
            if kind == "status":
                body = kiro_review.git_status(cwd)
                if project_scope and body.get("repo"):
                    body["controls"] = project_git.controls(cwd, body["repo"])
            elif kind == "file":
                rel = (project_workspace.project_path(cwd, query.get("path", "")) if project_scope
                       else kiro_review.clean_relative(cwd, query.get("path", ""), logical))
                body = kiro_review.read_text(cwd, rel)
            elif kind == "diff":
                path = query.get("path", "")
                if project_scope:
                    path = project_workspace.project_path(cwd, path)
                body = kiro_review.git_diff(cwd, query.get("area", ""), path)
            else:
                return 404, {"error": "未知的审阅请求", **echo}
            body["cwd"] = cwd
            return 200, {**body, **echo}
        except kiro_review.ReviewError as error:
            return error.status, {"error": error.message, **echo}
        except (OSError, ValueError, subprocess.SubprocessError):
            return 500, {"error": "读取失败，请稍后重试", **echo}  # never echo paths or git's own messages
        finally:
            self.review_slots.release()

    def new_target(self, group_id, snapshot):
        """(source, cwd) for a new conversation in a group the panel itself recognises. Nothing comes from the browser."""
        if group_id == OTHER_GROUP:
            _, cwd, root = self.daily_paths()
            self.prepare_daily(cwd, root)  # only reached when a message is being sent
            return self.daily_source(cwd, root), cwd
        project = snapshot.by_group.get(group_id) if isinstance(group_id, str) else None
        if project is None or not project.roots:
            raise ValueError("找不到这个 Codex 项目，请刷新页面")
        main = project.roots[0]
        if not main.is_dir():
            raise ValueError("项目目录不存在或无法访问：" + str(main))
        candidates = [s for s in self.all_sources(snapshot) if s.cwd == main and self.group_of(s, snapshot) == group_id]
        default_logs = main / "work" / "kiro-runs"
        source = next((s for s in candidates if s.root == default_logs), None) or (candidates[-1] if candidates else None)
        if source is None:
            logs = Path(os.path.realpath(default_logs))
            if logs != default_logs:
                raise ValueError("项目内的 work/kiro-runs 指向项目之外，已拒绝")
            source = Source(main, logs, strict=False)
        return source, main

    def kiro_services(self):
        if self.fixture:
            return
        if not self.injected_services:
            self.connections.executable("kiro")
        with self.provider_lock:
            if self.models is None or self.usage is None:
                import run_kiro
                environment = run_kiro.kiro_environment()
                if not self.injected_services:
                    environment["KIRO_CLI"] = self.connections.executable("kiro")
                if self.models is None:
                    self.models = kiro_catalog.default_catalog(environment, self.config_path.parent)
                if self.usage is None:
                    self.usage = kiro_usage.UsageService(kiro_usage.AcpUsageFetcher(environment))

    def context_cwd(self, group_id=None, source_id=None, strict=True):
        if not group_id:
            return None
        if group_id == OTHER_GROUP and not source_id:
            return str(self.daily_paths()[1])
        snapshot = self.catalog.refresh()
        if source_id:
            source = self.source_by_id(source_id, snapshot)
            if source is not None and self.group_of(source, snapshot) == group_id:
                return str(source.cwd)
        else:
            project = next((p for p in snapshot.projects if p.group_id == group_id), None)
            if project and project.roots:
                return str(project.roots[0])
        if strict:
            raise ValueError("项目目录不可用")
        return None

    def account_view(self, provider, cwd=None):
        if provider is None:
            return None
        if not self.injected_services:
            try:
                self.connections.executable(provider)
            except ValueError:
                return {"status": "unsupported", "message": "请在设置中连接 CLI；连接检查不会调用模型"}
        if provider == "claude":
            return self.claude_usage.snapshot(cwd=cwd)
        if not self.injected_services:
            # Opening or polling a page must never trigger token refresh/login.
            return self.usage.snapshot() if self.usage else {
                "status": "idle", "data": None, "message": "点击刷新主动读取 Kiro 账户额度"}
        # A machine without Kiro can still read its old Kiro conversations.
        if not self.fixture and (not self.injected_services or shutil.which("kiro-cli")):
            self.kiro_services()
        return self.usage.poll() if self.usage else None

    def resolve_selection(self, payload, recorded, provider="kiro", cwd=None, role=False):
        """The validated {"model", "effort"} to launch with, or None for the runner's own default.

        The browser's values are only a request: they must be in the account's real catalog, and an effort is
        accepted only if that model supports it (a model without effort accepts none)."""
        has_model, has_effort = "model" in payload, "effort" in payload
        if not has_model and not has_effort:
            if recorded is None:
                if provider == "kiro" and not self.connections.defaults_configured(provider):
                    return None  # Kiro selection is verified by the startup relay
                defaults = self.connections.defaults(provider)
                model, effort = defaults["model"], defaults["effort"]
            else:
                model, effort = recorded
        elif has_model and has_effort:
            model, effort = payload["model"], payload["effort"]
        else:
            raise kiro_catalog.ConfigError("selection_incomplete", "模型和思考强度必须一起提交（不支持强度的模型请提交 null）")
        if provider == "kiro" and not self.injected_services and (model, effort) == (
                kiro_catalog.DEFAULT_MODEL, kiro_catalog.DEFAULT_EFFORT):
            # The runner's ACP relay verifies the pinned selection before forwarding the task.
            self.connections.executable("kiro")
            return {"model": model, "effort": effort}
        if provider == "kiro":
            self.kiro_services()
        models = self.claude_models if provider == "claude" else self.models
        if not models:
            raise kiro_catalog.ConfigError("catalog_unavailable", "当前无法验证模型选择，未发送消息")
        try:
            model, effort = models.validate(model, effort, cwd=cwd) if provider == "claude" else models.validate(model, effort)
            recorded_alias = recorded == (model, effort) and model in {"sonnet", "opus", "haiku"}
            if provider == "claude" and not self.injected_services and not role and not recorded_alias:
                catalog = models.snapshot(cwd=cwd)
                if catalog.get("status") == "ok":
                    models.validate(model, effort, cwd=cwd, check_catalog=True)
        except kiro_catalog.ConfigError as error:
            if provider == "kiro" and error.code == "catalog_unavailable" and (model, effort) == (kiro_catalog.DEFAULT_MODEL, kiro_catalog.DEFAULT_EFFORT):
                return {"model": model, "effort": effort}  # the default is still checked by the runner and relay
            raise
        return {"model": model, "effort": effort}

    def refusal(self, source, outcome_state, agent_id, run_id):
        """409 body for a run that ended before Kiro accepted the prompt: nothing ran, the draft stays."""
        reason = outcome_state.get("startup")
        if outcome_state.get("provider") == "claude":
            submitted = outcome_state.get("prompt_submitted", False)
            return 409, {"error": "Claude 启动或执行失败，请查看诊断日志；" + (
                         "任务可能已执行部分操作，请先检查工作目录" if submitted else "尚未启动任务"),
                         "refused": not submitted, "reason": "claude_failed", "agent_id": agent_id,
                         "run_id": run_id, "source": source.id}
        model, effort = outcome_state.get("model"), outcome_state.get("effort")
        known = {"model_unavailable": f"Kiro 的完整模型目录中没有 {model}，消息没有发送",
                 "effort_unsupported": f"{model} 不支持思考强度 {effort or '（无）'}，消息没有发送",
                 "effort_required": f"{model} 需要选择思考强度，消息没有发送",
                 "catalog_timeout": "Kiro 的模型目录加载超时（多为网络、登录或代理问题），消息没有发送，可稍后重试",
                 "set_failed": "Kiro 拒绝了所选模型或强度，消息没有发送",
                 "confirm_timeout": "Kiro 没有及时确认所选模型和强度，消息没有发送",
                 "server_exited": "Kiro 服务在启动阶段退出，消息没有发送",
                 "temp_unavailable": "无法创建受管理的临时目录（可能磁盘空间不足），消息没有发送"}
        tail = ""
        try:
            tail = kiro_acp_scrub(read_local((source.root / run_id).resolve(), "stderr.log", 600).strip()[-300:])
        except OSError:
            pass
        text = known.get(reason) or "Kiro 没有接受这次启动，消息没有发送"
        return 409, {"error": text + (f"。详情：{tail}" if tail else ""), "refused": True, "reason": reason,
                     "agent_id": agent_id, "run_id": run_id, "source": source.id}

    def enabled_role(self, role_id):
        role = self.roles.get(role_id)
        if not role["enabled"]:
            raise ValueError("角色已停用，请在设置中启用：" + role_id)
        return role

    def send(self, payload):
        if self.fixture and self.fixture.get("sends_disabled"):
            return 403, {"error": "这是测试预览，已禁用发送；不会启动 Kiro"}
        text, group_id = payload.get("text"), payload.get("group")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("请输入要发送给 subagent 的消息")
        if not isinstance(group_id, str):
            raise ValueError("缺少项目分组")
        # Connection is checked before creating directories or querying any CLI metadata.
        provider = payload.get("provider")
        role = None
        role_override = False
        if "role_id" in payload:
            if payload.get("agent"):
                raise ValueError("已有对话不能更换角色")
            role_id = payload["role_id"]
            if not isinstance(role_id, str) or not role_id:
                raise ValueError("角色 ID 无效")
            role_override = "model" in payload or "effort" in payload
            if role_override:
                role = self.enabled_role(role_id)
                provider = provider if "provider" in payload else role["provider"]
            else:
                role = self.roles.get(role_id, executable=True, connections=self.connections)
                if "provider" in payload and provider != role["provider"]:
                    raise ValueError("更换 CLI 时，请同时选择模型和思考强度")
                provider = role["provider"]
                payload = {**payload, "model": role["model"], "effort": role["effort"]}
        if not payload.get("agent"):
            if not provider:
                raise ValueError("请先选择 CLI")
            if not self.injected_services:
                self.connections.executable(provider)
        snapshot = self.catalog.refresh()
        agent_id, source_id = payload.get("agent"), payload.get("source")
        if agent_id:
            source = self.source_by_id(source_id, snapshot)
            if source is None or not isinstance(agent_id, str):
                raise ValueError("所选对话不存在")
            if self.group_of(source, snapshot) != group_id:
                return 409, {"error": "这条对话所属的项目已变化，请刷新页面后再发送"}
            with source.lock:
                runs = source.list_runs()
                agent = next((a for a in group_agents(runs) if a["id"] == agent_id), None)
                if agent is None:
                    raise ValueError("所选对话不存在")
                provider = agent["provider"]
                if "provider" in payload and payload["provider"] != provider:
                    raise ValueError("已有对话不能更换后端")
                recorded = source.recorded_selection(runs, agent_id)
            if agent.get("role_id"):
                if agent["role_execution_override"]:
                    role = self.enabled_role(agent["role_id"])
                    role_override = True
                    if recorded is None:
                        raise ValueError("没有记录可继续使用的模型，请新建对话")
                    payload = {**payload, "model": recorded[0], "effort": recorded[1]}
                else:
                    configured_role, _ = agent_roles.continuation(agent["role_id"], provider, text, self.roles, self.connections)
                    payload = {**payload, "model": configured_role["model"], "effort": configured_role["effort"]}
            selection = self.resolve_selection(payload, recorded, provider, cwd=agent.get("cwd"), role=bool(agent.get("role_id")))  # no source lock held
            cli_path = self.connections.executable(provider) if not self.injected_services else None
            with source.lock:
                result = source.start_turn(text, agent_id=agent_id, selection=selection, provider=provider, cli_path=cli_path,
                                           permission_mode=permission_selection(payload, provider), config_path=self.config_path,
                                           role=role, role_override=role_override)
            return self.confirmed(source, result)
        agent_providers.provider(provider)
        selection = self.resolve_selection(payload, None, provider, cwd=next((str(p.roots[0]) for p in snapshot.projects if p.group_id == group_id and p.roots), None), role=role is not None and not role_override)
        cli_path = self.connections.executable(provider) if not self.injected_services else None
        source, cwd = self.new_target(group_id, snapshot)
        daily = group_id == OTHER_GROUP
        left_run = False
        try:
            with source.lock:
                try:
                    result = source.start_turn(text, cwd=cwd, selection=selection, provider=provider, cli_path=cli_path,
                                               permission_mode=permission_selection(payload, provider), config_path=self.config_path,
                                               role=role, role_override=role_override)
                except ValueError:
                    left_run = daily and bool(source.list_runs())
                    raise
        finally:
            if left_run:  # outside source.lock: self.lock is never taken while a source lock is held
                self.retain_daily(source)
        if daily and result[0] in (201, 202):
            self.retain_daily(source)
        elif result[0] in (201, 202):
            self.retain(source)  # a conversation created in a formerly empty project must survive the project's removal
        return self.confirmed(source, result)

    def confirmed(self, source, result):
        """Hold the answer until Kiro has accepted the selected model/effort (the relay forwards the prompt only
        then). A refusal is a 409 so the browser keeps the draft; a slow start is reported as still starting."""
        if result[0] not in (201, 202) or not self.start_wait:
            return result
        outcome, state = source.await_start(result[1]["run_id"], self.start_wait)
        if outcome == "refused":
            return self.refusal(source, state, result[1]["agent_id"], result[1]["run_id"])
        # forwarded: Kiro accepted the selection and received the prompt (201). pending: still starting (202); the
        # browser keeps the original draft until polling shows prompt_forwarded, and nothing is ever resent.
        return (201 if outcome == "forwarded" else 202), dict(result[1], forwarded=outcome == "forwarded")

    def delete(self, payload):
        snapshot = self.catalog.refresh()
        source = self.source_by_id(payload.get("source"), snapshot)
        if source is None:
            return 404, {"error": "这条对话已不存在"}
        if self.group_of(source, snapshot) != payload.get("group"):
            return 409, {"error": "这条对话所属的项目已变化，请刷新页面后再删除"}
        with source.lock:
            return source.delete_conversation(payload.get("agent"))

    def models_view(self, refresh=False, provider=None, cwd=None):
        provider = provider or self.default_provider
        if provider is not None:
            agent_providers.provider(provider)
        if not self.injected_services:
            try:
                self.connections.executable(provider)
            except ValueError:
                return {"provider": provider, "status": "error", "models": [], "default": {"available": False},
                        "error": "请先在设置中连接 CLI"}
        agent_providers.provider(provider)
        if provider == "claude":
            catalog = self.claude_models.snapshot(force=refresh, cwd=cwd)
            return self.catalog_with_saved_default(provider, catalog)
        if not self.injected_services and not refresh:
            if self.models is not None and self.models.current is not None:
                return self.catalog_with_saved_default(provider, self.models.view(time.time()))  # cache only
            defaults = self.connections.defaults(provider)
            if self.connections.defaults_configured(provider) and defaults != {
                    "model": kiro_catalog.DEFAULT_MODEL, "effort": kiro_catalog.DEFAULT_EFFORT}:
                return {"provider": provider, "status": "idle", "models": [],
                        "default": {**defaults, "available": False}, "message": "请在设置中读取模型目录。"}
            return {"provider": "kiro", "status": "ok", "source": "pinned-default",
                    "message": "默认模型在任务启动时由 Kiro 门禁验证；其他模型请在设置中主动读取目录。",
                    "default": {"model": kiro_catalog.DEFAULT_MODEL, "effort": kiro_catalog.DEFAULT_EFFORT, "available": True},
                    "models": [{"id": kiro_catalog.DEFAULT_MODEL, "name": "Sonnet 5.5", "selectable": True,
                                "effort": {"known": True, "supported": True, "levels": [kiro_catalog.DEFAULT_EFFORT],
                                           "default": kiro_catalog.DEFAULT_EFFORT}}]}
        self.kiro_services()
        if not self.models:
            return {"status": "error", "models": [], "error": "测试预览没有模型目录", "default": {"available": False}}
        return self.catalog_with_saved_default(provider, self.models.snapshot(force=refresh))

    def catalog_with_saved_default(self, provider, catalog):
        if not self.connections.defaults_configured(provider):
            return catalog
        selection = self.connections.defaults(provider)
        available = any(m["id"] == selection["model"] and m.get("selectable") is not False
                        for m in catalog.get("models", []))
        return {**catalog, "default": {**selection, "available": available}}

    def usage_refresh(self, provider=None, cwd=None):
        provider = provider or self.default_provider
        if not self.injected_services:
            self.connections.executable(provider)
        agent_providers.provider(provider)
        if provider == "claude":
            snapshot = self.claude_usage.refresh(cwd=cwd)
            return (429 if snapshot.get("rate_limited") else 200), snapshot
        self.kiro_services()
        if not self.usage:
            return 404, {"error": "没有可用的额度来源"}
        snapshot = self.usage.refresh()
        return (429 if snapshot.get("rate_limited") else 200), snapshot

    def connection_action(self, payload):
        if self.fixture:
            return 403, {"error": "测试预览不允许连接 CLI"}
        action, provider = payload.get("action"), payload.get("provider")
        agent_providers.provider(provider)
        if action not in {"connect", "disconnect", "default", "test", "catalog", "pause", "start", "defaults"}:
            raise ValueError("未知连接操作")
        if not self.connection_slots.acquire(blocking=False):
            return 409, {"ok": False, "message": "另一个连接检查正在执行，请稍后重试"}
        try:
            if action == "connect":
                result = self.connections.connect(provider, payload.get("path", ""), allow_login=True)
            elif action == "disconnect":
                result = {"ok": True, "message": "已断开连接；正在运行的任务不受影响", **self.connections.disconnect(provider)}
            elif action == "default":
                result = {"ok": True, "message": "已设为默认 CLI", **self.connections.set_default(provider)}
            elif action == "defaults":
                self.connections.executable(provider)
                catalog = self.models_view(provider=provider)
                def validate_pair(pair):
                    model, effort = pair.get("model"), pair.get("effort")
                    entry = next((m for m in catalog.get("models", []) if m["id"] == model and m.get("selectable") is not False), None)
                    if catalog.get("status") != "ok" or not entry or catalog.get("source") == "pinned-default":
                        raise ValueError("请先读取当前 CLI 的模型目录并选择可用模型")
                    options = entry.get("effort") or {}
                    if not options.get("known") or (effort is None and options.get("supported") and provider == "kiro") or (
                            effort is not None and (not options.get("supported") or effort not in options.get("levels", []))):
                        raise ValueError("请选择该模型支持的思考强度")
                    if provider == "claude":
                        self.claude_models.validate(model, effort)
                    return model, effort

                model, effort = validate_pair(payload)
                overrides = payload.get("overrides", {})
                if not isinstance(overrides, dict):
                    raise ValueError("无效的角色模型配置")
                switch_roles = payload.get("switch_roles", [])
                if not isinstance(switch_roles, list) or any(not isinstance(role_id, str) or role_id not in overrides
                                                             for role_id in switch_roles):
                    raise ValueError("无效的角色 CLI 切换")
                for pair in overrides.values():
                    if not isinstance(pair, dict) or set(pair) != {"model", "effort"}:
                        raise ValueError("无效的角色模型配置")
                    if pair != {"model": None, "effort": None}:
                        validate_pair(pair)
                roles = None
                if overrides or payload.get("revision") == "missing":
                    roles = self.roles.mutate({"action": "models", "provider": provider,
                                               "revision": payload.get("revision"), "overrides": overrides,
                                               "switch_roles": switch_roles}, model_settings=True)
                result = {"ok": True, "message": "模型设置已保存，下次任务生效",
                          **self.connections.set_defaults(provider, model, effort)}
                if roles is not None:
                    result["roles_config"] = roles
            elif action in {"pause", "start"}:
                result = {"ok": True, "message": "已暂停，新任务将停止使用此 CLI；正在运行的任务继续执行" if action == "pause" else "已启动 CLI",
                          **self.connections.set_paused(provider, action == "pause")}
            elif action == "catalog":
                catalog = self.models_view(refresh=True, provider=provider)
                return (200 if catalog.get("status") != "error" else 400), {
                    "ok": catalog.get("status") != "error", "message": catalog.get("error") or "模型目录已读取", "catalog": catalog}
            else:
                return self.test_connection(provider)
            if result.get("ok"):
                self.default_provider = self.connections.snapshot()["default_provider"]
                if provider == "kiro" and action in {"connect", "disconnect", "pause", "start"}:
                    with self.provider_lock:
                        self.models = self.usage = None
                if action == "connect":
                    try:
                        catalog = self.models_view(refresh=True, provider=provider)
                        result["catalog"] = catalog
                        result["message"] = ("连接成功；模型目录读取失败，请点击读取模型目录重试" if catalog.get("error")
                                             or catalog.get("status") == "error" else "连接成功，模型目录已读取")
                    except (OSError, ValueError, subprocess.SubprocessError):
                        result["message"] = "连接成功；模型目录读取失败，请重新连接或读取目录重试"
            return (200 if result.get("ok") else 400), result
        finally:
            self.connection_slots.release()

    def test_connection(self, provider):
        binary = self.connections.executable(provider)
        # An explicit model call, isolated from project files and with tools disabled by --check.
        with tempfile.TemporaryDirectory(prefix="coder-agents-probe-") as directory:
            runner = SKILL / "scripts" / agent_providers.provider(provider)["runner"]
            command = [sys.executable, str(runner), "--cwd", directory, "--run-dir", str(Path(directory) / "run"),
                       "--cli-path", binary, "--check"]
            command += ["--timeout", "30"]
            defaults = self.connections.defaults(provider)
            command += ["--model", defaults["model"]]
            command += ["--effort", defaults["effort"]] if defaults["effort"] else ["--no-effort"]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                diagnostic = agent_diagnostics.diagnose("timeout")
                return 400, {"ok": False, "message": diagnostic["message"], "diagnostic": diagnostic}
            if code:
                directory = Path(directory) / "run"
                try:
                    state = json.loads(read_local(directory, "state.json"))
                except (ValueError, OSError):
                    state = {}
                diagnostic = safe_diagnostics(read_local(directory, "stderr.log"), state)
                return 400, {"ok": False, "message": diagnostic["message"] + "；" + diagnostic["next_step"], "diagnostic": diagnostic}
            return 200, {"ok": True, "message": "模型测试调用成功（与连接检查独立）"}

    def task_stop(self, payload):
        source = self.source_by_id(payload.get("source"), self.catalog.refresh())
        run_id = payload.get("run")
        if source is None or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("所选任务不存在")
        if self.group_of(source, self.catalog.snapshot()) != payload.get("group"):
            raise ValueError("任务所属项目已变化，请刷新页面")
        with source.lock:
            directory = source.root / run_id
            if directory.is_symlink() or directory.resolve().parent != source.root:
                raise ValueError("任务目录无效")
            result = agent_tasks.stop(directory)
        return 200, {"run": run_id, "status": result.get("status"),
                     "stopped": result.get("status") not in ACTIVE_STATUSES}

    def temp_action(self, payload):
        """Explicit cleanup of one task's data, for either CLI provider."""
        snapshot = self.catalog.refresh()
        source = self.source_by_id(payload.get("source"), snapshot)
        run_id, agent_id = payload.get("run"), payload.get("agent")
        if source is None or not isinstance(run_id, str) or not isinstance(agent_id, str) \
                or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            return 404, {"error": "找不到这轮任务"}
        if self.group_of(source, snapshot) != payload.get("group"):
            return 409, {"error": "这条对话所属的项目已变化，请刷新页面后再操作"}
        with source.lock:
            runs = [r for r in source.list_runs() if r["id"] == run_id and r["agent_id"] == agent_id]
        directory = source.root / run_id
        if not runs or directory.is_symlink() or directory.resolve().parent != source.root:
            return 404, {"error": "找不到这轮任务"}
        if not self.temp_lock.acquire(blocking=False):
            return 409, {"error": "另一项任务数据清理正在进行，请稍后再试", "code": "busy"}
        try:
            outcome = self.temp_clean_turn(source, runs[0], "pending", {"run": run_id, "round": 1})
            if outcome["outcome"] != "cleaned":
                return 409, {"error": outcome["reason"], "code": outcome["code"]}
            return 200, {"cleaned": True, **outcome}
        finally:
            self.temp_lock.release()

    # Unified task data: results, logs and recorded managed temporary directories.
    def temp_conversations(self, snapshot=None):
        """Enumerate every provider's task data, grouped by conversation (read-only)."""
        snapshot = snapshot or self.catalog.refresh()
        names = {p.group_id: p.name for p in snapshot.projects}
        names[OTHER_GROUP] = "其他对话"
        order = [p.group_id for p in snapshot.projects] + [OTHER_GROUP]
        now, found = time.time(), []
        for source in self.all_sources(snapshot):
            group = self.group_of(source, snapshot)
            with source.lock:
                runs = source.list_runs()
            for agent in group_agents(runs, now):
                turns, states = [], {}
                own = [r for r in runs if r["agent_id"] == agent["id"]]
                for index, state in enumerate(own, 1):
                    kind = "running" if state.get("status") in ACTIVE_STATUSES else "pending"
                    states[state["id"]] = (state, kind)
                    turns.append({"run": state["id"], "round": index, "state": kind, "provider": state.get("provider", "kiro"), "started_at": state.get("started_at")})
                counts = {k: sum(t["state"] == k for t in turns) for k in ("pending", "running", "needs_review", "unknown", "cleaned")}
                busy = any(r.get("status") in ACTIVE_STATUSES for r in own)
                found.append((group, names.get(group, group), order, source,
                              {"key": source.id + ":" + agent["id"], "group": group, "source": source.id, "agent": agent["id"],
                               "title": agent["title"], "status": agent["status"], "running": busy, "activity": agent["activity"],
                               "counts": counts, "turns": turns}, states))
        return found

    def temp_overview(self):
        snapshot = self.catalog.refresh()
        found = self.temp_conversations(snapshot)
        groups, totals = {}, {"conversations": 0, "pending": 0, "running": 0, "needs_review": 0, "unknown": 0, "cleaned": 0}
        for group, name, order, source, conversation, _ in found:
            entry = groups.setdefault(group, {"id": group, "name": name, "conversations": [], "rank": order.index(group) if group in order else len(order)})
            entry["conversations"].append(conversation)
            totals["conversations"] += 1
            for key, value in conversation["counts"].items():
                totals[key] += value
        ordered = sorted(groups.values(), key=lambda g: g["rank"])
        for entry in ordered:
            entry.pop("rank")
            entry["conversations"].sort(key=lambda c: (c["activity"], c["key"]), reverse=True)
        return {"groups": ordered, "totals": totals, "generated_at": datetime.now(timezone.utc).isoformat()}

    def temp_bulk(self, payload):
        """Delete results, logs and managed temp for selected/all non-running tasks."""
        scope = payload.get("scope")
        wanted = None
        if scope == "selected":
            items = payload.get("conversations")
            if not isinstance(items, list) or not items or len(items) > 500:
                return 400, {"error": "没有选择对话", "code": "empty_selection"}
            wanted = {}
            for item in items:
                if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and len(item[k]) <= 200 for k in ("group", "source", "agent")):
                    return 400, {"error": "所选对话格式无效", "code": "bad_selection"}
                wanted[(item["source"], item["agent"])] = item["group"]
        elif scope != "all":
            return 400, {"error": "清理范围无效", "code": "bad_scope"}
        if not self.temp_lock.acquire(blocking=False):
            return 409, {"error": "另一项任务数据清理正在进行，请稍后再试", "code": "busy"}
        try:
            found = {(s.id, c["agent"]): (g, s, c, st) for g, _, _, s, c, st in self.temp_conversations()}
            result = {"scope": scope, "cleaned": 0, "skipped": 0, "errors": 0, "already_cleaned": 0, "conversations": []}
            targets = []
            for key in (found if wanted is None else wanted):
                if key in found and (wanted is None or wanted[key] == found[key][0]):
                    targets.append(found[key])
                else:
                    result["skipped"] += 1
                    result["conversations"].append({"source": key[0], "agent": key[1], "title": "", "items": [
                        {"run": "", "round": 0, "outcome": "skipped", "code": "conversation_changed",
                         "reason": "这条对话已不存在或所属项目已变化，已跳过"}]})
            for group, source, conversation, states in targets:
                entry = {"group": group, "source": source.id, "agent": conversation["agent"], "title": conversation["title"], "items": []}
                for turn in conversation["turns"]:
                    state, kind = states[turn["run"]]
                    outcome = self.temp_clean_turn(source, state, kind, turn)
                    if outcome is None:
                        result["already_cleaned"] += 1
                        continue
                    result[{"cleaned": "cleaned", "skipped": "skipped", "error": "errors"}[outcome["outcome"]]] += 1
                    entry["items"].append(outcome)
                result["conversations"].append(entry)
            result["partial"] = result["cleaned"] > 0 and (result["skipped"] > 0 or result["errors"] > 0)
            return 200, result
        finally:
            self.temp_lock.release()

    def temp_clean_turn(self, source, state, kind, turn):
        """Delete a non-running task's recorded data. No acceptance or /proc scan required."""
        item = {"run": turn["run"], "round": turn["round"]}
        try:
            with source.lock:
                run_id = turn["run"]
                directory = source.root / run_id
                if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or directory.is_symlink() or directory.resolve().parent != source.root:
                    raise ValueError("运行目录位置异常，未删除")
                if not directory.exists():
                    return {**item, "outcome": "skipped", "code": "missing", "reason": "任务已清理"}
                # Re-read immediately before deleting; an earlier overview is not authoritative.
                current = json.loads((directory / "state.json").read_text())
                if current.get("status") in ACTIVE_STATUSES:
                    return {**item, "outcome": "skipped", "code": "active", "reason": "任务运行中，已跳过"}
                record = current.get("temp")
                if isinstance(record, dict) and isinstance(record.get("path"), str):
                    path = Path(record["path"])
                    expected = source.cwd / "work/tmp" / run_id
                    if path != expected or path.resolve() != expected or path == directory or path in directory.parents or directory in path.parents:
                        raise ValueError("临时目录位置异常，未删除任务数据")
                    if path.exists():
                        kiro_temp.clean_tree(path, os.lstat(path))
                shutil.rmtree(directory)
                (source.root / (".launch-" + run_id + ".log")).unlink(missing_ok=True)
                source.legacy_sessions.pop(run_id, None)
                source.summaries.pop(run_id, None)
                agent_id = current.get("agent_id") or run_id
                if not any(r["agent_id"] == agent_id for r in source.list_runs()):
                    metadata = source.root / "conversations.json"
                    if metadata.is_file():
                        saved = json.loads(metadata.read_text())
                        if isinstance(saved, dict) and agent_id in saved:
                            saved.pop(agent_id)
                            write_json(metadata, saved)
            return {**item, "outcome": "cleaned", "code": "cleaned", "reason": ""}
        except (OSError, ValueError, kiro_temp.TempError) as error:
            return {**item, "outcome": "error", "code": "cleanup_error", "reason": "清理失败：" + str(error).split(" / ")[0]}

    def active_tasks(self):
        return sum(r.get("status") in ACTIVE_STATUSES for s in self.all_sources(self.catalog.snapshot()) for r in s.list_runs())

    def cleanup(self, now=None):
        with self.cleanup_lock:
            # The user's retention preference also gates startup and manual cleanup requests.
            # An unreadable or malformed setting must never authorize deletion.
            try:
                retention_config = json.loads(self.config_path.read_text())
                enabled = retention_config.get("auto_cleanup_enabled", True) is True
            except (OSError, ValueError, AttributeError):
                enabled = False
            if not enabled:
                return {"enabled": False, "retention_days": None, "deleted_conversations": 0,
                        "deleted_runs": 0, "skipped_active": 0, "skipped_unknown_dates": 0, "errors": []}
            now = time.time() if now is None else now
            result = {"checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                      "retention_days": RETENTION_DAYS, "deleted_conversations": 0, "deleted_runs": 0,
                      "skipped_active": 0, "skipped_unknown_dates": 0, "errors": []}
            for source in self.all_sources(self.catalog.refresh()):
                try:
                    with source.lock:
                        totals = source.cleanup(now)
                    for key, value in totals.items():
                        result[key] += value
                except (OSError, ValueError) as error:
                    result["errors"].append({"source": source.id, "error": str(error)})
            write_json(self.cleanup_path, result)
            self.last_cleanup = result
            return result


def make_handler(panel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_body(self, body, mime="application/json; charset=utf-8", status=200, headers=()):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in headers:
                self.send_header(name, value)
            if mime.startswith("text/html"):
                self.send_header("Content-Security-Policy", PAGE_CSP)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self):
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in {f"127.0.0.1:{panel.port}", f"localhost:{panel.port}"} or (origin and origin != "http://" + host):
                self.send_error(403)
                return False
            panel.last_seen = time.monotonic()
            return True

        def token_remainder(self, path):
            """The part of a CLI/legacy path after the private token, or None. Compared in constant time."""
            if len(path) < len(panel.prefix) or not hmac.compare_digest(path[:len(panel.prefix)].encode(), panel.prefix.encode()):
                return None
            return path[len(panel.prefix):]

        def authenticated(self):
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
            except CookieError:
                return False
            morsel = jar.get(panel.cookie_name)
            return bool(morsel) and hmac.compare_digest(morsel.value.encode(), panel.cookie_value.encode())

        def browser_post_ok(self):
            """Cookie-authenticated, same-origin JSON only. Origin (checked above) or Fetch Metadata must prove it."""
            if not self.authenticated():
                self.send_body({"error": "登录状态已失效，请重新打开固定入口 http://127.0.0.1:%d/" % panel.port}, status=401)
                return False
            same_site = self.headers.get("Origin") == "http://" + self.headers.get("Host", "") or self.headers.get("Sec-Fetch-Site") == "same-origin"
            if not same_site or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                self.send_error(403)
                return False
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self.send_error(415)
                return False
            return True

        def payload(self, limit=131072):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError("消息不能为空，且需要小于 128 KB" if limit == 131072 else "文件请求体过大或为空")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Invalid message")
            return payload

        def page(self, initial_source=None):
            html = (SKILL / "assets/viewer.html").read_bytes()
            if initial_source:  # a legacy bookmark: tell the page which source to select, then it drops the path
                html = html.replace(b"<head>", b'<head><meta name="kiro-initial-source" content="' + initial_source.encode() + b'">', 1)
            cookie = f"{panel.cookie_name}={panel.cookie_value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
            self.send_body(html, "text/html; charset=utf-8", headers=[("Set-Cookie", cookie)])

        def do_POST(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            try:
                remainder = self.token_remainder(path)
                if remainder is not None:  # command-line and legacy routes, authenticated by the private token
                    if remainder == "stop" or re.fullmatch(r"p/[0-9a-f]{16}/stop", remainder):
                        panel.stopping.set()
                        self.send_body({"stopped": True})
                    elif remainder == "cleanup":
                        self.payload()
                        self.send_body(panel.cleanup())
                    elif remainder == "register":
                        source = panel.register(self.payload())
                        self.send_body({"url": panel.address + "/", "project_id": source.id})
                    else:
                        self.send_error(404)
                elif path in {"/api/workspace", "/api/workspace/git"}:
                    if not self.browser_post_ok():
                        return
                    is_git = path.endswith("/git")
                    status, result = panel.workspace("git" if is_git else "write",
                                                     self.payload() if is_git else self.payload(6 * kiro_review.MAX_FILE_BYTES + 32768))
                    self.send_body(result, status=status)
                elif path in {"/api/send", "/api/delete", "/api/stop", "/api/tasks/stop", "/api/usage/refresh", "/api/temp", "/api/temp/bulk", "/api/connections", "/api/roles", "/api/github/auth"}:
                    if not self.browser_post_ok():
                        return
                    if path == "/api/stop":
                        panel.stopping.set()
                        self.send_body({"stopped": True})
                        return
                    if path == "/api/usage/refresh":
                        payload = self.payload()
                        cwd = panel.context_cwd(payload.get("group"), payload.get("source"))
                        status, result = panel.usage_refresh(payload.get("provider"), cwd=cwd)
                        self.send_body(result, status=status)
                        return
                    payload = self.payload()
                    if path == "/api/github/auth":
                        action = payload.get("action")
                        if panel.fixture and action == "login":
                            self.send_body({"error": "预览模式无法登录 GitHub"}, status=409)
                            return
                        status, result = panel.github.action(action)
                        self.send_body(result, status=status)
                        return
                    if path == "/api/roles":
                        self.send_body(panel.roles.mutate(payload))
                        return
                    if path == "/api/connections":
                        status, result = panel.connection_action(payload)
                        self.send_body(result, status=status)
                        return
                    status, result = {"/api/send": panel.send, "/api/delete": panel.delete, "/api/temp": panel.temp_action,
                                     "/api/tasks/stop": panel.task_stop, "/api/temp/bulk": panel.temp_bulk}[path](payload)
                    self.send_body(result, status=status)
                else:
                    self.send_error(404)
            except (OSError, ValueError, KeyError) as error:
                self.send_body({"error": str(error)}, status=400)

        def do_GET(self):
            if not self.allowed():
                return
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/":
                # Hand out the browser cookie only to a top-level navigation, never to a cross-site request.
                if self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}:
                    self.send_error(403)
                    return
                self.page()
            elif path.startswith("/vendor/") and path[1:] in VENDOR_ASSETS:
                name, mime = VENDOR_ASSETS[path[1:]]
                self.send_body((SKILL / name).read_bytes(), mime)
            elif path == "/api/health":
                if not self.authenticated():
                    self.send_body({"error": "需要面板登录状态"}, status=401)
                    return
                self.send_body({"service": "coder-agents", "ready": True, "version": VERSION})
            elif path == "/api":
                if not self.authenticated():
                    self.send_body({"error": "登录状态已失效，请重新打开固定入口 http://127.0.0.1:%d/" % panel.port}, status=401)
                    return
                query = parse_qs(parts.query)
                first = lambda name: (query.get(name) or [""])[0][:200]
                self.send_body(panel.view(first("group"), first("source"), first("agent"), first("provider")))
            elif path in {"/api/workspace/tree", "/api/workspace/file", "/api/review", "/api/review/file", "/api/review/diff"}:
                if not self.authenticated():
                    self.send_body({"error": "登录状态已失效，请重新打开固定入口 http://127.0.0.1:%d/" % panel.port}, status=401)
                    return
                query = {key: values[0][:4096] for key, values in parse_qs(parts.query).items() if values}
                if path.startswith("/api/workspace/"):
                    status, body = panel.workspace(path.rsplit("/", 1)[1], query)
                else:
                    kind = {"/api/review": "status", "/api/review/file": "file", "/api/review/diff": "diff"}[path]
                    status, body = panel.review(kind, query)
                self.send_body(body, status=status)
            elif path == "/api/temp/overview":
                if not self.authenticated():
                    self.send_body({"error": "登录状态已失效，请重新打开固定入口 http://127.0.0.1:%d/" % panel.port}, status=401)
                    return
                self.send_body(panel.temp_overview())
            elif path == "/api/roles":
                if not self.authenticated():
                    self.send_body({"error": "需要面板登录状态"}, status=401)
                    return
                try:
                    self.send_body(panel.roles.snapshot())
                except (OSError, ValueError) as error:
                    self.send_body({"error": str(error)}, status=400)
            elif path in {"/api/connections", "/api/github/auth"}:
                if not self.authenticated():
                    self.send_body({"error": "需要面板登录状态"}, status=401)
                    return
                self.send_body(panel.github.snapshot() if path == "/api/github/auth" else panel.connections.snapshot())
            elif path == "/api/models":
                if not self.authenticated():
                    self.send_body({"error": "登录状态已失效，请重新打开固定入口 http://127.0.0.1:%d/" % panel.port}, status=401)
                    return
                query = parse_qs(parts.query)
                try:
                    cwd = None
                    if query.get("group"):
                        snapshot = panel.catalog.refresh()
                        if query.get("source"):
                            source = panel.source_by_id(query["source"][0], snapshot)
                            if source is None or panel.group_of(source, snapshot) != query["group"][0]:
                                raise ValueError("项目目录不可用")
                            cwd = str(source.cwd)
                        else:
                            project = next((p for p in snapshot.projects if p.group_id == query["group"][0]), None)
                            cwd = str(project.roots[0]) if project and project.roots else None
                    self.send_body(panel.models_view(refresh=query.get("refresh") == ["1"],
                                                     provider=(query.get("provider") or [None])[0], cwd=cwd))
                except ValueError as error:
                    self.send_body({"error": str(error)}, status=400)
            else:
                # A tokenized bookmark from an earlier version: select its source, then continue at "/".
                remainder = self.token_remainder(path)
                legacy = re.fullmatch(r"p/([0-9a-f]{16})/", remainder) if remainder is not None else None
                if remainder == "":
                    self.page()
                elif legacy and panel.source_by_id(legacy.group(1)):
                    self.page(panel.source_by_id(legacy.group(1)).id)
                else:
                    self.send_error(404)
    return Handler


def activated_server(panel, handler):
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise ValueError("Missing systemd activation socket")
    listener = socket.socket(fileno=3)
    if listener.family != socket.AF_INET or listener.getsockname() != ("127.0.0.1", panel.port):
        listener.detach()
        raise ValueError("Unexpected activation socket address")
    listener.set_inheritable(False)
    server = ThreadingHTTPServer(("127.0.0.1", panel.port), handler, bind_and_activate=False)
    server.socket.close()
    server.socket = listener
    server.server_address = listener.getsockname()
    server.server_name = "localhost"
    server.server_port = panel.port
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--cleanup", action="store_true", help="Delete conversations inactive for more than seven days")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--systemd-socket", action="store_true")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    os.environ["CODER_AGENTS_CONFIG"] = str(config_path)
    if not config_path.exists():
        parser.error("请先运行 python3 -m coder_agents service install（见 README），安装固定入口和按需启动服务")
    config = json.loads(config_path.read_text())
    address = f'http://127.0.0.1:{int(config["port"])}/{config["token"]}/'
    if not args.serve:
        if args.cleanup:
            request = Request(address + "cleanup", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        elif args.stop:
            request = Request(address + "stop", method="POST")
        else:
            if not args.cwd:
                parser.error("--cwd is required when registering a project")
            cwd = args.cwd.expanduser().resolve(strict=True)
            root = args.root.expanduser().resolve() if args.root else cwd / "work/kiro-runs"
            request = Request(address + "register", data=json.dumps({"cwd": str(cwd), "root": str(root)}).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
        with LOCAL_HTTP.open(request, timeout=290 if args.cleanup else 10) as response:
            result = json.load(response)
        print(json.dumps(result, ensure_ascii=False) if args.cleanup else
              "Python panel stopped; click the fixed entry to reopen" if args.stop else result["url"], flush=True)
        return
    panel = Panel(config, config_path)
    panel.cleanup()
    handler = make_handler(panel)
    server = activated_server(panel, handler) if args.systemd_socket else ThreadingHTTPServer(("127.0.0.1", panel.port), handler)
    server.timeout = .5
    signal.signal(signal.SIGTERM, lambda *_: panel.stopping.set())
    print(panel.address + "/", flush=True)
    deadline = time.monotonic() + 8 * 60 * 60
    try:
        while not panel.stopping.is_set() and time.monotonic() < deadline:
            server.handle_request()
            if (time.monotonic() - panel.last_seen > config.get("idle_timeout", 120)
                    and panel.github.snapshot()["state"] not in {"starting", "waiting", "checking", "cancelling"}
                    and not panel.active_tasks()):
                break
    except KeyboardInterrupt:
        pass
    finally:
        panel.github.close()
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(f"Kiro panel failed: {error}", file=sys.stderr)
        raise SystemExit(1)
