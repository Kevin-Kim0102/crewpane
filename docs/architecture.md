# 产品架构与迁移说明

## 产品方向

Coder-Agents 的方向是通用的本地 CLI Agent 可视化面板：Python 标准库服务 + 浏览器页面 + 本机已安装的 CLI Agent。已接入 Kiro CLI 与 Claude Code。

## Provider 架构

```text
Codex skill / 终端 → coder_agents run --role ID
浏览器 → watch_kiro.py → 按对话 provider 选择 runner
                            ├─ run_claude.py → claude -p（stream-json）
                            └─ run_kiro.py → ACP relay → kiro-cli
```

`scripts/agent_providers.py` 提供后端元数据和 Claude 模型配置。`scripts/cli_connections.py` 管理显式连接、登录检查和私有配置持久化；读取配置不运行 CLI。`run_agent.py` 分发 CLI 调用；通过进程替换把信号直接交给 runner。`run_claude.py` 独立处理 Claude 的执行、会话、结果判定和日志，不经过 Kiro ACP。Kiro 模型与额度服务延迟初始化，Claude-only 使用不探测 Kiro。

每轮记录 provider，已有 Kiro 记录缺少字段时按 Kiro 解释。面板后端选择只作用于新对话，续聊按保存的 provider 和 cwd 路由。CLI 续聊需要显式传原 provider 和原生 session ID。数据继续保存在 `work/kiro-runs/`，不迁移旧历史。

Claude 保留 `raw-events.jsonl`，并把完整 assistant/tool/result 消息转换为既有面板事件格式。由于未启用 partial messages，文本以完整消息更新，工具调用仍实时显示。Claude 续聊事件是当前调用的输出，前端不会套用 Kiro 的历史重放过滤规则。成功需要零退出码、成功 result 事件、没有权限拒绝且输出格式完整；退出码为零而缺失结果仍判失败。

Claude 用独立 POSIX 进程组运行。取消和超时先发 SIGTERM，必要时升级 SIGKILL，并写入终态。默认 `dontAsk`，CLI 可显式选 acceptEdits/plan 或允许特定工具，不加入 bypassPermissions。不会自动重试任务；Claude CLI 自身可能重试网络请求。CLI 的原生会话存储由 Claude 管理。

Claude 模型目录来自当前生效的 API 地址，主动读取并按请求配置隔离缓存。`claude_catalog.py` 解析 CLI 配置并查询有界、禁止重定向的模型接口；思考强度选项来自 CLI 规则，实际支持由执行时校验。只支持可复用 API Key/Auth Token 的目录认证，不读取私有订阅凭据。产品默认模型和强度保存在连接配置中，角色调用仍以最新角色配置为准。用量和报告费用来自当前 result 事件；`claude_usage.py` 可主动从当前 Claude API 地址的 `/v1/usage` 读取 Sub2API 兼容额度，按配置指纹隔离缓存，不固定域名。未返回重置时间时不推算；不支持协议的 API 明确提示。Kiro 的实际模型目录、强度门禁和额度接口保留原行为。

`agent_tasks.py` 共用任务进程身份、异常恢复、单任务停止及全局日志来源登记。两个 runner 都记录启动周期、进程出生标识和独立 CLI 进程组，并支持整体执行超时。`tasks list/show/stop` 与面板共用能力，身份不确定时不误杀其他进程。`agent_diagnostics.py` 统一错误分类及有界脱敏摘要。任务记录角色 ID 和名称，不保留角色配置快照。面板标记手动调整执行配置的对话；续聊读取最新角色职责，并沿用这类对话记录的 CLI、模型和强度。其他角色对话跟随角色配置并检查后端是否一致。

## Kiro 内部架构


```
浏览器 ──HTTP(127.0.0.1:固定端口)──> scripts/watch_kiro.py（面板服务，Python 标准库）
                                         │ 读取 assets/viewer.html、assets/vendor/*
                                         │ 注册表/配置在状态目录
                                         ▼
                              scripts/run_kiro.py（runner，每个任务一个进程）
                                         │ 固定模型 claude-sonnet-5.5 + medium + V3
                                         ▼
                      scripts/kiro_acp_relay.py（模型/强度门禁）──> kiro-cli
```

