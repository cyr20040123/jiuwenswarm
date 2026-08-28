# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-optimization 闭环:proposal 生成候选 + optimize 复合编排。

对齐 Penguin agent-optimization 的决策规则(实现简化):
- Reference = scoreboard 末条(其完整 Evaluation 从 scoreboard 读回,用户不复述);
- 运行时一致性:Reference.version 必须等于被测 agent 当前 sidecar 版本,否则停止;
- 每轮:快照(已存在则复用,不覆盖)→ proposal LLM 生成候选 → agent-edit →
  eval-matrix → **接受 = 每 cell 有效且顶层均分严格大于 Reference**;
  被拒 → restore 回退(版本回滚、被拒候选版本不复用),不入 scoreboard;
- 达 target 或轮数上限早停;被拒候选证据保留在 evaluations/ 归档。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from jiuwenswarm.harness_evolve import agent_state, benchmark_store, paths, schemas
from jiuwenswarm.harness_evolve.errors import (
    AgentStateError,
    BenchmarkValidationError,
    GradingFailure,
    HarnessUsageError,
    LaunchFailure,
    ScoreboardError,
    SnapshotError,
)

logger = logging.getLogger(__name__)

_PROPOSAL_TEMPLATE = """你是 agent harness 优化器。基于评估证据改进被测 agent 的 system prompt。

## 被测 agent 当前 prompt
{current_prompt}

## Reference 评估(必须超越它的顶层均分)
{reference_yaml}

## 本轮诊断(逐 case 得分)
{diagnosis}

## 目标
目标均分: {target}

## 输出要求
只输出一个 JSON 对象,不要输出任何其它文字:
{{
  "summary_title": "<候选版本一句话标题>",
  "summary": "<候选版本说明>",
  "prompt": "<改进后的完整 system prompt,直接可作 agent prompt 全文>",
  "changes": ["<改动点 1>", "<改动点 2>"]
}}
"""


@dataclass
class Proposal:
    """LLM 生成的候选定义。"""

    summary_title: str
    summary: str
    prompt: str
    changes: list[str] = field(default_factory=list)


@dataclass
class RoundReport:
    """一轮 optimize 的记录(接受决定与假设支持分离记录)。"""

    round_index: int
    reference_version: int | None = None  # 本轮起点的 Reference 版本
    candidate_version: int | None = None  # agent-edit 后的 sidecar 版本
    proposed_changes: list[str] = field(default_factory=list)
    evaluation_valid: bool = False
    matrix_score: float | None = None
    reference_score: float | None = None
    accepted: bool = False
    rolled_back: bool = False
    reason: str = ""


def _reference(agent_name: str, benchmark_id: str) -> schemas.Evaluation:
    """scoreboard 末条;无条目 -> 不可 optimize(必须先有已接受基线)。"""
    board = benchmark_store.read_scoreboard(benchmark_id)
    if not board.evaluations:
        raise HarnessUsageError(
            "scoreboard 为空,无法 optimize:先基准冻结并接受至少一条评估"
        )
    reference = board.evaluations[-1]
    # Penguin: 运行时一致性——Reference 版本必须是被测 agent 当前版本
    current = agent_state.agent_version(agent_name)
    if reference.version != current:
        raise ScoreboardError(
            f"运行时不一致:scoreboard 末条版本 v{reference.version} "
            f"≠ agent 当前版本 v{current};请先 agent-restore 或重新评估"
        )
    return reference


async def propose_candidate(
    *,
    agent_name: str,
    benchmark_id: str,
    reference: schemas.Evaluation,
    diagnosis: str,
    target_score: float,
    model=None,
) -> Proposal:
    """LLM 生成候选 prompt(JSON 契约,后解析)。"""
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    agent = AgentConfigService().get_agent(agent_name)
    if agent is None:
        raise HarnessUsageError(f"agent 不存在: {agent_name}")
    prompt = _PROPOSAL_TEMPLATE.format(
        current_prompt=agent.prompt,
        reference_yaml=schemas.evaluation_to_yaml(reference),
        diagnosis=diagnosis,
        target=target_score,
    )
    message = await model.invoke(prompt)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return _parse_proposal(str(content or ""))


