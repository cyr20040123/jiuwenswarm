# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent 管理命令:agent-register / agent-list / agent-inspect。

薄封装 AgentConfigService(复用其名字校验与内置 agent 保护),
注册后惰性初始化 sidecar 状态(version=1)。
"""

from __future__ import annotations

import argparse
from typing import Callable

from jiuwenswarm.harness_evolve import agent_state, paths
from jiuwenswarm.harness_evolve.commands import out
from jiuwenswarm.harness_evolve.errors import HarnessUsageError


def _service():
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    return AgentConfigService()


def _parse_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _register_agent_reg(sub: argparse.ArgumentParser) -> None:
    """agent-register:新建自定义 agent 并初始化 sidecar 状态。"""
    sub.add_argument("--name", required=True, help="agent 名(3-50 字符,[A-Za-z0-9_-])")
    sub.add_argument("--description", required=True, help="一句话描述")
    sub.add_argument("--prompt-file", required=True, help="prompt 正文文件(md/文本)")
    sub.add_argument("--model", default=None, help="模型名(缺省用 config default)")
    sub.add_argument("--tools", default=None, help="工具白名单,逗号分隔(缺省 '*' 全部)")
    sub.add_argument("--skills", default=None, help="预加载 skill 名,逗号分隔")
    sub.add_argument("--max-iterations", type=int, default=None)
    sub.add_argument("--when-to-use", default=None, help="何时调度此 agent 的说明")
    sub.add_argument("--location", choices=["user", "project", "local"],
                     default="user", help="存储位置(默认 user)")


def _run_agent_register(args) -> dict:
    from pathlib import Path

    from jiuwenswarm.server.runtime.agent_config_service import CreateAgentParams

    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_file():
        raise HarnessUsageError(f"prompt 文件不存在: {prompt_file}")
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise HarnessUsageError("prompt 文件内容为空")

    params = CreateAgentParams(
        name=args.name,
        description=args.description,
        prompt=prompt,
        location=args.location,
        model=args.model,
        tools=_parse_csv(args.tools),
        skills=_parse_csv(args.skills),
        max_iterations=args.max_iterations,
        when_to_use=args.when_to_use,
    )
    try:
        created = _service().create_agent(params)
    except ValueError as exc:
        raise HarnessUsageError(str(exc)) from exc

    state = agent_state.ensure_agent_state(created.name, created.file_path)
    # 主 agent 消费自定义 agent 需要 config 显式启用(_load_custom_subagents),
    # 注册后自动补上这一步,用户无需手改配置
    from jiuwenswarm.harness_evolve.config_registry import enable_subagent_in_config

    enabled, enable_msg = enable_subagent_in_config(created.name)
    out(args, f"agent '{created.name}' 已创建: {created.file_path}(version={state['version']})")
    out(args, f"  {enable_msg}")
    # 通知运行中的 agent 服务热重载(失败不阻断,降级为"重启后生效"提示)
    from jiuwenswarm.harness_evolve.hot_reload import notify_agent_reload

    hot_ok, hot_msg = notify_agent_reload()
    if hot_msg:
        out(args, f"  {hot_msg}")
    return {
        "name": created.name,
        "file_path": created.file_path,
        "version": state["version"],
        "subagent_enabled": enabled,
        "hot_reload": hot_ok,
        "hot_reload_msg": hot_msg or None,
    }


def _register_agent_list(sub: argparse.ArgumentParser) -> None:
    """agent-list:列出自定义 agent(含 sidecar 版本)。"""


def _run_agent_list(args) -> dict:
    agents = []
    for agent in _service().list_agents():
        if agent.source == "builtin":
            continue
        entry = {
            "name": agent.name,
            "description": agent.description,
            "model": agent.model,
            "tools": agent.tools,
            "enabled": agent.enabled,
            "source": agent.source,
        }
        if agent_state.state_exists(agent.name):
            entry["version"] = agent_state.agent_version(agent.name)
        else:
            entry["version"] = None
        agents.append(entry)
        ver = f"v{entry['version']}" if entry["version"] else "-"
        tools = ",".join(agent.tools or ["*"])
        out(args, f"{agent.name:<20} {ver:<5} {tools:<30} {agent.description}")
    if not agents:
        out(args, "(无自定义 agent)")
    return {"agents": agents}


def _register_agent_inspect(sub: argparse.ArgumentParser) -> None:
    """agent-inspect:查看 agent 完整定义与 sidecar 状态。"""
    sub.add_argument("--name", required=True)


def _run_agent_inspect(args) -> dict:
    agent = _service().get_agent(args.name)
    if agent is None:
        raise HarnessUsageError(f"agent 不存在: {args.name}")
    result = {
        "name": agent.name,
        "description": agent.description,
        "prompt": agent.prompt,
        "model": agent.model,
        "tools": agent.tools,
        "disallowed_tools": agent.disallowed_tools,
        "skills": agent.skills,
        "max_iterations": agent.max_iterations,
        "when_to_use": agent.when_to_use,
        "permission_mode": agent.permission_mode,
        "memory_scope": agent.memory_scope,
        "source": agent.source,
        "file_path": agent.file_path,
        "enabled": agent.enabled,
    }
    state = None
    if agent_state.state_exists(agent.name):
        state = dict(agent_state.get_agent_state(agent.name))
        state["snapshots"] = sorted(
            p.name for p in paths.snapshots_dir(agent.name).glob("v*.tar.gz")
        )
    result["state"] = state
    out(args, f"# {agent.name} ({agent.source})")
    out(args, f"description: {agent.description}")
    out(args, f"model: {agent.model or '(default)'} | tools: {','.join(agent.tools or ['*'])}")
    if state:
        out(
            args,
            f"version: {state['version']} | next_version: {state['next_version']} "
            f"| baseline: {state['baseline_version']} | snapshots: {state['snapshots']}",
        )
    out(args, "--- prompt ---")
    out(args, agent.prompt)
    return result


def _register_agent_export_package(sub: argparse.ArgumentParser) -> None:
    """agent-export-package:把 agent 打包为 Harness 包(注入主 agent)。"""
    sub.add_argument("--name", required=True, help="agent 名")
    sub.add_argument(
        "--activate",
        action="store_true",
        help="打包后标记为激活(默认只打包;重启 agent 服务或到 web 面板激活)",
    )


def _run_agent_export_package(args) -> dict:
    from jiuwenswarm.harness_evolve.package_exporter import export_agent_package

    result = export_agent_package(args.name, activate=args.activate)
    out(args, f"agent '{result['name']}' 已打包: {result['package_dir']}")
    out(args, f"  package_id: {result['package_id']}")
    out(args, f"  prompt_sections: {result['prompt_sections']} "
              f"| skills: {','.join(result['skills']) or '-'}")
    skipped = ",".join(result["skipped_tools"]) or "-"
    out(args, f"  工具不打包(主 agent 自带,声明会导致热加载绑定冲突),已跳过: {skipped}")
    if result["activated"]:
        out(args, "  已标记激活 -- 到 web 端 Harness Package 管理面板热加载即生效")
    else:
        out(args, "  未激活 -- 到 web 端 Harness Package 管理面板激活后生效")
    out(args, f"  注意:重启 agent 服务会清空激活回到 Native,需重新激活")
    return result


def _register_agent_export(sub: argparse.ArgumentParser) -> None:
    """agent-export:导出跨机迁移 bundle(定义+状态+快照+skills)。

    与 agent-export-package 的区别:后者打包 Harness 包注入主 agent,
    本命令导出可在其它电脑导入复用的完整 agent。
    """
    sub.add_argument("--name", required=True, help="agent 名")
    sub.add_argument("--out", required=True, help="输出 zip 路径(自动补 .zip 后缀)")


def _run_agent_export(args) -> dict:
    from jiuwenswarm.harness_evolve.agent_transfer import export_agent_bundle

    result = export_agent_bundle(args.name, args.out)
    snaps = ",".join(f"v{n}" for n in result["snapshots"]) or "-"
    out(args, f"agent '{result['name']}' 已导出: {result['zip']}")
    out(args, f"  快照: {snaps} | skills: {','.join(result['skills']) or '-'}")
    if result["skills_missing"]:
        out(args, f"  技能缺失(已跳过): {','.join(result['skills_missing'])}")
    return result


def _register_agent_import(sub: argparse.ArgumentParser) -> None:
    """agent-import:从 bundle 导入 agent(同名拒绝,--as 改名导入)。"""
    sub.add_argument("--file", required=True, help="bundle zip 路径")
    sub.add_argument("--as", dest="as_name", default=None,
                     help="改名导入(目标机器已有同名 agent 时)")


def _run_agent_import(args) -> dict:
    from jiuwenswarm.harness_evolve.agent_transfer import import_agent_bundle

    result = import_agent_bundle(args.file, new_name=args.as_name)
    verb = "已改名导入" if result["renamed"] else "已导入"
    out(args, f"agent '{result['name']}' {verb}: {result['file_path']} "
              f"(version={result['version']}, 快照 {len(result['snapshots'])} 个)")
    if result["skills_skipped_existing"]:
        out(args, f"  技能已存在跳过(未覆盖): {','.join(result['skills_skipped_existing'])}")
    if result["skills_missing"]:
        out(args, f"  技能缺失(已跳过): {','.join(result['skills_missing'])}")
    out(args, f"  {result['subagent_enable_msg']}")
    # 通知运行中的 agent 服务热重载(失败不阻断,降级为"重启后生效"提示)
    from jiuwenswarm.harness_evolve.hot_reload import notify_agent_reload

    hot_ok, hot_msg = notify_agent_reload()
    if hot_msg:
        out(args, f"  {hot_msg}")
    result["hot_reload"] = hot_ok
    result["hot_reload_msg"] = hot_msg or None
    return result


def _register_agent_delete(sub: argparse.ArgumentParser) -> None:
    """agent-delete:删除 agent 及其关联产物(危险,需 --yes 确认)。"""
    sub.add_argument("--name", required=True, help="agent 名")
    sub.add_argument("--yes", action="store_true", help="确认删除")
    sub.add_argument("--with-benchmarks", action="store_true",
                     help="连同引用它的 benchmark 一并删除(默认保留)")


def _print_delete_plan(args, plan: dict) -> None:
    side = plan["sidecar"]
    snaps = ",".join(f"v{n}" for n in side["snapshots"]) or "-"
    out(args, f"将删除 agent '{plan['name']}':")
    out(args, f"  - 定义文件: {plan['md']['path']}")
    if side["exists"]:
        out(args, f"  - sidecar: state={side['state']}, 快照 {snaps}, "
                  f"工作区 {side['workspaces']} 个目录")
    if plan["package"]["exists"]:
        out(args, f"  - Harness 包: {plan['package']['path']}")
    if plan["config_entry"]:
        out(args, f"  - config.yaml 条目: react.subagents.{plan['name']}")
    if plan["benchmarks"]:
        action = "一并删除" if args.yes and getattr(args, "with_benchmarks", False) else "保留(加 --with-benchmarks 一并删除)"
        out(args, f"  - 引用它的 benchmark({action}): {', '.join(plan['benchmarks'])}")


def _run_agent_delete(args) -> dict:
    from jiuwenswarm.harness_evolve.agent_transfer import (
        collect_delete_plan,
        execute_delete,
    )

    plan = collect_delete_plan(args.name)
    _print_delete_plan(args, plan)
    if not args.yes:
        summary = f"将删除 {plan['name']}(benchmark 影响: {plan['benchmarks'] or '无'})"
        raise HarnessUsageError(f"未确认删除 -- {summary};加 --yes 执行")
    result = execute_delete(args.name, with_benchmarks=args.with_benchmarks)
    out(args, f"agent '{result['name']}' 已删除: "
              f"md={result['deleted'].get('md')}, sidecar={result['deleted'].get('sidecar')}, "
              f"package={result['deleted'].get('package')}, config={result['deleted'].get('config_entry')}")
    for warning in result["warnings"]:
        out(args, f"  [警告] {warning}")
    # 通知运行中的 agent 服务热重载(失败不阻断,降级为"重启后生效"提示)
    from jiuwenswarm.harness_evolve.hot_reload import notify_agent_reload

    hot_ok, hot_msg = notify_agent_reload()
    if hot_msg:
        out(args, f"  {hot_msg}")
    result["hot_reload"] = hot_ok
    result["hot_reload_msg"] = hot_msg or None
    return result


def register(registry: dict[str, tuple[Callable, Callable]]) -> None:
    registry["agent-register"] = (_register_agent_reg, _run_agent_register)
    registry["agent-list"] = (_register_agent_list, _run_agent_list)
    registry["agent-inspect"] = (_register_agent_inspect, _run_agent_inspect)
    registry["agent-export-package"] = (
        _register_agent_export_package,
        _run_agent_export_package,
    )
    registry["agent-export"] = (_register_agent_export, _run_agent_export)
    registry["agent-import"] = (_register_agent_import, _run_agent_import)
    registry["agent-delete"] = (_register_agent_delete, _run_agent_delete)