| 模块 | 职责 |
| --- | --- |
| `watch_kiro.py` | 本机面板服务、HTTP API、项目注册表、可选的 systemd socket 激活 |
| `run_kiro.py` | 启动 Kiro 任务，写入 `work/kiro-runs/<id>/{task.txt,events.jsonl,stderr.log,state.json}` |
| `kiro_acp_relay.py` | 转发 ACP 流量，确认“本次所选”模型与强度后才转发任务（门禁，未改动） |
| `kiro_acp.py` | ACP 客户端、kiro-cli/Node 定位 |
| `kiro_catalog.py` | 账户模型目录与强度缓存 |
| `kiro_usage.py` | 只读账户额度 |
| `kiro_review.py` | 只读文件/Git 审查 |
| `project_workspace.py` | 按项目列目录、读取带版本的文本、创建、保存和移到回收站 |
| `kiro_temp.py` | 任务临时目录的归属校验与显式清理 |
| `coder_agents/` | 新增：统一 CLI 与服务安装器，不改变上述模块的行为边界 |

安全约束保持不变：只监听回环；浏览器只拿 HttpOnly cookie，token 不出现在页面；模型与强度由后端对照真实目录校验；
自动清理由 `auto_cleanup_enabled` 控制：`serve`/`service install` 新建的配置写入 `false`，但缺少该键的旧配置按开启处理；没有明确请求不会删除任何对话或日志。

项目浏览使用 Codex 项目列表中的主根目录，文件树按层加载。写操作使用同源 JSON 和现有登录 cookie，校验项目根目录与文件版本；保存通过同目录临时文件原子替换，Git 查询仍只读。Monaco 和两个 Web Worker 随包发布，首次打开文本时从面板同源加载。

## 数据位置

- 面板配置与注册表：`~/.local/state/kiro-panel/`（沿用旧位置，`CODER_AGENTS_CONFIG` 可指定其他配置文件，注册表与缓存在其同目录）。
- 用户设置：`${XDG_CONFIG_HOME:-~/.config}/coder-agents/settings.json`（`CODER_AGENTS_SETTINGS` 可覆盖）。
- 任务日志：各项目 `work/kiro-runs/`，临时目录 `work/tmp/`。
- 个人历史位置不会被本仓库的任何命令移动或删除。

## 相对原 Kiro Panel 的改动

1. 目录：`scripts/*.py`（8 个）与 `assets/`（viewer.html、vendor 及许可证）原样迁入，只做下列最小改动。
2. `run_kiro.py`：设置文件不再取自 skill 目录旁的 `settings.json`，改为用户配置目录或 `CODER_AGENTS_SETTINGS`；`proxy_defaults` 缺省时视为空。
3. `watch_kiro.py`：配置路径可由 `CODER_AGENTS_CONFIG` 指定；没有已注册项目且配置无 `default_project` 时不再报错，只提供“其他对话”；缺配置的提示改为指向 `service install`/`serve`。
4. 测试迁入 `tests/`，去除本机路径；DOM 测试改用 npm 的 jsdom。
5. 新增 `coder_agents` CLI、服务安装器、文档与 CI。

## 从旧安装迁移

旧安装由 `~/.codex/skills/kiro-coding` 与 `codex-kiro-panel.*` 用户单元组成。本阶段**不**改动它们：

1. 并行使用：旧服务继续占用 47831，新仓库可用 `python3 -m coder_agents status`、`run`、`settings` 等。
2. 设置迁移（可选）：`python3 -m coder_agents settings migrate-legacy --apply`。
3. 代码来源统一（无需改 systemd）：`python3 -m coder_agents legacy link --apply`，见 [legacy-link.md](legacy-link.md)。
4. 服务切换（后续阶段，需人工执行）：在没有活跃任务时，确认旧服务已按计划停用后再运行 `service install --apply`。
   安装器沿用旧 `config.json`/注册表，所以历史、端口和 token 保持不变；旧单元文件与清理定时器由使用者自行处理。
