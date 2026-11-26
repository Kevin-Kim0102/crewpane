"""crewpane: a local browser panel for Kiro CLI and Claude Code sessions."""
from pathlib import Path

__version__ = "0.1.0"

_HERE = Path(__file__).resolve().parent
# Source checkout (and editable install): <root>/{coder_agents,scripts,assets}.
# Installed wheel: scripts/ and assets/ are packaged as coder_agents/_runtime/.
ROOT = _HERE.parent if (_HERE.parent / "scripts/watch_kiro.py").is_file() else _HERE / "_runtime"
SCRIPTS = ROOT / "scripts"
ASSETS = ROOT / "assets"
MODULES = ("kiro_acp", "kiro_acp_relay", "kiro_catalog", "kiro_review", "kiro_temp", "kiro_usage", "run_kiro", "watch_kiro",
           "agent_providers", "run_agent", "run_claude", "cli_connections", "agent_roles", "agent_tasks", "agent_diagnostics", "claude_catalog", "claude_usage",
           "project_workspace")
RESOURCES = tuple(f"scripts/{m}.py" for m in MODULES) + (
    "assets/viewer.html", "assets/vendor/marked.umd.js", "assets/vendor/purify.min.js",
    "assets/vendor/marked.LICENSE.txt", "assets/vendor/dompurify.LICENSE-Apache-2.0.txt",
    "assets/vendor/dompurify.LICENSE-MPL-2.0.txt", "assets/vendor/monaco-editor.js", "assets/vendor/monaco-editor.css",
    "assets/vendor/monaco-worker.js", "assets/vendor/monaco-json-worker.js", "assets/vendor/codicon.ttf",
    "assets/vendor/monaco.LICENSE.txt", "assets/vendor/monaco.ThirdPartyNotices.txt", "assets/vendor/codicon.LICENSE.txt")


def missing_resources():
    return [name for name in RESOURCES if not (ROOT / name).is_file()]
