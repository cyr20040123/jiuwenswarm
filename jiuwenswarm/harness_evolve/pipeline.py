# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""评测编排(无状态):单 case 多 run 聚合、matrix 聚合为 Evaluation。

- ``evaluate_case``:一个 case 跑 runs 次,每次打分,聚合为 CaseScore。
- ``evaluate_matrix``:全部 case × runs,聚合为 Evaluation(**不写 scoreboard**,
  由调用方决定接受与否;scoreboard-append 才做 frozen+单调校验)。
- 失败 run(launch_failed/version_changed)按 Penguin 语义不入聚合——matrix
  中任一 cell 无效,顶层 ``valid=False``,不可接受。
- cost 为 null 时聚合仍有效(允许 null);duration_ms 为非负整数。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from jiuwenswarm.harness_evolve import agent_state, benchmark_store, paths, schemas
from jiuwenswarm.harness_evolve.errors import BenchmarkValidationError, GradingFailure
from jiuwenswarm.harness_evolve.evaluator import RunResult, run_single_case
from jiuwenswarm.harness_evolve.grader import grade_run

logger = logging.getLogger(__name__)


@dataclass
class CellOutcome:
    """一个 case × run 的完整证据(评估 + 打分)。"""

    run: RunResult
    score: float | None = None  # None = 不可打分(launch 失败等)
    rubric_scores: dict[str, float] = field(default_factory=dict)
    failure: str | None = None


def _effective_model_name(agent_name: str, model_name: str | None) -> str | None:
    """CLI/参数 model > frontmatter model(与 run_single_case 同一优先级)。"""
    if model_name:
        return model_name
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    agent = AgentConfigService().get_agent(agent_name)
    return agent.model if agent is not None and agent.model else None


async def evaluate_case(
    agent_name: str,
    benchmark_id: str,
    case_id: str,
    *,
    runs: int = 1,
    model_name: str | None = None,
    debug_trace: bool = True,
) -> tuple[schemas.CaseScore | None, list[CellOutcome]]:
    """一个 case 跑 ``runs`` 次并打分。

    Returns:
        (CaseScore, cells)。任一 run 不可打分 -> CaseScore 为 None(该 case 无效);
        cells 保留全部证据(含失败 run)供报告/审计。
    """
    benchmark_store.read_config(benchmark_id)
    rubric_points = schemas.parse_rubric_points(
        benchmark_store.read_rubric(benchmark_id, case_id)
    )
    statement = benchmark_store.read_statement(benchmark_id, case_id)
    eff_model = _effective_model_name(agent_name, model_name)
    model, model_id = _resolve_model_cached(eff_model)

    cells: list[CellOutcome] = []
    for run_index in range(1, runs + 1):
        run = await run_single_case(
            agent_name, benchmark_id, case_id, run_index,
            model_name=eff_model, debug_trace=debug_trace,
        )
        if run.status != "ok":
            cells.append(CellOutcome(run=run, failure=run.failure_code or "failed"))
            continue
        try:
            outcome = await grade_run(
                rubric_points, statement, run.workspace, run.trace_file,
                model=model, model_id=model_id, run_stats=run.tokens,
            )
            cells.append(
                CellOutcome(
                    run=run,
                    score=outcome.total,
                    rubric_scores=outcome.items,
                    failure=None,
                )
            )
        except GradingFailure as exc:
            cells.append(CellOutcome(run=run, failure=f"grading_failed: {exc}"))

    if not cells or any(c.score is None for c in cells):
        return None, cells
    return _case_score_from_cells(case_id, cells), cells


def _case_score_from_cells(case_id: str, cells: list[CellOutcome]) -> schemas.CaseScore:
    """有效 cells -> CaseScore(score=run 均分,duration=累计)。"""
    valid = [c for c in cells if c.score is not None]
    return schemas.CaseScore(
        case=case_id,
        score=round(sum(c.score for c in valid) / len(valid), 2),
        cost=None,
        duration_ms=sum(c.run.duration_ms for c in valid),
        runs=[
            schemas.RunScore(
                score=c.score,
                cost=c.run.cost,
                duration_ms=c.run.duration_ms,
                session_id=c.run.session_id,
            )
            for c in valid
        ],
    )


