"""User-global role configuration; reads never write files or invoke a CLI."""
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from agent_providers import ClaudeModels, provider

BUILTINS = ("architect", "coder", "tester", "reviewer")
MAX_ROLES = 66  # retain every custom role from an older 64-role configuration
FIELDS = {"id", "name", "enabled", "description", "provider", "model", "effort",
          "instructions", "constraints", "deliverables"}
DEFAULTS = [
    {"id": "architect", "name": "架构师", "enabled": True, "description": "制定技术方案、接口和模块边界。",
     "provider": None, "model": None, "effort": None,
     "instructions": "阅读需求与现有实现，说明关键取舍，给出开发和测试可使用的接口约定与验收要点。",
     "constraints": ["默认只读，不修改实现或测试。", "说明未经验证的假设。"],
     "deliverables": ["技术方案与关键取舍", "接口与影响范围", "实施顺序与验收要点"]},
    {"id": "coder", "name": "编码", "enabled": True, "description": "实现功能、修复业务代码，并处理测试反馈。",
     "provider": None, "model": None, "effort": None,
     "instructions": "根据主 Agent 下达的需求和验收标准实现功能。收到测试反馈后定位并修复业务实现，再交由测试角色复测。",
     "constraints": ["遵守主 Agent 指定的修改范围和项目规范。", "不修改测试角色负责的测试脚本，不降低验收标准。",
                     "需求有歧义或与测试反馈有分歧时，报告主 Agent 判断。"],
     "deliverables": ["改动说明", "相关文件与必要验证结果", "待测试事项及已知问题"]},
    {"id": "tester", "name": "测试", "enabled": True, "description": "编写测试脚本，编码完成后执行测试、反馈并复测。",
     "provider": None, "model": None, "effort": None,
     "instructions": "根据需求和验收标准独立编写测试脚本。准备阶段仅编写脚本并报告就绪，等待主 Agent 确认编码完成后，按执行任务运行测试、输出反馈；修复完成后复测。不要自行轮询等待或自动启动编码角色。",
     "constraints": ["只修改测试脚本及主 Agent 明确授权的测试辅助文件，不修改业务实现。",
                     "不因实现未通过而降低测试标准；测试自身有误时说明原因并修正。",
                     "对需求和验收标准的疑问交由主 Agent 判断。"],
     "deliverables": ["测试脚本与执行命令", "执行阶段的通过/失败结果和失败证据", "反馈给编码角色的复现步骤与待复测项"]},
    {"id": "reviewer", "name": "代码审查", "enabled": True, "description": "独立审查实际改动的正确性、兼容性和维护风险。",
     "provider": None, "model": None, "effort": None,
     "instructions": "对照需求审查实际差异和测试，优先发现回归、安全风险和测试缺口。",
     "constraints": ["默认只读，不修改实现或测试。", "按严重程度排列问题，说明证据与不确定性。"],
     "deliverables": ["问题、文件位置、触发条件和影响", "修改建议与未覆盖的验证项"]},
]


def roles_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not base.is_absolute():
        raise ValueError("XDG_CONFIG_HOME 必须是绝对路径")
    return base / "coder-agents/roles.json"


def validate_role(role):
    if not isinstance(role, dict) or set(role) != FIELDS:
        raise ValueError("角色字段不完整或包含未知字段")
    for key, limit in (("id", 64), ("name", 80), ("description", 1000), ("instructions", 16000)):
        value = role[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
            raise ValueError("角色字段无效：" + key)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role["id"]):
        raise ValueError("角色 ID 只能使用小写字母、数字和连字符，并以字母开头")
    if type(role["enabled"]) is not bool:
        raise ValueError("enabled 必须为布尔值")
    if role["provider"] is None:
        if role["model"] is not None or role["effort"] is not None:
            raise ValueError("请先为角色选择 CLI，再设置模型和思考强度")
    else:
        provider(role["provider"])
    from claude_catalog import MODEL_ID
    pattern = MODEL_ID if role["provider"] == "claude" else re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,199}")
    model = role["model"]
    if model is not None and (not isinstance(model, str) or not pattern.fullmatch(model)):
        raise ValueError("模型 ID 无效")
    if model is None and role["effort"] is not None:
        raise ValueError("沿用默认模型时须同时沿用默认思考强度")
    if role["effort"] is not None and role["effort"] not in ("low", "medium", "high", "max"):
        raise ValueError("思考强度无效")
    if model is not None and role["provider"] == "claude":
        ClaudeModels().validate(role["model"], role["effort"])
    for key in ("constraints", "deliverables"):
        values = role[key]
        if not isinstance(values, list) or len(values) > 40 or any(
                not isinstance(v, str) or not v.strip() or len(v) > 2000 or "\0" in v for v in values):
            raise ValueError(key + " 必须是文本列表，每项不能为空")
    return deepcopy(role)


