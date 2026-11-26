#!/usr/bin/env python3
"""Run Kiro with the user's fixed model and effort; refuse known fallback paths."""
import argparse
import math
import agent_tasks
import agent_roles
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import selectors
import codecs
from datetime import datetime, timezone
import tempfile
import time
import uuid
import signal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kiro_catalog
import kiro_acp
import kiro_temp

# Defaults used when the caller passes no explicit selection (Codex delegation always uses these).
MODEL = kiro_catalog.DEFAULT_MODEL
EFFORT = kiro_catalog.DEFAULT_EFFORT
EXIT_TEMP = 3
TOOLS = ",".join([
    "read_file", "list_directory", "file_search", "grep_search",
    "fs_write", "fs_append", "str_replace", "delete_file",
    "execute_bash", "run_command",
])
SELECTION_FAILURE = re.compile(
    r"failed to set (?:model|effort)|INVALID_MODEL_ID|InvalidModelError|"
    r"offers no effort levels|model.{0,150}(?:not available|not supported)|"
    r"(?:falling back|fallback).{0,80}model",
    re.IGNORECASE,
)


RELAY = Path(__file__).resolve().with_name("kiro_acp_relay.py")
RELAY_TIMEOUT = 60


def gate_message(reason, model=MODEL, effort=EFFORT):
    shown = f"effort '{effort}'" if effort else "no effort setting"
    return {
        "catalog_timeout": "Kiro's model catalog did not finish loading in time. The task prompt was NOT sent and no work "
                           "started. This is a startup/network/login problem, not proof the model is missing; retry later.",
        "model_unavailable": f"The complete model catalog does not contain {model}. No model was substituted and the "
                             "task prompt was NOT sent.",
        "effort_unsupported": f"The complete catalog says {model} does not offer {shown}. The effort was not "
                              "changed and the task prompt was NOT sent.",
        "effort_required": f"The complete catalog says {model} needs an effort level, but none was selected. Nothing was "
                           "changed and the task prompt was NOT sent.",
        "set_failed": "Kiro rejected the model or effort setting. The task prompt was NOT sent.",
        "confirm_timeout": "Kiro did not confirm the model and effort in time. The task prompt was NOT sent.",
        "server_exited": "The Kiro agent server exited during startup. The task prompt was NOT sent.",
        "no_node": "The relay could not start the Kiro agent server's Node binary. The task prompt was NOT sent.",
    }.get(reason, f"Kiro startup was stopped ({reason}). The task prompt was NOT sent.")


def locate_node(environment):
    """The real Node binary Kiro CLI would use for its agent server (never the relay itself)."""
    current = environment.get("KIRO_KAS_NODE_PATH")
    if current and RELAY.exists() and Path(current).resolve() == RELAY.resolve():
        current = environment.get("KIRO_RELAY_REAL_NODE")  # nested run: the outer runner already swapped it
    if not current:
        current = str(kiro_acp.data_directory(environment) / "node")
    if not os.access(current, os.X_OK):
        raise OSError(f"Cannot find Kiro's Node binary ({current}); set KIRO_KAS_NODE_PATH to it. "
                      "The task prompt was NOT sent.")
    return current


def relay_environment(environment, status_file, model=MODEL, effort=EFFORT, catalog_file=None):
    if not RELAY.is_file() or not os.access(RELAY, os.X_OK):
        raise OSError(f"Startup relay {RELAY} is missing or not executable; reinstall the kiro-coding skill. "
                      "The task prompt was NOT sent.")
    result = dict(environment)
    result.update(KIRO_RELAY_REAL_NODE=locate_node(environment), KIRO_KAS_NODE_PATH=str(RELAY),
                  KIRO_RELAY_MODEL=model, KIRO_RELAY_EFFORT=effort or "", KIRO_RELAY_STATUS=str(status_file))
    if catalog_file:
        result["KIRO_RELAY_CATALOG"] = str(catalog_file)
    result.setdefault("KIRO_RELAY_TIMEOUT", str(RELAY_TIMEOUT))
    return result