async def evaluate_matrix(
    agent_name: str,
    benchmark_id: str,
    *,
    runs: int = 1,
    model_name: str | None = None,
    debug_trace: bool = True,
) -> tuple[schemas.Evaluation | None, dict[str, list[CellOutcome]]]:
    """全 case × runs 聚合为 Evaluation;任一 cell 无效则整体 None。

    Returns:
        (Evaluation, {case_id: [CellOutcome, ...]})。Evaluation 不含 scoreboard
        版本语义(version 由 scoreboard-append 时生成),time 为当前 UTC。
    """
    config = benchmark_store.read_config(benchmark_id)
    cases = benchmark_store.list_cases(benchmark_id)
    if not cases:
        raise BenchmarkValidationError(f"benchmark 无任何 case: {benchmark_id}")

    all_cells: dict[str, list[CellOutcome]] = {}
    case_scores: list[schemas.CaseScore] = []
    for case_id in cases:
        case_score, cells = await evaluate_case(
            agent_name, benchmark_id, case_id,
            runs=runs, model_name=model_name, debug_trace=debug_trace,
        )
        all_cells[case_id] = cells
        if case_score is None:
            logger.warning(
                "[harness-evolve] case %s 存在无效 cell,matrix 作废", case_id
            )
            return None, all_cells
        case_scores.append(case_score)

    eff_model = _effective_model_name(agent_name, model_name)
    evaluation = _evaluation_from_cells(
        benchmark_id, agent_name, all_cells,
        model_id=_resolve_model_cached(eff_model)[1],
        summary="pipeline.evaluate_matrix 聚合(未入 scoreboard)",
    )
    return evaluation, all_cells


def _evaluation_from_cells(
    benchmark_id: str,
    agent_name: str,
    cells_by_case: dict[str, list[CellOutcome]],
    model_id: str,
    *,
    summary: str,
) -> schemas.Evaluation:
    """有效 cells 聚合为 Evaluation(顺序/子进程两条路径共用)。"""
    case_scores = [
        _case_score_from_cells(case_id, cell_list)
        for case_id, cell_list in cells_by_case.items()
    ]
    return schemas.Evaluation(
        time=schemas.now_utc_iso(),
        version=agent_state.agent_version(agent_name),  # scoreboard 引用的是被测 agent 版本
        model_id=model_id,
        summary_title=f"{benchmark_id} matrix",
        summary=summary,
        score=round(sum(cs.score for cs in case_scores) / len(case_scores), 2),
        cost=None,
        duration_ms=sum(cs.duration_ms for cs in case_scores),
        cases=case_scores,
    )


# ── 子进程隔离并发(openjiuwen 全局注册表进程级单例,并发须隔进程) ──────────


def _build_cell_command(
    agent_name: str,
    benchmark_id: str,
    case_id: str,
    model_name: str | None,
    no_trace: bool,
    data_dir: str | None,
) -> list[str]:
    cmd = [
        sys.executable, "-m", "jiuwenswarm.harness_evolve.cli",
        "eval-run", "--agent", agent_name, "--benchmark", benchmark_id,
        "--case", case_id, "--json",
    ]
    if model_name:
        cmd += ["--model", model_name]
    if no_trace:
        cmd += ["--no-trace"]
    if data_dir:
        cmd += ["--data-dir", str(data_dir)]
    return cmd


def _run_cell_subprocess(cmd: list[str]) -> dict:
    """跑单个 cell 子进程,返回其 --json payload(容忍启动日志混入 stdout)。"""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchmarkValidationError(
            f"cell 子进程失败({proc.returncode}): {proc.stderr[-500:]}"
        )
    out_text = proc.stdout or ""
    idx = out_text.find("{")
    if idx < 0:
        raise BenchmarkValidationError(
            f"cell 子进程输出非 JSON: {out_text[-500:]}"
        )
    return json.loads(out_text[idx:])


