"""Build hook: put scripts/ and assets/ inside the wheel as coder_agents/_runtime/{scripts,assets}.

The scripts locate assets as <scripts dir>/../assets, so this layout works unchanged. A source checkout
(and `pip install -e .`) keeps using the top-level scripts/ and assets/ directly."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = Path(__file__).resolve().parent
REQUIRED = ["scripts/watch_kiro.py", "scripts/run_kiro.py", "assets/viewer.html", "assets/vendor/marked.umd.js", "examples/settings.example.json"]
REQUIRED += ["assets/vendor/" + name for name in (
    "monaco-editor.js", "monaco-editor.css", "monaco-worker.js", "monaco-json-worker.js",
    "monaco.LICENSE.txt", "monaco.ThirdPartyNotices.txt", "codicon.ttf", "codicon.LICENSE.txt",
)]


class BuildWithRuntime(build_py):
    def run(self):
        missing = [name for name in REQUIRED if not (HERE / name).is_file()]
        if missing:
            raise SystemExit("Refusing to build: runtime files missing from the source tree: " + ", ".join(missing))
        super().run()
        runtime = Path(self.build_lib) / "coder_agents/_runtime"
        if runtime.exists():
            shutil.rmtree(runtime)
        for name in ("scripts", "assets", "examples"):
            shutil.copytree(HERE / name, runtime / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


setup(cmdclass={"build_py": BuildWithRuntime})
