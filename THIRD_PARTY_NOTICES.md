# 第三方声明

随仓库分发（`assets/vendor/`，校验值和打包方法见 `assets/vendor/README.md`）：

| 文件 | 来源 | 版本 | 许可证 |
| --- | --- | --- | --- |
| `marked.umd.js` | [marked](https://www.npmjs.com/package/marked) `lib/marked.umd.js` | 18.0.11 | MIT，`assets/vendor/marked.LICENSE.txt` |
| `purify.min.js` | [DOMPurify](https://www.npmjs.com/package/dompurify) `dist/purify.min.js` | 3.4.17 | Apache-2.0 或 MPL-2.0，`assets/vendor/dompurify.LICENSE-*.txt` |
| `monaco-editor.js`、`monaco-editor.css`、`monaco-*-worker.js`、`monaco-worker.js` | [Monaco Editor](https://github.com/microsoft/monaco-editor)，基于 npm 发布文件打包 | 0.55.1 | MIT，`assets/vendor/monaco.LICENSE.txt`；第三方声明见 `monaco.ThirdPartyNotices.txt` |
| `codicon.ttf` | Microsoft [Codicons](https://github.com/microsoft/vscode-codicons)，随 Monaco 同版本发布的字体 | Monaco 0.55.1 | CC BY 4.0，`assets/vendor/codicon.LICENSE.txt` |

仅用于测试、不随产品分发：`jsdom`（devDependency，MIT；完整依赖树见 `package-lock.json`）。

本仓库原创代码的许可证尚待作者确认，见 `docs/licensing.md`。
