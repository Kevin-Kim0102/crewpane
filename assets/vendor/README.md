# Vendored browser libraries

The panel works offline. These files are served by `watch_kiro.py` from an explicit allowlist
from the repository's `assets/vendor/` directory. The two files below are unmodified npm release files.

| File | Package | Version | License |
| --- | --- | --- | --- |
| `marked.umd.js` | [marked](https://www.npmjs.com/package/marked) `lib/marked.umd.js` | 18.0.11 | MIT (`marked.LICENSE.txt`) |
| `purify.min.js` | [dompurify](https://www.npmjs.com/package/dompurify) `dist/purify.min.js` | 3.4.17 | Apache-2.0 or MPL-2.0 (`dompurify.LICENSE-*.txt`) |

SHA-256 of the shipped files:

```
69451c8541c9c1e7a4bf3ffc6f73c4d89633de92bfbe3e484dfe182ef8091f88  marked.umd.js
cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31  purify.min.js
```

Both files end with a `sourceMappingURL` comment. The `.map` files are intentionally not shipped,
so browser developer tools may log a harmless 404 for them.

To update, replace the files with a newer npm release, update the table and hashes, and run
`python3 -m unittest discover -s tests` and `npm test`.

## Monaco Editor

The editor, language definitions, and two workers are bundled locally from
[`monaco-editor@0.55.1`](https://www.npmjs.com/package/monaco-editor/v/0.55.1). The panel loads them
when a project file is opened. Codicon is Microsoft’s [Codicons](https://github.com/microsoft/vscode-codicons)
font from the same distribution (CC BY 4.0; unmodified).

To rebuild, unpack Monaco’s npm tarball and use the esbuild 0.25.12 binary for your platform:

```sh
python3 tools/bundle_monaco.py /path/to/extracted/package /path/to/esbuild
```

SHA-256 (npm tarball: `eec3721fb6b1dc5a0bd1a73e38a5eb5d0c3791af684f7d2571efb90ad8634871`):

```
7ce90b1738ff39da245117f2f247efbce1728b8fe67014146bcf4ad6e40acfbe  monaco-editor.js
a2c463693b19b9f7194ff11c001461fbb4d9c1098d80be7ff73296c95ddf6615  monaco-editor.css
9d25513c861704be650eacef8c4588aceabdc8b668857747b5e42b299e926918  codicon.ttf
52e5aba8568bcde6ecbd33212bce5d1a79eb2f4dbf947748993d63f6217434c6  monaco-worker.js
9cf05bb418d46366b7a1930d58f277c4a7d196dd50bb307ebf4759a8b68e69d0  monaco-json-worker.js
```

## Provider marks

`viewer.html` embeds the Claude and Kiro website icons as local SVG symbols (retrieved 2026-11-19 from https://claude.com/icon.svg and https://kiro.dev/icon.svg). These identify the respective CLI providers; marks belong to their owners. No external icon requests are made by the panel.
