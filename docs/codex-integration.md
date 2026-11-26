# Codex 集成（可选）

Coder-Agents 定位为 Subagent 工作台，仅用于执行主 Agent 委派的任务，不建议直接作为主 Agent 独立完成开发。建议由 Codex 等主 Agent 拆解需求、协调编码与测试，并审核和最终验收。技术上不依赖 Codex，但面板中的直接消息主要用于补充指令和排查问题。本页介绍 Codex 的集成方式，基本使用见 [README](../README.md)。

模板支持 **Claude Code 与 Kiro CLI**。委派必须使用 `run --role ID` 动态读取用户角色；`--provider` 仅用于 `--check` 诊断。无需先启动面板，也无需 MCP 服务。CLI 使用本机已有登录状态。

在面板“设置 → Codex 技能”复制安装提示词，在此项目的 Codex 对话中执行即可配置全局 `coder-agents` Skill。提示词仅指导 Codex 定位项目、自行查找全局技能目录、按模板安装并校验，不展示本机源码路径或硬编码安装地址。角色读取、调度与验收规则统一维护在 Skill 模板中。该页面只复制提示词，不会直接写入个人技能目录。

也可以用模板生成**你自己的**全局 skill；渲染结果含个人绝对路径，不要提交到仓库。已有同名技能时先检查并保留自定义内容。

```bash
ROOT="$(pwd)"                                  # Coder-Agents 源码目录
DEST="${CODEX_HOME:-$HOME/.codex}/skills/coder-agents"
mkdir -p "$DEST/agents"
python3 -c 'import pathlib,sys; src=pathlib.Path(sys.argv[1]); dest=pathlib.Path(sys.argv[2]); dest.write_text((src/"integrations/codex/SKILL.md.template").read_text().replace("{{CODER_AGENTS_ROOT}}",str(src)))' "$ROOT" "$DEST/SKILL.md"
cp integrations/codex/openai.yaml.template "$DEST/agents/openai.yaml"
```

