#!/usr/bin/env python3
"""Execute and resume Claude Code print sessions with private, reviewable logs."""
import argparse
import codecs
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import kiro_temp
import agent_tasks
import agent_roles
import time
import uuid

from agent_providers import ClaudeModels, ConfigError
from run_kiro import temp_settings
from cli_connections import runner_executable


def now():
    return datetime.now(timezone.utc).isoformat()


class Record:
    def __init__(self, directory, state, prompt):
        self.directory, self.state = directory, state
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.streams = {}
        for name in ("task.txt", "events.jsonl", "raw-events.jsonl", "stderr.log", "result.txt"):
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.streams[name] = os.fdopen(fd, "w", encoding="utf-8")
        self.write("task.txt", prompt)
        self.update()

    def write(self, name, text):
        self.streams[name].write(text)
        self.streams[name].flush()

    def update(self, **changes):
        self.state.update(changes)
        path = self.directory / ".state.tmp"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False)
        path.replace(self.directory / "state.json")

    def event(self, event):
        text = json.dumps(event, ensure_ascii=False) + "\n"
        self.write("events.jsonl", text)
        sys.stdout.write(text)
        sys.stdout.flush()

    def close(self):
        for stream in self.streams.values():
            stream.close()


def normalize(event):
    """Complete assistant messages, not partial deltas: no double rendering of final text."""
    kind = event.get("type")
    if kind in {"assistant", "user"}:
        message = event.get("message") or {}
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "text":
                yield {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": block.get("text", "")}}
            elif kind == "assistant" and block.get("type") == "tool_use":
                name = block.get("name", "Tool")
                yield {"sessionUpdate": "tool_call", "toolCallId": block.get("id"), "title": name,
                       "kind": {"Read": "read", "Edit": "edit", "Write": "edit", "Bash": "execute",
                                "Grep": "search", "Glob": "search"}.get(name, "other"),
                       "status": "in_progress", "rawInput": block.get("input", {})}
            elif kind == "user" and block.get("type") == "tool_result":
                yield {"sessionUpdate": "tool_call_update", "toolCallId": block.get("tool_use_id"),
                       "status": "failed" if block.get("is_error") else "completed", "rawOutput": block.get("content", "")}
    elif kind == "result":
        yield {"type": "runFinished", "data": {"finalText": event.get("result", ""),
               "status": "failed" if event.get("is_error") or event.get("subtype") != "success" else "success",
               "stopReason": event.get("subtype"), "usage": event.get("usage", {})}}


def stop_process(process):
    if process is None:
        return
    # The child owns a separate session, so descendants receive the same cancellation.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # The leader may exit on SIGTERM while a tool descendant ignores it.
    # A completed leader does not prove the process group is empty.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def execute(args, record, binary, prompt):
    command = [binary, "-p", "--verbose", "--output-format", "stream-json", "--model", args.model,
               "--permission-mode", args.permission_mode]
    if args.effort:
        command += ["--effort", args.effort]
    if args.resume_id:
        command += ["--resume", args.resume_id]
    if args.allowed_tools:
        command += ["--allowedTools", ",".join(args.allowed_tools)]
    if args.check:
        command += ["--tools", ""]
    process, result, malformed = None, None, False
    deadline = time.monotonic() + args.timeout
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as task:
            task.write(prompt)
            task.seek(0)
            # Claude uses its own CLI settings and the inherited environment.
            environment = os.environ.copy()
            environment = kiro_temp.temp_environment(environment, record.state["temp"], bool(temp_settings().get("pytest_basetemp")))
            process = subprocess.Popen(command, cwd=args.cwd, env=environment, stdin=task,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        record.update(status="running", child_pid=process.pid, child_identity=agent_tasks.process_identity(process.pid), prompt_submitted=True)
        decoders = {"out": codecs.getincrementaldecoder("utf-8")("replace"),
                    "err": codecs.getincrementaldecoder("utf-8")("replace")}
        buffer = ""

        def consume(line):
            nonlocal result, malformed
            if not line.strip():
                return
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
            except ValueError:
                malformed = True
                return
            session = event.get("session_id")
            if isinstance(session, str) and session:
                record.update(session_id=session, prompt_forwarded=True)
            if event.get("type") == "system" and event.get("subtype") == "init":
                record.update(actual_model=event.get("model"))
            if event.get("type") == "result":
                result = event
                record.update(usage=event.get("usage"), cost_usd=event.get("total_cost_usd"),
                              permission_denials=event.get("permission_denials", []))
                record.write("result.txt", str(event.get("result") or ""))
            for normalized in normalize(event):
                record.event(normalized)

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Claude task exceeded --timeout; inspect logs before retrying")
                for key, _ in selector.select(timeout=min(0.2, max(0, deadline - time.monotonic()))):
                    name = key.data
                    block = os.read(key.fileobj.fileno(), 65536)
                    text = decoders[name].decode(block, final=not block)
                    record.write("raw-events.jsonl" if name == "out" else "stderr.log", text)
                    if name == "out":
                        buffer += text
                        lines = buffer.split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            consume(line)
                        if not block and buffer:
                            consume(buffer)
                            buffer = ""
                    if not block:
                        selector.unregister(key.fileobj)
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        # A tool can be denied while Claude still completes the turn. Keep the denials
        # in the run state so the panel can show them separately from the exit status.
        if code or malformed or not result or result.get("is_error") or result.get("subtype") != "success":
            record.write("stderr.log", "Claude did not complete successfully (exit/result/stream). Inspect raw-events.jsonl; do not retry blindly.\n")
            if result:
                from agent_diagnostics import redact
                record.write('stderr.log', redact(str(result.get('result') or '') + '\n' +
                             json.dumps(result.get('permission_denials') or [], ensure_ascii=False)) + '\n')
            return code if code > 0 else 1
        return 0
    finally:
        stop_process(process)
        if process:
            process.stdout.close()
            process.stderr.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--cli-path", help="Explicit executable path supplied by saved panel connection")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--agent-id")
    parser.add_argument("--title")
    parser.add_argument("--resume-id")
    parser.add_argument("--model", default="sonnet", help="Claude alias or full model ID; validated by Claude")
    effort = parser.add_mutually_exclusive_group()
    effort.add_argument("--effort", choices=("low", "medium", "high", "max"), default="medium")
    effort.add_argument("--no-effort", dest="effort", action="store_const", const=None)
    parser.add_argument("--permission-mode", choices=("dontAsk", "acceptEdits", "plan"), default="dontAsk")
    parser.add_argument("--allowed-tools", action="append", default=[], help="Explicit Claude tool allow rule; repeatable")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--check", action="store_true", help="Send a tool-free smoke request (uses model quota)")
    args = parser.parse_args(argv)
    args.cwd = args.cwd.expanduser().resolve(strict=True)
    if not args.cwd.is_dir() or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--cwd must be a directory and --timeout must be positive and finite")
    if args.agent_id and not re.fullmatch(r"[A-Za-z0-9_-]+", args.agent_id):
        parser.error("invalid --agent-id")
    if args.resume_id:
        try:
            uuid.UUID(args.resume_id)
        except ValueError:
            parser.error("--resume-id must be a Claude session UUID")
    try:
        ClaudeModels().validate(args.model, args.effort)
    except ConfigError as error:
        parser.error(str(error))
    binary = runner_executable("claude", args.cli_path)
    prompt = "Reply only CLAUDE_READY. Do not use tools." if args.check else sys.stdin.read()
    if not prompt.strip():
        parser.error("Provide a non-empty task on stdin")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    directory = args.run_dir.expanduser().absolute() if args.run_dir else args.cwd / "work/kiro-runs" / run_id
    state = {"schema_version": 1, "id": directory.name, "provider": "claude", "cwd": str(args.cwd),
             "agent_id": args.agent_id or directory.name, "title": args.title or prompt.strip().splitlines()[0][:80],
             "model": args.model, "effort": args.effort, "status": "checking", "started_at": now(),
             "runner_pid": os.getpid(), "runner_identity": agent_tasks.process_identity(os.getpid()),
             **agent_roles.identity(), "permission_mode": args.permission_mode, "prompt_forwarded": False}
    if args.resume_id:
        state.update(resume_id=args.resume_id, session_id=args.resume_id)
    record = Record(directory, state, prompt)
    agent_tasks.register(args.cwd, directory.parent)
    previous = {}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, interrupted)
    code = 1
    try:
        temp = kiro_temp.create_run_temp(args.cwd, directory.name, directory, record.state["agent_id"],
                                         kiro_temp.min_free_mb(configured=temp_settings().get("min_free_mb")))
        record.update(temp=kiro_temp.public_temp(temp))
        code = execute(args, record, binary, prompt)
    except KeyboardInterrupt:
        code = 130
        record.write("stderr.log", "Claude task cancelled; inspect the working tree before retrying.\n")
    except (OSError, ValueError, subprocess.SubprocessError, kiro_temp.TempError) as error:
        record.write("stderr.log", str(error) + "\n")
    finally:
        record.update(status="completed" if code == 0 else "cancelled" if code == 130 else "failed",
                      exit_code=code, finished_at=now())
        record.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print("Claude runner failed: " + str(error), file=sys.stderr)
        sys.exit(1)