class RoleStore:
    def __init__(self, path=None, connections=None):
        self.path = Path(path) if path is not None else roles_path()
        self.connections = connections
        self.lock = threading.RLock()

    def read(self):
        try:
            raw = self.path.read_bytes()
            if len(raw) > 131072:
                raise ValueError("角色配置不能超过 128 KB")
            data = json.loads(raw)
        except FileNotFoundError:
            return {"version": 1, "roles": deepcopy(DEFAULTS)}, "missing"
        except (OSError, ValueError) as error:
            raise ValueError("无法读取 roles.json，请检查角色配置：" + str(error)) from error
        if not isinstance(data, dict) or set(data) != {"version", "roles"} or type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("角色配置版本或格式无效")
        roles = data["roles"]
        if not isinstance(roles, list) or not 2 <= len(roles) <= MAX_ROLES:
            raise ValueError("角色数量必须为 2–" + str(MAX_ROLES))
        for role in roles:
            validate_role(role)
        ids = [role["id"] for role in roles]
        if len(set(ids)) != len(ids):
            raise ValueError("角色 ID 重复")
        saved = dict(zip(ids, roles))
        if saved.get("reviewer", {}).get("name") == "代码审查员":
            saved["reviewer"]["name"] = "代码审查"
        data["roles"] = [saved.pop(r["id"], r) for r in deepcopy(DEFAULTS)] + list(saved.values())
        return data, hashlib.sha256(raw).hexdigest()

    def snapshot(self):
        with self.lock:
            data, revision = self.read()
            return {**data, "revision": revision}

    def get(self, role_id, executable=False, connections=None):
        for role in self.snapshot()["roles"]:
            if role["id"] == role_id:
                if executable and not role["enabled"]:
                    raise ValueError("角色已停用，请在设置中启用：" + role_id)
                if executable and role["provider"] is None:
                    raise ValueError("请先在角色设置中选择 CLI：" + role["name"])
                if executable and role["model"] is None:
                    from cli_connections import Connections
                    role.update((connections or self.connections or Connections()).defaults(
                        role["provider"], require_configured=True))
                return role
        raise ValueError("角色不存在：" + str(role_id))

    def mutate(self, payload, *, model_settings=False):
        allowed = ("models",) if model_settings else ("save", "delete")
        if not isinstance(payload, dict) or payload.get("action") not in allowed:
            raise ValueError("未知角色操作")
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "a") as lockfile:
                fcntl.flock(lockfile, fcntl.LOCK_EX)
                data, revision = self.read()
                if payload.get("revision") != revision:
                    raise ValueError("角色配置已更新，请刷新角色列表后重试；当前草稿仍保留")
                if payload["action"] == "save":
                    role = validate_role(payload.get("role"))
                    index = next((i for i, r in enumerate(data["roles"]) if r["id"] == role["id"]), None)
                    if payload.get("create") and index is not None:
                        raise ValueError("角色 ID 已存在，请使用不同的 ID")
                    if index is None:
                        if len(data["roles"]) >= MAX_ROLES:
                            raise ValueError(f"最多支持 {MAX_ROLES} 个角色")
                        data["roles"].append(role)
                    else:
                        data["roles"][index] = role
                elif payload["action"] == "models":
                    overrides = payload.get("overrides")
                    if not isinstance(overrides, dict) or any(not isinstance(pair, dict) or set(pair) != {"model", "effort"} for pair in overrides.values()):
                        raise ValueError("无效的角色模型配置")
                    selected = payload["provider"]
                    provider(selected)
                    switches = set(payload.get("switch_roles", []))
                    eligible = {r["id"]: r for r in data["roles"]
                                if r["provider"] == selected or r["id"] in switches}
                    if set(overrides) - eligible.keys():
                        raise ValueError("角色的 CLI 已变化，请重新打开模型设置")
                    for role_id, pair in overrides.items():
                        role = eligible[role_id]
                        role.update(validate_role({**role, **pair, "provider": selected}))
                else:
                    role_id = payload.get("id")
                    if not isinstance(role_id, str):
                        raise ValueError("角色 ID 无效")
                    if role_id in BUILTINS:
                        raise ValueError("内置角色不能删除，可以停用")
                    if not any(r["id"] == role_id for r in data["roles"]):
                        raise ValueError("角色不存在")
                    data["roles"] = [r for r in data["roles"] if r["id"] != role_id]
                raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
                if len(raw) > 131072:
                    raise ValueError("角色配置不能超过 128 KB")
                fd, temp = tempfile.mkstemp(prefix=".roles-", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, self.path)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                return {**data, "revision": hashlib.sha256(raw).hexdigest()}


def task_prompt(role, task):
    if not isinstance(task, str) or not task.strip():
        raise ValueError("请通过 stdin 提供非空任务")
    lines = ["# 角色：" + role["name"], role["description"], "", "## 执行说明", role["instructions"],
             "", "## 工作约束", *["- " + item for item in role["constraints"]],
             "", "## 交付要求", *["- " + item for item in role["deliverables"]],
             "", "角色配置不会扩大工具权限；遵守本次任务授权和项目规范。", "", "# 本次任务", task]
    return "\n".join(lines)


def continuation(role_id, prior_provider, task, store=None, connections=None):
    """Resolve current role for a follow-up, never crossing native CLI sessions."""
    role = (store or RoleStore()).get(role_id, executable=True, connections=connections)
    if role['provider'] != prior_provider:
        raise ValueError('角色的 CLI 已变更，请新建对话；不能跨 CLI 继续原会话')
    return role, task_prompt(role, task)


def identity(environment=None):
    """Record role identity and whether the panel chose a conversation-specific execution setup."""
    environment = os.environ if environment is None else environment
    role_id, name = environment.get('CODER_AGENTS_ROLE_ID'), environment.get('CODER_AGENTS_ROLE_NAME')
    if not isinstance(role_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', role_id):
        return {}
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or '\0' in name:
        return {}
    identity = {'role_id': role_id, 'role_name': name}
    if environment.get('CODER_AGENTS_ROLE_OVERRIDE') == '1':
        identity['role_execution_override'] = True
    return identity
