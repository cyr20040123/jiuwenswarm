# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-optimization 命令:agent-version / snapshot-create / agent-edit /
agent-restore / baseline-record / optimize。

- 编辑一律经 agent-edit(白名单字段 + 前置快照 + 漂移检测 + 版本单调),
  禁止技能直接改 *.md;
- optimize 一键复合:Reference=scoreboard 末条(运行时一致性校验),每轮
  snapshot → proposal LLM 生成候选 → agent-edit → eval-matrix →
  接受=每 cell 有效且顶层均分严格大于 Reference(入 scoreboard),
  否则 restore(不入 scoreboard);达 target 或轮数上限早停。
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Callable

from jiuwenswarm.harness_evolve import agent_state, paths
from jiuwenswarm.harness_evolve.commands import out
from jiuwenswarm.harness_evolve.errors import HarnessUsageError


def _parse_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _service():
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    return AgentConfigService()


def _require_state(name: str) -> dict:
    if not agent_state.state_exists(name):
        raise HarnessUsageError(
            f"agent '{name}' 无 sidecar 状态:请用 agent-register 重新注册"
        )
    return agent_state.get_agent_state(name)


# ── agent-version ──────────────────────────────────────────────────────────


def _register_agent_version(sub: argparse.ArgumentParser) -> None:
    """agent-version:查看被测 agent 的 sidecar 版本。"""
    sub.add_argument("--name", required=True)


def _run_agent_version(args) -> dict:
    state = _require_state(args.name)
    result = {
        "name": args.name,
        "version": state["version"],
        "next_version": state.get("next_version"),
        "baseline_version": state.get("baseline_version"),
        "md_sha256": state.get("md_sha256"),
    }
    out(
        args,
        f"version={state['version']} | next={state.get('next_version')} "
        f"| baseline={state.get('baseline_version')}",
    )
    return result


# ── snapshot-create ────────────────────────────────────────────────────────


def _register_snapshot_create(sub: argparse.ArgumentParser) -> None:
    """snapshot-create:创建 v<version>.tar.gz(存在即拒,禁覆盖)。"""
    sub.add_argument("--name", required=True)
    sub.add_argument("--version", type=int, default=None,
                     help="快照版本(默认当前版本)")


def _run_snapshot_create(args) -> dict:
    state = _require_state(args.name)
    version = args.version or int(state["version"])
    snapshot_path = agent_state.create_snapshot(args.name, version)
    out(args, f"快照已创建: {snapshot_path}")
    return {"name": args.name, "version": version, "snapshot": str(snapshot_path)}


# ── agent-edit ─────────────────────────────────────────────────────────────


def _register_agent_edit(sub: argparse.ArgumentParser) -> None:
    """agent-edit:候选版本方式修改 agent 定义(前置快照 + 漂移检测 + 版本+1)。"""
    sub.add_argument("--name", required=True)
    sub.add_argument("--expected-version", type=int, required=True,
                     help="基于哪个版本编辑(必须等于当前内容版本)")
    sub.add_argument("--prompt-file", default=None, help="新 prompt 正文文件")
    sub.add_argument("--description", default=None)
    sub.add_argument("--model", default=None)
    sub.add_argument("--tools", default=None, help="逗号分隔工具白名单")
    sub.add_argument("--skills", default=None, help="逗号分隔 skill 名")
    sub.add_argument("--max-iterations", type=int, default=None)
    sub.add_argument("--when-to-use", default=None)
    sub.add_argument("--force", action="store_true",
                     help="md 文件被外部修改时强制按当前文件对齐后继续")


def _run_agent_edit(args) -> dict:
    from pathlib import Path

    prompt = None
    if args.prompt_file:
        prompt_file = Path(args.prompt_file)
        if not prompt_file.is_file():
            raise HarnessUsageError(f"prompt 文件不存在: {prompt_file}")
        prompt = prompt_file.read_text(encoding="utf-8")
        if not prompt.strip():
            raise HarnessUsageError("prompt 文件内容为空")

    state = agent_state.apply_agent_edit(
        args.name,
        expected_version=args.expected_version,
        force=args.force,
        prompt=prompt,
        description=args.description,
        model=args.model,
        tools=_parse_csv(args.tools),
        skills=_parse_csv(args.skills),
        max_iterations=args.max_iterations,
        when_to_use=args.when_to_use,
    )
    out(
        args,
        f"agent '{args.name}' 已更新: version {args.expected_version} -> "
        f"{state['version']} (next={state.get('next_version')})",
    )
    # 通知运行中的 agent 服务热重载(失败不阻断,降级为"重启后生效"提示)
    from jiuwenswarm.harness_evolve.hot_reload import notify_agent_reload

    hot_ok, hot_msg = notify_agent_reload()
    if hot_msg:
        out(args, f"  {hot_msg}")
    return {
        "name": args.name,
        "version": state["version"],
        "next_version": state.get("next_version"),
        "md_path": state.get("md_path"),
        "hot_reload": hot_ok,
        "hot_reload_msg": hot_msg or None,
    }


