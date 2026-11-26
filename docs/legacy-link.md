# 旧安装连接迁移（legacy link）

目的：让旧 `~/.codex/skills/kiro-coding` 里已有的 systemd `ExecStart` 和 Codex skill 路径继续可用，同时运行的代码只来自本仓库。无需改 systemd、不重启服务。

## 做什么、不做什么

`legacy link --apply` 只对旧安装里的 `scripts/` 和 `assets/` 各做两步：

1. 改名（rename）到旧安装内新建的唯一备份目录 `backup-coder-agents-<时间>-<随机>/`（不删除任何文件，备份里有 `MANIFEST.json`）。
2. 原位创建目录符号链接，指向本仓库的 `scripts/`、`assets/`。

不动：`SKILL.md`、`settings.json`、`agents/`、面板配置、注册表、历史、`auto_cleanup_enabled`；不扫描历史；不重启服务。

## 保护

- 默认预演；只有 `--apply` 才执行。旧安装路径用 `--legacy DIR` 指定，默认 `~/.codex/skills/kiro-coding`。
- 已是指向本仓库的链接则跳过（重复执行无副作用）；指向别处的链接、缺失目录、普通文件一律拒绝。
- 有活跃任务（`checking`/`running`）拒绝；无法读取注册表或任务状态时同样拒绝（fail closed）。`--apply` 前会再检查一次。
- 任一步失败自动回滚（撤销已建链接、把已改名的目录改回），退出码 1；拒绝的退出码是 3。

## 命令

```bash
cd /path/to/crewpane
python3 -m crewpane legacy link                 # 预演，查看计划
python3 -m crewpane legacy link --apply         # 执行
python3 -m crewpane selfcheck                   # 检查资源
python3 -m crewpane status
```

## 回滚

推荐（同样默认预演，且同样拒绝活跃任务）：

```bash
python3 -m crewpane legacy unlink               # 预演；有多个备份时加 --backup <备份目录>
python3 -m crewpane legacy unlink --apply
```

它会删除两个符号链接（只在它们指向本仓库时），把备份里的目录改回原位，并把备份里的 `MANIFEST.json` 改名为 `MANIFEST.restored.json`；备份目录本身保留。

手动回滚（无 Python 时，先确认没有活跃任务；`B` 为备份目录）：

```bash
L=~/.codex/skills/kiro-coding
B=$(ls -d "$L"/backup-coder-agents-* | tail -1)
rm "$L/scripts" "$L/assets"        # 只能是符号链接；rm 不带 -r，目录会拒绝删除
mv "$B/scripts" "$L/scripts" && mv "$B/assets" "$L/assets"
```

链接后 Python 可能在本仓库 `scripts/` 下写入 `__pycache__`（已被 `.gitignore` 忽略）。
