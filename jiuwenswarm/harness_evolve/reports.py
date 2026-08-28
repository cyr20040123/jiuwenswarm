# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Markdown 诊断报告:每 case 得分差距、逐项低点、session/trace/工作区路径。

纯文本渲染,无 IO;由 ``report`` 命令把结果写盘/输出。诊断信息不涉及 rubric
私有内容——逐项分数本身就是 benchmark 作者可见的(scoreboard 已是如此)。
"""

from __future__ import annotations

from jiuwenswarm.harness_evolve.pipeline import CellOutcome
from jiuwenswarm.harness_evolve.schemas import Evaluation


def render_matrix_report(
    agent_name: str,
    benchmark_id: str,
    evaluation: Evaluation | None,
    cells: dict[str, list[CellOutcome]],
    *,
    target_score: float | None = None,
) -> str:
    """渲染 matrix 诊断报告。

    Args:
        agent_name / benchmark_id: 报告头部上下文。
        evaluation: 有效 matrix 的聚合;None 表示 matrix 作废。
        cells: 每 case 的 cell 证据(含失败)。
        target_score: 可选的目标分(报告里给出差距)。

    Returns:
        Markdown 文本。
    """
    lines: list[str] = []
    lines.append(f"# 诊断报告: {benchmark_id} @ {agent_name}")
    lines.append("")
    if evaluation is None:
        lines.append("**matrix 无效**——存在不可打分的 cell(launch 失败 / 版本漂移),")
        lines.append("此轮结果不可接受,也未进入 scoreboard。")
        lines.append("")
        _render_cells(lines, cells, valid=False)
        return "\n".join(lines)

    lines.append(f"- 顶层均分: **{evaluation.score}**"
                 + (f" vs 目标 {target_score}(差距 {round(target_score - evaluation.score, 2)})"
                    if target_score is not None else ""))
    lines.append(f"- 模型: {evaluation.model_id} | 总耗时: {evaluation.duration_ms} ms")
    lines.append(f"- 版本: v{evaluation.version}(agent sidecar) | 时间: {evaluation.time}")
    lines.append("")
    lines.append("## 逐 case 得分")
    lines.append("")
    lines.append("| case | score | 与顶层差距 | runs | 状态 |")
    lines.append("| ---- | ----- | ---------- | ---- | ---- |")
    for case_score in evaluation.cases:
        cells_for_case = cells.get(case_score.case, [])
        failures = [c for c in cells_for_case if c.failure]
        status = f"{len(failures)} 失败" if failures else "ok"
        lines.append(
            f"| {case_score.case} | {case_score.score} | "
            f"{round(evaluation.score - case_score.score, 2)} | "
            f"{len(case_score.runs)} | {status} |"
        )
    lines.append("")
    lines.append("## 逐项诊断")
    lines.append("")
    _render_cells(lines, cells, valid=True)
    lines.append("## 证据")
    lines.append("")
    lines.append("| case | session_id | 输出tokens | trace | 工作区 |")
    lines.append("| ---- | ---------- | --------- | ----- | ------ |")
    for case_id, case_cells in cells.items():
        for cell in case_cells:
            tokens_out = (cell.run.tokens or {}).get("output_tokens", "-")
            lines.append(
                f"| {case_id} | `{cell.run.session_id}` | {tokens_out} | "
                f"`{cell.run.trace_file}` | `{cell.run.workspace}` |"
            )
    lines.append("")
    return "\n".join(lines)


def _render_cells(lines: list[str], cells: dict[str, list[CellOutcome]], *, valid: bool) -> None:
    for case_id, case_cells in cells.items():
        lines.append(f"### {case_id}")
        for cell in case_cells:
            if cell.failure:
                lines.append(f"- run `{cell.run.session_id}`: **不可打分**({cell.failure})")
                continue
            if not valid:
                continue
            low_items = sorted(
                cell.rubric_scores.items(), key=lambda kv: kv[1]
            )[:2]
            low_desc = "; ".join(f"{name}={score}" for name, score in low_items) or "-"
            lines.append(
                f"- run `{cell.run.session_id}`: score={cell.score}(低分项: {low_desc})"
            )
        lines.append("")


__all__ = ["render_matrix_report"]
