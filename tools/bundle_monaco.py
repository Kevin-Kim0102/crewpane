"""Rebuild offline Monaco assets from an unpacked npm distribution (see assets/vendor/README.md)."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


MONACO_VERSION = "0.55.1"
LANGUAGES = (
    "cpp", "csharp", "css", "dockerfile", "go", "hcl", "html", "ini", "java", "javascript", "lua",
    "markdown", "php", "python", "ruby", "rust", "shell", "sql", "typescript", "xml", "yaml",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="extracted monaco-editor npm package directory")
    parser.add_argument("esbuild", type=Path, help="path to the esbuild executable")
    args = parser.parse_args()
    package = args.package.resolve()
    if json.loads((package / "package.json").read_text())["version"] != MONACO_VERSION:
        parser.error("expected monaco-editor " + MONACO_VERSION)
    source = package / "esm/vs"
    dest = Path(__file__).resolve().parents[1] / "assets/vendor"
    esbuild = [str(args.esbuild.resolve()), "--bundle", "--format=esm", "--minify", "--target=es2022",
               "--legal-comments=none"]
    with tempfile.TemporaryDirectory(prefix="coder-agents-monaco-") as temp:
        entry = Path(temp) / "entry.js"
        imports = ["editor/editor.all.js", "language/json/monaco.contribution.js"] + [
            "basic-languages/%s/%s.contribution.js" % (name, name) for name in LANGUAGES
        ]
        entry.write_text('''globalThis.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    return new Worker(new URL(label === "json" ? "./monaco-json-worker.js" : "./monaco-worker.js", import.meta.url),
      { type: "module", name: label });
  }
};
''' + "\n".join("import " + json.dumps(str(source / name)) + ";" for name in imports) +
            "\nexport { editor, Uri, KeyMod, KeyCode } from " + json.dumps(str(source / "editor/editor.api.js")) + ";\n")
        subprocess.run([*esbuild, str(entry), "--loader:.ttf=file", "--asset-names=[name]",
                        "--outfile=" + str(dest / "monaco-editor.js")], check=True)
        for path, name in (("editor/editor.worker.js", "monaco-worker.js"),
                           ("language/json/json.worker.js", "monaco-json-worker.js")):
            subprocess.run([*esbuild, str(source / path), "--outfile=" + str(dest / name)], check=True)
    shutil.copyfile(package / "LICENSE", dest / "monaco.LICENSE.txt")
    shutil.copyfile(package / "ThirdPartyNotices.txt", dest / "monaco.ThirdPartyNotices.txt")


if __name__ == "__main__":
    main()