def read_status(path, settle=0):
    """Relay status; with `settle`, wait up to that many seconds for the relay's final write."""
    start = time.monotonic()
    while True:
        try:
            data = json.loads(Path(path).read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        # A relay that never wrote a status (the CLI died before starting it) is given only a moment.
        limit = settle if data else min(settle, 1)
        if data.get("final") or time.monotonic() - start >= limit:
            return data
        time.sleep(0.05)


def settings_path(environment=None):
    """User settings file: $CODER_AGENTS_SETTINGS, else $XDG_CONFIG_HOME (or ~/.config)/coder-agents/settings.json.

    Never read from the source checkout, so personal proxy values cannot end up in the repository."""
    environment = os.environ if environment is None else environment
    explicit = environment.get("CODER_AGENTS_SETTINGS")
    if explicit:
        return Path(explicit).expanduser()
    base = environment.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "coder-agents" / "settings.json"


def kiro_environment():
    environment = dict(os.environ)
    settings = settings_path(environment)
    if settings.exists():
        defaults = json.loads(settings.read_text()).get("proxy_defaults", {})
        for key, default in defaults.items():
            if key not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"} or not isinstance(default, str):
                raise ValueError("Invalid proxy default in settings file")
            lower = key.lower()
            value = environment[lower] if lower in environment else environment.get(key, default)
            environment[key] = environment[lower] = value
    return environment


def temp_settings():
    """Optional `temp` object of settings.json: {"min_free_mb": int, "pytest_basetemp": bool}."""
    try:
        data = json.loads(settings_path().read_text()).get("temp", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


class RunRecord:
    def __init__(self, cwd, prompt, directory, agent_id=None, resume_id=None, title=None, selection=None):
        self.directory = directory
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        selection = selection or {"model": MODEL, "effort": EFFORT, "source": "default"}
        self.selection = selection
        self.state = {"id": directory.name, "cwd": str(cwd), "model": selection["model"],
                      "agent_id": agent_id or directory.name,
                      "title": title or prompt.strip().splitlines()[0][:80],
                      "effort": selection["effort"], "selection": selection["source"],
                      "status": "checking", "started_at": datetime.now(timezone.utc).isoformat(),
                      **kiro_temp.my_identity(), "runner_identity": agent_tasks.process_identity(os.getpid()),
                      **agent_roles.identity()}
        if resume_id:
            self.state.update(resume_id=resume_id, session_id=resume_id)
        self.update()
        self.task = directory / "task.txt"
        self.task.write_text(prompt)
        self.task.chmod(0o600)
        self.logs = {}
        for name in ["events.jsonl", "stderr.log"]:
            path = directory / name
            path.touch(mode=0o600)
            self.logs[name] = path.open("a", encoding="utf-8", buffering=1)
        print(f"KIRO_RUN_DIR={directory}", file=sys.stderr, flush=True)

    def update(self, **changes):
        self.state.update(changes)
        path = self.directory / "state.tmp"
        path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        path.replace(self.directory / "state.json")

    def diagnostic(self, text):
        self.logs["stderr.log"].write(text)
        self.logs["stderr.log"].flush()
        sys.stderr.write(text)
        sys.stderr.flush()

    def finish(self, result):
        self.update(status="completed" if result == 0 else "cancelled" if result == 130 else "failed",
                    exit_code=result, finished_at=datetime.now(timezone.utc).isoformat())
        for stream in self.logs.values():
            stream.close()


def sync_startup(record, status_file, seen):
    """Mirror the relay's startup progress into state.json so the panel can learn whether Kiro accepted the
    selection (prompt_forwarded) without waiting for the whole task."""
    try:
        status = json.loads(Path(status_file).read_text())
    except (OSError, ValueError):
        return
    if not isinstance(status, dict):
        return
    view = (status.get("state"), bool(status.get("prompt_forwarded")))
    if view != seen[0]:
        seen[0] = view
        record.update(startup=status.get("state") or "unknown", prompt_forwarded=view[1])


def execute(binary, cwd, prompt, environment, check, record, timeout=3600):
    deadline = time.monotonic() + timeout
    selection = record.selection
    model, effort = selection["model"], selection["effort"]
    settings = temp_settings()
    try:
        temp = kiro_temp.create_run_temp(cwd, record.state["id"], record.directory, record.state.get("agent_id"),
                                         kiro_temp.min_free_mb(environment, settings.get("min_free_mb")))
    except kiro_temp.TempError as error:
        record.update(startup="temp_unavailable", prompt_forwarded=False)
        record.diagnostic(f"{error}\nThe task prompt was NOT sent; fix the problem and resubmit.\n")
        return EXIT_TEMP
    record.update(temp=kiro_temp.public_temp(temp))
    environment = kiro_temp.temp_environment(environment, temp, bool(settings.get("pytest_basetemp")))
    record.diagnostic(f"Managed temp directory: {temp['path']} (kept until accepted; see kiro_temp.py)\n")
    catalog = subprocess.run(
        [binary, "chat", "--v3", "--list-models", "--format", "json"],
        cwd=cwd, env=environment, capture_output=True, text=True, timeout=min(60, max(.01, deadline - time.monotonic())),
    )
    if catalog.returncode:
        record.diagnostic(catalog.stderr + "Kiro model query failed; coding was not started.\n")
        return catalog.returncode
    data = json.loads(catalog.stdout)
    available = [item["model_id"] for item in data["models"]]
    if model not in available:
        record.diagnostic(f"Required model {model} is absent from this execution environment's catalog.\n"
                          + "Available models: " + ", ".join(available) + "\n"
                          + "No model was substituted; coding was not started and the task prompt was NOT sent.\n")
        return 2

    command = [binary, "chat", "--v3", "--no-interactive", "--output-format", "stream-json", "--model", model]
    if effort:
        command += ["--effort", effort]
    command.append("--trust-tools=" + ("" if check else TOOLS))
    if record.state.get("resume_id"):
        command += ["--resume-id", record.state["resume_id"]]
    record.diagnostic(f"Kiro: model={model}, effort={effort or 'none (model has no effort setting)'}, cwd={cwd}\n")
    state_dir = Path(environment.get("KIRO_PANEL_STATE_DIR") or kiro_catalog.STATE_DIR)
    with tempfile.TemporaryDirectory(prefix="kiro-relay-", dir=temp["path"]) as relay_dir, \
            tempfile.TemporaryFile(mode="w+t", encoding="utf-8", dir=temp["path"]) as task:
        status_file = Path(relay_dir) / "status.json"
        environment = relay_environment(environment, status_file, model, effort, state_dir / "model-catalog.json")
        task.write(prompt)
        task.seek(0)
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=task,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        child = kiro_temp.read_stat(process.pid) or {}
        record.update(status="running", pid=process.pid, pid_start_ticks=child.get("start"),
                      child_identity=agent_tasks.process_identity(process.pid))
        selection_failed = False
        seen = [None]
        decoders = {"events.jsonl": codecs.getincrementaldecoder("utf-8")("replace"),
                    "stderr.log": codecs.getincrementaldecoder("utf-8")("replace")}
        recent_stderr = ""
        event_buffer = ""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, ("events.jsonl", sys.stdout))
                selector.register(process.stderr, selectors.EVENT_READ, ("stderr.log", sys.stderr))
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Kiro task exceeded --timeout; inspect logs before retrying")
                    sync_startup(record, status_file, seen)
                    for key, _ in selector.select(timeout=min(1, max(.01, deadline - time.monotonic()))):
                        name, output = key.data
                        block = os.read(key.fileobj.fileno(), 65536)
                        text = decoders[name].decode(block, final=not block)
                        record.logs[name].write(text)
                        record.logs[name].flush()
                        output.write(text)
                        output.flush()
                        if name == "events.jsonl":
                            event_buffer += text
                            lines = event_buffer.split("\n")
                            event_buffer = lines.pop()
                            for line in lines:
                                try:
                                    event = json.loads(line)
                                    session_id = event.get("data", {}).get("sessionId")
                                    if session_id and not record.state.get("session_id"):
                                        record.update(session_id=session_id)
                                except (ValueError, AttributeError):
                                    pass
                        if name == "stderr.log":
                            recent_stderr = (recent_stderr + text)[-8192:]
                            if SELECTION_FAILURE.search(recent_stderr):
                                selection_failed = True
                                process.terminate()
                                break
                        if not block:
                            selector.unregister(key.fileobj)
                    if selection_failed:
                        break
            try:
                result = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                result = 1
        except BaseException:
            agent_tasks.stop_process_group(process)
            status = read_status(status_file, settle=.2)
            record.update(prompt_forwarded=bool(status.get("prompt_forwarded")))
            record.diagnostic("The task prompt " + ("had already been forwarded; the task may have partly run.\n"
                              if status.get("prompt_forwarded") else "was never forwarded to Kiro.\n"
                              if status.get("final") else "may or may not have been forwarded; inspect the working tree.\n"))
            raise
        finally:
            agent_tasks.stop_process_group(process)
            process.stdout.close()
            process.stderr.close()
        return judge(record, read_status(status_file, settle=12), selection_failed, result)


def judge(record, status, selection_failed, result):
    """Final exit code. Success needs the relay's positive confirmation that model and effort were set and
    the prompt was forwarded exactly once; the relay persists `prompt_forwarded` before forwarding."""
    selection = getattr(record, "selection", None) or {"model": MODEL, "effort": EFFORT}  # helpers/older callers
    model, effort = selection["model"], selection["effort"]
    forwarded = bool(status.get("prompt_forwarded"))
    record.update(prompt_forwarded=forwarded, startup=status.get("reason") or status.get("state") or "unknown")
    if selection_failed:
        record.diagnostic("Rejected model/effort selection failure. Do not accept this run as successful.\n")
        if not forwarded and status.get("final"):
            record.diagnostic("The task prompt was NOT sent to the model; resubmitting is safe.\n")
        return 2
    if status.get("state") == "failed" and not forwarded:
        reason = status.get("reason", "")
        record.diagnostic(gate_message(reason, model, effort) + "\n")
        if status.get("final"):
            record.diagnostic("The relay proved the prompt never reached Kiro; resubmitting is safe.\n")
        return 2
    if result == 0 and not (forwarded and status.get("confirmed")):
        record.diagnostic("Kiro exited successfully but the relay never confirmed the selected model and effort; "
                          "this run is not accepted.\n")
        return 2
    if result != 0 and result != 130 and forwarded:
        record.diagnostic("The task prompt had already been forwarded to Kiro, so the task may have partly run. "
                          "Do not resubmit it blindly; inspect the working tree and the run logs first.\n")
    elif result != 0 and not forwarded and status.get("final"):
        record.diagnostic("The task prompt was never forwarded to Kiro; resubmitting is safe.\n")
    return result if result >= 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--cli-path", help="Executable path from saved CLI connection")
    parser.add_argument("--timeout", type=float, default=3600, help="Overall task deadline in seconds")
    parser.add_argument("--check", action="store_true", help="Check availability and run a tool-free smoke request")
    parser.add_argument("--run-dir", type=Path, help="New directory for task, live events, diagnostics, and status")
    parser.add_argument("--agent-id", help="Stable identity for multiple turns of one subagent")
    parser.add_argument("--resume-id", help="Continue a Kiro session (default model/effort unless --model is given)")
    parser.add_argument("--title", help="Display name of this subagent conversation")
    parser.add_argument("--model", help="Explicit model (panel use). Needs --effort LEVEL or --no-effort. Codex delegation "
                        "omits this and gets claude-sonnet-5.5 / medium")
    parser.add_argument("--effort", help="Explicit effort level for the selected model")
    parser.add_argument("--no-effort", action="store_true", help="The selected model has no effort setting: none is passed")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    selection = {"model": MODEL, "effort": EFFORT, "source": "default"}
    if args.model or args.effort or args.no_effort:
        if args.no_effort and args.effort:
            parser.error("--effort and --no-effort are mutually exclusive")
        if args.no_effort and not args.model:
            parser.error("--no-effort needs --model")
        if args.model and not (args.effort or args.no_effort):
            parser.error("--model needs --effort LEVEL or --no-effort; no effort is guessed")
        if (args.model and not kiro_catalog.MODEL_ID.fullmatch(args.model)) or \
                (args.effort and not kiro_catalog.EFFORT_ID.fullmatch(args.effort)):
            parser.error("invalid --model or --effort value")
        selection = {"model": args.model or MODEL, "effort": None if args.no_effort else args.effort, "source": "explicit"}
    cwd = args.cwd.expanduser().resolve(strict=True)
    if not cwd.is_dir():
        parser.error("--cwd must be a directory")
    from cli_connections import runner_executable
    binary = runner_executable("kiro", args.cli_path)
    prompt = ("Reply only KIRO_READY. Do not call tools or read files." if args.check else sys.stdin.read())
    if not prompt.strip():
        parser.error("Provide a non-empty task on stdin")
    environment = kiro_environment()
    environment["KIRO_CLI"] = binary

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    directory = args.run_dir.expanduser().resolve() if args.run_dir else cwd / "work" / "kiro-runs" / run_id
    record = RunRecord(cwd, prompt, directory, args.agent_id, args.resume_id, args.title, selection)
    agent_tasks.register(cwd, directory.parent)
    def interrupt_run(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt_run)
    result = 1
    try:
        result = execute(binary, cwd, prompt, environment, args.check, record, args.timeout)
    except KeyboardInterrupt:
        record.diagnostic("Kiro run interrupted.\n")
        result = 130
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        record.diagnostic(f"Kiro runner failed: {error}\n")
    finally:
        record.finish(result)
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"Kiro runner failed: {error}", file=sys.stderr)
        sys.exit(1)