- 使用这个 skill **不需要修改全局 `AGENTS.md`**，本仓库也没有为此提供 setup 命令。只有你希望在所有项目里都默认委派时，才自行手动往个人偏好里添加规则；本仓库不会替你改。
- 模板中的命令统一为 `PYTHONPATH=<ROOT> python3 -m coder_agents ...`。
- 面板读取 Codex 项目列表是只读的，缺少 Codex 时自动降级为“其他对话”。
- 不要在启用本 skill 的同时让旧 `kiro-coding`/`kiro-panel` skill 去操作同一端口的另一份服务；切换前先完成 [迁移](architecture.md#从旧安装迁移)。
- 直接调用保留 Kiro 默认模型门禁；角色调用采用用户保存的模型/思考强度。模型与工具权限由对应 CLI 验证，不接受静默回退。

## 一句话打开和关闭面板

安装或更新 Skill 后，直接告诉 Codex：“启动 Coder-Agents，打开面板”或“关闭 Coder-Agents 面板”。Skill 调用 `start` 等待服务就绪，再通过 Codex 的浏览器工具打开返回地址；关闭时调用 `stop`。后台启动不依赖终端持续打开，重复调用复用已有面板。面板服务的空闲退出行为不变，需要时再说一句打开即可。

```bash
PYTHONPATH="<ROOT>" python3 -m coder_agents start
PYTHONPATH="<ROOT>" python3 -m coder_agents stop
```

启动失败不会打开页面；端口被其他程序或无法验证的旧服务占用时明确报告，不自动换端口或结束其他进程。后台日志位于面板配置旁的 `panel.log`。仅执行角色任务不需要启动面板。

## 全局角色与开发分工

在“设置 → Subagent 角色”管理全局角色。配置保存在 `$XDG_CONFIG_HOME/coder-agents/roles.json`，未设置 XDG_CONFIG_HOME 时使用 `~/.config/coder-agents/roles.json`。首次读取提供默认角色但不写文件，第一次显式保存时才创建。内置角色可编辑、启停；自定义角色可新增、编辑、启停、删除。用户角色不保存在 Skill 目录内，也没有项目级覆盖。

编辑时从对应 CLI 的模型列表中选择，思考强度联动显示模型支持的选项。此读取使用已有模型目录，不触发付费测试请求；目录未就绪时先到“CLI 连接”中连接并读取目录。已保存的模型不在当前可选列表中时保留提示，需明确重新选择后保存，不自动替换。

- `architect`（架构师）：需要设计时先提出技术方案，明确接口、模块边界、取舍和验收要点，由 Codex 确认后交给编码与测试。
- `coder`（编码）：实现业务代码，可补自己模块的单元测试；根据反馈修复实现，保留测试角色负责的独立脚本与标准。
- `tester`（测试）：负责独立验收和回归。接口明确时可先写脚本；接口未定时先列用例与预期结果。编码完成后由 Codex 单独派发验证，测试角色报告证据、修正有误的脚本并复测。
- `reviewer`（代码审查）：验收前独立查看实际改动、需求和测试证据，按严重程度报告问题、文件位置与影响。
- Codex 负责分工、时序、反馈协调、审核与最终验收，不是本产品的角色。

```bash
PYTHONPATH="<ROOT>" python3 -m coder_agents roles list
PYTHONPATH="<ROOT>" python3 -m coder_agents roles show tester
PYTHONPATH="<ROOT>" python3 -m coder_agents roles list --all
PYTHONPATH="<ROOT>" python3 -m coder_agents run --role coder \
  --cwd /绝对路径/项目 --agent-id coder --title "实现任务" < task.txt
```

每次委派前查询当前角色，执行器也重新加载角色配置。使用 `--role` 时不要同时传 provider/model/effort、cli-path 或 check；CLI、模型与思考强度由角色决定。Claude 角色需要编辑时，按本次授权传 `--permission-mode acceptEdits`，执行测试命令时按需允许具体工具规则。角色约束是执行说明，不是沙箱或权限授权。

产品不会自行等待编码完成、循环运行测试或自动派发修复任务；由 Codex 明确安排测试准备、编码、测试执行和修复复测。续聊前读取当前角色并核对原会话 provider/cwd；角色切换 CLI 后开启新会话。角色修改对下次委派生效，不额外保存角色配置快照，通常的任务和执行日志仍保留。

## Claude 委派与续聊

安装后在新对话中加载。Skill 允许自动选用，读取用户的当前全局角色；是否委派由 Codex 根据任务判断。也可用 `$coder-agents` 明确调用，例如：“使用 coder-agents，让编码和测试角色完成这个功能，由你审核并验收”。仅在实际请求中授权的范围内运行工具。

```bash
# 在任意目录调用源码 checkout 中的入口；用实际路径替换 <ROOT>。
printf '%s\n' '阅读项目并总结，不修改文件' | \
  PYTHONPATH=<ROOT> python3 -m coder_agents run --role reviewer \
    --cwd /绝对路径/项目 --agent-id reviewer --title "Claude 审阅" \
    --run-dir /绝对路径/新的运行日志目录
```

读取运行目录的 `state.json` 和 `result.txt`。成功条件是 `status=completed` 且 `exit_code=0`。失败时读取 `stderr.log`、`raw-events.jsonl` 并检查实际文件改动，不能仅依据最终回复接受结果。

下一轮使用原 cwd 和 agent ID，再加 `--resume-id <session_id>`，输出到另一个新的运行日志目录。角色调用使用最新角色模型与强度；选择跟随默认值的角色自动使用对应 CLI 的当前默认配置。面板中没有角色的普通会话仍可继续。尚未保存默认配置时兼容使用 `sonnet` / `medium`，实际模型另存于 `actual_model`。需要修改文件时显式加 `--permission-mode acceptEdits`；需要执行命令时显式允许具体工具规则。默认不进行全量权限绕过。

macOS 和 Linux 日常使用 `start` 后台启动，`serve` 留给前台调试；`service install` 仍只支持 Linux systemd。修改 skill 模板不会自动覆盖已安装的个人 skill，需要重新渲染模板。仓库不写入全局 `AGENTS.md`。

## 任务状态与诊断

`tasks list` 列出自动登记的任务；`tasks show --run-dir DIR` 返回状态和有界最终结果；`tasks stop --run-dir DIR` 只停止这个任务。面板关闭期间启动的任务也会登记，面板下次打开时能发现独立 worktree 或自定义日志目录。

任务记录角色 ID 和名称，不保存角色配置快照。面板续聊重新读取最新角色职责；角色停用时拒绝继续。面板新对话可手动调整角色的 CLI、模型或思考强度，续聊保留该对话选择。未手动调整的对话跟随角色配置；如果角色更换了 CLI，需新建对话。崩溃、强制退出或重启后，仅在进程身份能证明任务已经结束时恢复为中断；身份无法确认时保留状态并提示排查，不误杀其他进程。停止或失败后检查已有代码，不自动重提任务。

权限拒绝、认证、额度、模型、网络、超时和未知错误会提供脱敏摘要，可在面板诊断页复制。Claude 正常退出且报告成功时，工具拒绝单独显示为提示；已完成不代表被拒绝的操作已执行。连接成功不等于任务工具已获授权；权限拒绝时不自动扩权。

## Claude 模型目录与默认配置

在 Claude CLI 配置页主动获取当前 API 地址的 `/v1/models` 目录，支持自定义代理/中转、分页及按生效配置隔离的缓存；页面自动刷新只读缓存，不自动发模型目录请求。设置解析覆盖进程环境、用户设置、当前工作目录的项目/本地设置及文件托管设置，使用与 CLI 一致的覆盖顺序。不读取私有登录凭据存储，不执行 apiKeyHelper；仅订阅登录、云平台认证或不支持目录接口的服务会明确提示限制，不以固定列表代替。

连接任一 CLI 后，弹窗会提示选择默认模型和思考强度。在弹窗中展开角色配置可单独设置角色。连接另一个 CLI 时，角色默认保留原 CLI 的模型和思考强度，包括跟随原 CLI 默认值的角色；未分配 CLI 的角色也显示「保留」原 CLI 已保存的默认模型与思考强度，作为预览；保存新 CLI 默认值时不会自动分配角色。之后也可在详情页的「模型与思考强度」卡片修改当前 CLI 的默认值，跟随该 CLI 默认值的角色会自动使用最新设置。默认模型和强度保存在产品连接配置中，不改写 CLI 自身的设置文件；不同项目配置了不同 API 地址时分别读取目录。思考强度选项来自 CLI，不能据此认定 API 已报告模型能力；可以选择“不设置”。凭据只在后端使用，不写入目录缓存、页面或复制的诊断。

## 保存的 CLI 连接

在面板设置中连接后，终端和 Codex 委派会优先使用 `connections.json` 中的路径；断开的 CLI 不会静默回退到 PATH。尚无该后端的连接记录时，显式 CLI 调用仍可通过 PATH 使用本机 CLI。面板本身始终要求显式连接。连接检查不会请求模型，测试调用会消耗额度。

Claude 任务和「测试调用」使用 Claude CLI 自身的 API 配置，并继承启动环境中已有的代理变量。Coder-Agents 用户设置中的 `proxy_defaults` 只用于 Kiro，不会注入 Claude，因此无需为 Claude 网关维护 `NO_PROXY` 域名名单。
