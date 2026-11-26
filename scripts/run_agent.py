#!/usr/bin/env python3
"""Dispatch local agent tasks without a shell."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

import agent_providers
from cli_connections import Connections
from agent_roles import RoleStore, task_prompt


def role_invocation(role_id, remaining, task):
    role, args, prompt = prepare_role(role_id, remaining, task)
    return role['provider'], args, prompt


def prepare_role(role_id, remaining, task):
    reserved = {"--provider", "--model", "--effort", "--no-effort", "--check", "--cli-path"}
    if any(arg.split("=", 1)[0] in reserved for arg in remaining):
        raise ValueError("使用 --role 时，CLI、模型和思考强度由角色配置决定，不能覆盖或执行 --check")
    role = RoleStore().get(role_id, executable=True)
    args = [*remaining, "--model", role["model"]]
    args += ["--effort", role["effort"]] if role["effort"] else ["--no-effort"]
    return role, args, task_prompt(role, task)


def verify_role_resume(role, remaining):
    """Do not attach a new role or switch providers on an existing native session."""
    values = {}
    for index, value in enumerate(remaining):
        name, separator, inline = value.partition('=')
        if name in {'--resume-id', '--cwd'}:
            values[name] = inline if separator else remaining[index + 1] if index + 1 < len(remaining) else ''
    if not values.get('--resume-id') or not values.get('--cwd'):
        return
    import json
    from agent_tasks import registered_sources
    cwd = Path(values['--cwd']).expanduser().resolve()
    for source in registered_sources():
        for path in Path(source['root']).glob('*/state.json'):
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(state, dict) or state.get('session_id') != values['--resume-id']:
                continue
            if not isinstance(state.get('cwd'), str) or Path(state['cwd']).resolve() != cwd:
                continue
            if state.get('role_id') != role['id'] or state.get('provider', 'kiro') != role['provider']:
                raise ValueError("原会话的角色或 CLI 不匹配，请新建会话")
            return
    raise ValueError("找不到与角色和工作目录匹配的会话，请新建会话")


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--provider", choices=tuple(agent_providers.PROVIDERS))
    parser.add_argument("--role", help="Global role ID from roles list")
    args, remaining = parser.parse_known_args(argv)
    prompt = None
    # Inherited role metadata must not label an independent direct provider call.
    os.environ.pop('CODER_AGENTS_ROLE_ID', None)
    os.environ.pop('CODER_AGENTS_ROLE_NAME', None)
    os.environ.pop('CODER_AGENTS_ROLE_OVERRIDE', None)
    help_only = "--help" in remaining or "-h" in remaining
    check = "--check" in remaining
    if not args.role and not check and not help_only:
        parser.error("派发任务必须指定 --role ID；--provider 只用于 --check")
    if check and not args.role and not args.provider and not help_only:
        parser.error("--check 必须明确指定 --provider")
    if check and "--resume-id" in {arg.split("=", 1)[0] for arg in remaining}:
        parser.error("--check 不能继续已有会话")
    if args.role and not help_only:
        try:
            if args.provider:
                raise ValueError("--role 与 --provider 不能同时使用，请在角色设置中选择 CLI")
            role, remaining, prompt = prepare_role(args.role, remaining, sys.stdin.read())
            verify_role_resume(role, remaining)
            args.provider = role['provider']
            os.environ['CODER_AGENTS_ROLE_ID'] = role['id']
            os.environ['CODER_AGENTS_ROLE_NAME'] = role['name']
        except (ValueError, OSError) as error:
            parser.error(str(error))
    args.provider = args.provider or "claude"  # help only when no provider or role was passed
    if not args.role and not help_only:
        # Product defaults apply only when the caller did not supply a model/effort pair.
        names = {arg.split('=', 1)[0] for arg in remaining}
        has_model = '--model' in names
        has_effort = '--effort' in names or '--no-effort' in names
        if not has_model:
            defaults = Connections().defaults(args.provider)
            remaining += ['--model', defaults['model']]
            if not has_effort:
                remaining += ['--effort', defaults['effort']] if defaults.get('effort') else ['--no-effort']
    if help_only:
        print("Tasks: --role ID (uses the role's CLI, model and effort)", flush=True)
        print("Diagnostics: --provider kiro|claude --check", flush=True)
    script = Path(__file__).resolve().with_name(agent_providers.provider(args.provider)["runner"])
    if prompt is not None:
        with tempfile.TemporaryFile() as stream:
            stream.write(prompt.encode("utf-8")); stream.seek(0)
            os.dup2(stream.fileno(), 0)
    os.execv(sys.executable, [sys.executable, str(script), *remaining])


if __name__ == "__main__":
    sys.exit(main())