def _parse_proposal(raw: str) -> Proposal:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HarnessUsageError(f"proposal 输出非 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessUsageError("proposal 顶层必须是对象")
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise HarnessUsageError("proposal 缺少非空 prompt")
    changes_raw = data.get("changes")
    changes = (
        [str(c) for c in changes_raw if str(c).strip()]
        if isinstance(changes_raw, list) else []
    )
    return Proposal(
        summary_title=str(data.get("summary_title") or "candidate"),
        summary=str(data.get("summary") or ""),
        prompt=prompt,
        changes=changes,
    )


def _ensure_snapshot(agent_name: str, version: int) -> None:
    """编辑前快照:已存在则复用(禁覆盖,restore 后 v<N> 仍在),否则创建。"""
    if paths.snapshot_file(agent_name, version).is_file():
        return
    agent_state.create_snapshot(agent_name, version)


def _rollback(agent_name: str, version: int) -> bool:
    """尽力回滚到 Reference 版本;返回是否成功。"""
    try:
        agent_state.restore_snapshot(agent_name, version)
        return True
    except Exception as exc:  # noqa: BLE001 - 回滚失败也要记录并继续
        logger.error("[harness-evolve] 回滚 v%s 失败: %s", version, exc)
        return False


async def run_optimize(
    agent_name: str,
    benchmark_id: str,
    *,
    target_score: float,
    rounds: int = 3,
    runs: int = 1,
    model_name: str | None = None,
    debug_trace: bool = True,
    parallel: int = 1,
    data_dir: str | None = None,
    model=None,  # 测试注入评测模型;None 时走 config 解析
    proposal_model=None,  # 测试注入 proposal 模型;None 复用 model
) -> list[RoundReport]:
    """执行 optimize 闭环,返回每轮报告(接受/回滚均不抛错)。

    决定规则(与 Penguin 一致):接受 = 每 cell 有效(evaluation 非 None)
    且顶层均分**严格大于** Reference;被拒候选不入 scoreboard。
    """
    from jiuwenswarm.harness_evolve import pipeline

    if rounds < 1:
        raise HarnessUsageError("--rounds 必须 >= 1")

    reference = _reference(agent_name, benchmark_id)
    reports: list[RoundReport] = []

    eff_model = pipeline._effective_model_name(agent_name, model_name)
    eval_model = model
    if eval_model is None:
        eval_model, _ = pipeline._resolve_model_cached(eff_model)
    prop_model = proposal_model if proposal_model is not None else eval_model

    for round_index in range(1, rounds + 1):
        # 运行时一致性:每轮开始时 Reference 仍是当前版本(accept 后同步更新,
        # reject/失败后回滚也回到 Reference,故两侧都成立)
        if agent_state.agent_version(agent_name) != reference.version:
            reports.append(RoundReport(
                round_index=round_index,
                reference_version=reference.version,
                reason="运行时版本漂移,停止",
            ))
            break

        report = RoundReport(
            round_index=round_index,
            reference_version=reference.version,
            reference_score=reference.score,
        )
        try:
            _ensure_snapshot(agent_name, reference.version)
            diagnosis = _render_diagnosis(reference)
            proposal = await propose_candidate(
                agent_name=agent_name, benchmark_id=benchmark_id,
                reference=reference, diagnosis=diagnosis,
                target_score=target_score, model=prop_model,
            )
            report.proposed_changes = proposal.changes
            state = agent_state.apply_agent_edit(
                agent_name,
                expected_version=reference.version,
                prompt=proposal.prompt,
            )
            report.candidate_version = state["version"]

            evaluation, cells = await pipeline.evaluate_matrix_isolated(
                agent_name, benchmark_id,
                runs=runs, model_name=eff_model, debug_trace=debug_trace,
                parallel=parallel, data_dir=data_dir,
            )
            report.evaluation_valid = evaluation is not None
            report.matrix_score = evaluation.score if evaluation is not None else None
            pipeline.write_evidence(
                benchmark_id, f"optimize-r{round_index}", evaluation, cells
            )

            accepted = (
                evaluation is not None
                and evaluation.score > reference.score
            )
            if accepted:
                benchmark_store.append_evaluation(benchmark_id, evaluation)
                report.accepted = True
                report.reason = (
                    f"接受: {evaluation.score} > {report.reference_score}"
                )
                reference = evaluation  # Reference 前移,下一轮以此为基线
                logger.info(
                    "[harness-evolve] round %d 接受 v%d", round_index, evaluation.version
                )
            else:
                report.rolled_back = _rollback(agent_name, report.reference_version)
                report.reason = (
                    f"拒绝: {evaluation.score if evaluation else 'matrix 无效'} "
                    f"未严格大于 {report.reference_score};已回滚"
                )
                logger.info(
                    "[harness-evolve] round %d 拒绝并回滚", round_index
                )
        except (LaunchFailure, GradingFailure) as exc:
            # 评测层失败(launcher/grader):候选不可打分,回滚后继续下一轮
            report.rolled_back = _rollback(agent_name, report.reference_version)
            report.reason = f"评测失败: {exc};已回滚"
            reports.append(report)
            continue
        except (
            AgentStateError,
            ScoreboardError,
            BenchmarkValidationError,
            SnapshotError,
            HarnessUsageError,
        ) as exc:
            # 状态/基准错误(版本漂移、benchmark 失效、proposal 契约坏):
            # 按 Penguin "运行时不一致→矩阵作废停止",回滚后停止
            report.rolled_back = _rollback(agent_name, report.reference_version)
            report.reason = f"失败: {exc};已回滚,停止"
            reports.append(report)
            break
        reports.append(report)

        if report.accepted and reference.score >= target_score:
            report.reason += ";已达目标分,早停"
            break
    return reports


def _render_diagnosis(reference: schemas.Evaluation) -> str:
    """从 Reference 的完整 Evaluation 生成诊断文本(逐 case 得分)。"""
    lines = [f"Reference: v{reference.version} score={reference.score}"]
    for case_score in reference.cases:
        lines.append(
            f"- {case_score.case}: score={case_score.score} "
            f"runs={len(case_score.runs)}"
        )
    return "\n".join(lines)


__all__ = [
    "Proposal",
    "RoundReport",
    "propose_candidate",
    "run_optimize",
    "_PROPOSAL_TEMPLATE",
]