def _cell_outcome_from_payload(payload: dict) -> CellOutcome:
    return CellOutcome(
        run=RunResult(
            status=payload.get("status", "failed"),
            failure_code=payload.get("failure_code"),
            session_id=payload.get("session_id", ""),
            workspace=Path(payload.get("workspace", "")),
            trace_file=Path(payload["trace_file"]) if payload.get("trace_file") else None,
            final_text=payload.get("final_text", ""),
            duration_ms=int(payload.get("duration_ms", 0)),
            tokens=payload.get("tokens"),
            exception=payload.get("exception"),
        ),
        score=payload.get("score"),
        failure=payload.get("failure_code"),
    )


async def evaluate_matrix_isolated(
    agent_name: str,
    benchmark_id: str,
    *,
    runs: int = 1,
    model_name: str | None = None,
    debug_trace: bool = True,
    parallel: int = 1,
    data_dir: str | None = None,
) -> tuple[schemas.Evaluation | None, dict[str, list[CellOutcome]]]:
    """matrix 聚合统一入口。

    ``parallel <= 1``:进程内顺序(与 evaluate_matrix 完全一致);
    ``parallel > 1``:每 cell 一个独立子进程**并发**执行(ThreadPoolExecutor),
    父进程收集 --json 结果后按相同规则聚合——任一 cell 无效则整体 None。
    """
    if parallel <= 1:
        return await evaluate_matrix(
            agent_name, benchmark_id,
            runs=runs, model_name=model_name, debug_trace=debug_trace,
        )
    cases = benchmark_store.list_cases(benchmark_id)
    if not cases:
        raise BenchmarkValidationError(f"benchmark 无任何 case: {benchmark_id}")
    commands = [
        _build_cell_command(
            agent_name, benchmark_id, case_id, model_name,
            not debug_trace, data_dir,
        )
        for case_id in cases
        for run_index in range(1, runs + 1)
    ]
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        payloads = await asyncio.gather(
            *(
                loop.run_in_executor(pool, _run_cell_subprocess, cmd)
                for cmd in commands
            )
        )

    cells_by_case: dict[str, list[CellOutcome]] = {}
    for payload in payloads:
        cells_by_case.setdefault(payload["case"], []).append(
            _cell_outcome_from_payload(payload)
        )
    if any(p["status"] != "ok" or p.get("score") is None for p in payloads):
        return None, cells_by_case
    evaluation = _evaluation_from_cells(
        benchmark_id, agent_name, cells_by_case,
        model_id=payloads[0].get("model_id") or "",
        summary="pipeline.evaluate_matrix_isolated 子进程并发聚合(未入 scoreboard)",
    )
    return evaluation, cells_by_case


_model_cache: dict[tuple[str | None, str], tuple] = {}


def _resolve_model_cached(model_name: str | None) -> tuple:
    """同一 matrix 内共享同一 Model 实例(避免重复构造)。"""
    key = ("evaluator", model_name or "")
    if key not in _model_cache:
        from jiuwenswarm.harness_evolve.evaluator import _resolve_model

        _model_cache[key] = _resolve_model(model_name)
    return _model_cache[key]


def write_evidence(
    benchmark_id: str, tag: str, evaluation: schemas.Evaluation | None,
    cells: dict[str, list[CellOutcome]],
) -> Path | None:
    """把 matrix 证据归档到 ``benchmarks/<id>/evaluations/``(被拒候选也留)。"""
    from jiuwenswarm.harness_evolve.schemas import evaluation_to_yaml

    ev_dir = paths.benchmark_evaluations_dir(benchmark_id)
    ev_dir.mkdir(parents=True, exist_ok=True)
    target = ev_dir / f"{tag}.yaml"
    if evaluation is None:
        # 作废的 matrix:记录失败原因概要,便于诊断
        payload = {
            "tag": tag,
            "valid": False,
            "cells": {
                case_id: [
                    {
                        "session_id": c.run.session_id,
                        "status": c.run.status,
                        "failure": c.failure,
                        "score": c.score,
                        "workspace": str(c.run.workspace),
                        "duration_ms": c.run.duration_ms,
                    }
                    for c in cells
                ]
                for case_id, cells in cells.items()
            },
        }
        import yaml

        target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return target
    target.write_text(evaluation_to_yaml(evaluation), encoding="utf-8")
    return target


__all__ = [
    "CellOutcome",
    "evaluate_case",
    "evaluate_matrix",
    "evaluate_matrix_isolated",
    "write_evidence",
]