# ── agent-restore ──────────────────────────────────────────────────────────


def _register_agent_restore(sub: argparse.ArgumentParser) -> None:
    """agent-restore:从快照恢复(文件 + sidecar 版本回退,写后校验)。"""
    sub.add_argument("--name", required=True)
    sub.add_argument("--version", type=int, required=True, help="恢复到哪个快照版本")


def _run_agent_restore(args) -> dict:
    state = agent_state.restore_snapshot(args.name, args.version)
    out(args, f"已恢复到 v{args.version}(当前 version={state['version']})")
    # 通知运行中的 agent 服务热重载(失败不阻断,降级为"重启后生效"提示)
    from jiuwenswarm.harness_evolve.hot_reload import notify_agent_reload

    hot_ok, hot_msg = notify_agent_reload()
    if hot_msg:
        out(args, f"  {hot_msg}")
    return {
        "name": args.name,
        "version": state["version"],
        "restored_to": args.version,
        "md_path": state.get("md_path"),
        "hot_reload": hot_ok,
        "hot_reload_msg": hot_msg or None,
    }


# ── baseline-record ────────────────────────────────────────────────────────


def _register_baseline_record(sub: argparse.ArgumentParser) -> None:
    """baseline-record:记录 benchmark 冻结时的 agent 基线版本。"""
    sub.add_argument("--name", required=True)
    sub.add_argument("--version", type=int, default=None,
                     help="基线版本(默认当前版本)")


def _run_baseline_record(args) -> dict:
    state = _require_state(args.name)
    version = args.version or int(state["version"])
    state = agent_state.record_baseline(args.name, version)
    out(args, f"baseline_version={state['baseline_version']}")
    return {"name": args.name, "baseline_version": state["baseline_version"]}


# ── optimize ───────────────────────────────────────────────────────────────


def _register_optimize(sub: argparse.ArgumentParser) -> None:
    """optimize:一键复合优化(需 scoreboard 已有已接受基线)。"""
    sub.add_argument("--agent", required=True)
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--target", type=float, required=True, help="目标均分")
    sub.add_argument("--rounds", type=int, default=3, help="最大轮数(默认 3)")
    sub.add_argument("--runs", type=int, default=1, help="每 case run 数(默认 1)")
    sub.add_argument("--parallel", type=int, default=2,
                     help="矩阵并发度(默认 2=子进程并发;1=顺序)")
    sub.add_argument("--model", default=None)
    sub.add_argument("--no-trace", action="store_true")


def _run_optimize(args) -> dict:
    from jiuwenswarm.harness_evolve import optimizer

    if args.target <= 0 or args.target > 100:
        raise HarnessUsageError("--target 必须 ∈ (0, 100]")

    async def _inner() -> dict:
        reports = await optimizer.run_optimize(
            args.agent, args.benchmark,
            target_score=args.target,
            rounds=args.rounds,
            runs=args.runs,
            model_name=args.model,
            debug_trace=not args.no_trace,
            parallel=args.parallel,
            data_dir=getattr(args, "data_dir", None),
        )
        return {
            "agent": args.agent,
            "benchmark": args.benchmark,
            "target_score": args.target,
            "rounds": [
                {
                    "round_index": r.round_index,
                    "reference_version": r.reference_version,
                    "candidate_version": r.candidate_version,
                    "proposed_changes": r.proposed_changes,
                    "evaluation_valid": r.evaluation_valid,
                    "matrix_score": r.matrix_score,
                    "reference_score": r.reference_score,
                    "accepted": r.accepted,
                    "rolled_back": r.rolled_back,
                    "reason": r.reason,
                }
                for r in reports
            ],
        }

    payload = asyncio.run(_inner())
    for r in payload["rounds"]:
        status = "接受" if r["accepted"] else ("回滚" if r["rolled_back"] else "-")
        score = r["matrix_score"] if r["matrix_score"] is not None else "-"
        out(
            args,
            f"round {r['round_index']}: {status} matrix={score} "
            f"vs ref={r['reference_score']} | {r['reason']}",
        )
    accepted_any = any(r["accepted"] for r in payload["rounds"])
    out(args, f"结果: {'已接受' if accepted_any else '未接受任何候选'}")
    return payload


def register(registry: dict[str, tuple[Callable, Callable]]) -> None:
    registry["agent-version"] = (_register_agent_version, _run_agent_version)
    registry["snapshot-create"] = (_register_snapshot_create, _run_snapshot_create)
    registry["agent-edit"] = (_register_agent_edit, _run_agent_edit)
    registry["agent-restore"] = (_register_agent_restore, _run_agent_restore)
    registry["baseline-record"] = (_register_baseline_record, _run_baseline_record)
    registry["optimize"] = (_register_optimize, _run_optimize)