5. `~/.local/state/kiro-panel` 及其中的历史、`work/kiro-runs` 不会被迁移或删除。

## 已知限制

- 前台面板与 Claude runner 支持 macOS / Linux；服务安装仍依赖 Linux systemd 用户实例。
- 设置页统一清理 Claude/Kiro 非运行任务的结果、日志和受管理临时目录，不要求验收或 Linux `/proc` 检查；checking/running 跳过，保留目录边界检查。Claude 新任务也设置 TMPDIR/TMP/TEMP，旧任务可清理已有日志。独立 kiro_temp CLI 的验收行为保持兼容。
- 面板只读读取 Codex 私有状态文件（非公开接口，格式可能变化） `~/.codex/.codex-global-state.json` 来列出 Codex 项目；文件缺失时只显示“其他对话”和已注册项目，不影响使用。
- “其他对话”的日期目录沿用 `~/Documents/Codex/<日期>`（日期按香港时区），可用配置项 `daily_base` 修改。

## 打包说明

除源码克隆外，也支持 `pip install -e .` 和普通安装，但文档主路径是源码克隆。wheel 内含 `coder_agents/_runtime/{scripts,assets,examples}`，由 `setup.py` 在从 sdist 构建时打入，源码树缺文件时构建失败；`MANIFEST.in` 精确列出 sdist 内容（排除 work、缓存、个人配置）。安装后用 `python3 -m coder_agents selfcheck` 验证。

## CLI 连接设置

浏览器 GET `/api/connections` 读取连接记录；POST `/api/connections` 使用现有 cookie、同源校验和 JSON 限制。支持 connect/disconnect/default/test/catalog/pause/start。连接操作为单并发，重复检查返回 409。路径输入是可执行文件绝对路径，不通过 shell，也不接受命令参数。

Connect 执行 `--version` 并识别产品；Claude Code 使用 `auth status --json` 检查 loggedIn，Kiro 使用 `whoami` 的退出状态。未登录时，仅显式 Connect 操作允许打开图形终端运行官方登录命令，并在最多 180 秒内轮询认证状态；macOS 使用 Terminal，Linux 使用可用的图形终端。登录凭据仅由 CLI 保存。检查有超时、stdin 关闭，失败不保存候选、不泄露认证输出；成功原子替换权限 0600 的 connections.json，不保存 token。路径保留符号链接，不固化升级器目标路径。

面板默认后端只来自保存的连接；既有 config.json 中的 default_provider 不再自动启用 CLI。未连接时历史可读，发送与模型/额度服务阻断。新任务通过 `--cli-path` 传入已保存路径，Kiro 元数据进程同时使用 KIRO_CLI，避免短 PATH 选错二进制。

已连接时普通页面读取同样不启动 CLI：Kiro 模型目录仅从缓存或固定默认选择返回，账户额度仅返回缓存。Connect 成功后自动读取模型目录；设置中的读取目录可重试，额度刷新仍是显式操作。目录失败保留已成功的连接，并返回单独的重试提示。Kiro 运行时目录遵循平台约定：macOS 使用 `~/Library/Application Support/kiro-cli`，Linux 使用 `~/.local/share/kiro-cli`，显式 XDG_DATA_HOME 和运行时覆盖配置优先。

Test 为独立、显式的模型调用，在隔离临时目录执行 runner --check 并关闭工具。Claude 有 30 秒内部超时，外层最多等待 40 秒。它与连接状态独立，不因页面打开或连接成功而自动执行。CLI 原生登录状态可以在连接后发生变化，保存状态只表示上次检查成功。

CLI 连接记录继续用 enabled 表示是否已连接，新增 paused 标识暂停。暂停时 connected 保持 true、available=false，executable 拒绝新任务与 PATH 回退；启动检查路径后恢复使用，已有子进程不受影响。没有保存默认后端时终端调度使用 Claude。配置页为每个 provider 分开保存模型列表和请求反馈，打开时仅读取缓存，显式获取才刷新；概览不显示操作结果。
