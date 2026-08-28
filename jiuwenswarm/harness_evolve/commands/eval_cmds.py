# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-evaluation 命令:eval-run / eval-matrix / report。

eval-run:单 case 单 run(--json 输出 RunResult+score),支持 pilot 校准。
eval-matrix:全 case × runs 聚合为 Evaluation 并写证据归档(不入 scoreboard);
    --parallel P>1 时按 cell 子进程隔离**并发**(ThreadPoolExecutor;openjiuwen
    全局注册表是进程级单例,并发必须隔进程)。
report:把最近一次 matrix 的证据渲染成 Markdown 诊断报告。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Callable

from jiuwenswarm.harness_evolve import benchmark_store, paths
from jiuwenswarm.harness_evolve.commands import out
from jiuwenswarm.harness_evolve.errors import HarnessUsageError

logger = logging.getLogger(__name__)


def _register_eval_run(sub: argparse.ArgumentParser) -> None:
    """eval-run:单 case 单 run 评测(不要求 frozen,支持 pilot)。"""
    sub.add_argument("--agent", required=True, help="被测 agent 名")
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--case", required=True, help="CASE-<nnn>-<semantic>")
    sub.add_argument("--model", default=None, help="模型名(缺省用 frontmatter/首个 default)")
    sub.add_argument("--no-trace", action="store_true", help="关闭 trace 落盘")


def _run_eval_run(args) -> dict:
    """同步包装:asyncio.run 跑单 cell(deep_agent 是 async 接口)。"""
    from jiuwenswarm.harness_evolve import evaluator, schemas
    from jiuwenswarm.harness_evolve.grader import GradingFailure, grade_run
    from jiuwenswarm.harness_evolve.pipeline import _effective_model_name, _resolve_model_cached

    eff_model = _effective_model_name(args.agent, args.model)

    async def _inner() -> dict:
        run = await evaluator.run_single_case(
            args.agent, args.benchmark, args.case, 1,
            model_name=eff_model, debug_trace=not args.no_trace,
        )
        base = {
            "agent": args.agent,
            "benchmark": args.benchmark,
            "case": args.case,
            "status": run.status,
            "failure_code": run.failure_code,
            "session_id": run.session_id,
            "duration_ms": run.duration_ms,
            "cost": run.cost,
            "model_id": eff_model,
            "final_text": run.final_text[:2000],
            "workspace": str(run.workspace),
            "trace_file": str(run.trace_file) if run.trace_file else None,
            "exception": run.exception,
        }
        if run.status != "ok":
            out(args, f"run 失败: {run.failure_code}: {run.exception}")
            return base
        try:
            # grade_run 期望解析后的 points dict;直接传 raw rubric 文本会在
            # 打分阶段 AttributeError(str 无 items)。与 pipeline.evaluate_case
            # 同款先解析(rubric 已在 case-write 时校验过和 == 100)。
            rubric_points = schemas.parse_rubric_points(
                benchmark_store.read_rubric(args.benchmark, args.case)
            )
            statement = benchmark_store.read_statement(args.benchmark, args.case)
            model, model_id = _resolve_model_cached(eff_model)
            outcome = await grade_run(
                rubric_points, statement, run.workspace, run.trace_file,
                model=model, model_id=model_id, run_stats=run.tokens,
            )
        except GradingFailure as exc:
            base["status"] = "failed"
            base["failure_code"] = "grading_failed"
            base["exception"] = str(exc)
            out(args, f"打分失败: {exc}")
            return base
        base["score"] = outcome.total
        base["items"] = outcome.items
        base["model_id"] = outcome.model_id
        base["tokens"] = run.tokens
        out(args, f"score={outcome.total} ({outcome.items})")
        if run.tokens:
            out(args, f"  tokens: {run.tokens}")
        return base

    return asyncio.run(_inner())


def _register_eval_matrix(sub: argparse.ArgumentParser) -> None:
    """eval-matrix:全 case × runs 聚合;不写 scoreboard,只写证据归档。"""
    sub.add_argument("--agent", required=True, help="被测 agent 名")
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--runs", type=int, default=1, help="每 case 的 run 数(默认 1)")
    sub.add_argument("--parallel", type=int, default=2,
                     help="并行度(默认 2=子进程并发;1=顺序)")
    sub.add_argument("--model", default=None)
    sub.add_argument("--no-trace", action="store_true")
    sub.add_argument("--tag", default=None, help="证据归档文件名(evaluations/<tag>.yaml)")


def _run_eval_matrix(args) -> dict:
    if args.runs < 1:
        raise HarnessUsageError("--runs 必须 >= 1")
    if args.parallel < 1:
        raise HarnessUsageError("--parallel 必须 >= 1")
    from jiuwenswarm.harness_evolve import pipeline

    async def _inner() -> dict:
        evaluation, cells = await pipeline.evaluate_matrix_isolated(
            args.agent, args.benchmark,
            runs=args.runs, model_name=args.model,
            debug_trace=not args.no_trace, parallel=args.parallel,
            data_dir=getattr(args, "data_dir", None),
        )
        if evaluation is None:
            out(args, "matrix 无效(存在不可打分的 cell),未生成 Evaluation")
        else:
            out(args, f"matrix 均分: {evaluation.score}({len(evaluation.cases)} cases)")
        return _matrix_payload(args, evaluation, cells)

    return asyncio.run(_inner())


def _matrix_payload(args, evaluation, cells) -> dict:
    tag = args.tag or f"matrix-{_now_slug()}"
    from jiuwenswarm.harness_evolve import pipeline

    evidence = pipeline.write_evidence(args.benchmark, tag, evaluation, cells)
    payload = {
        "agent": args.agent,
        "benchmark": args.benchmark,
        "tag": tag,
        "valid": evaluation is not None,
        "evidence": str(evidence),
        "evaluation": None,
    }
    if evaluation is not None:
        payload["evaluation"] = {
            "score": evaluation.score,
            "version": evaluation.version,
            "model_id": evaluation.model_id,
            "cases": [
                {"case": cs.case, "score": cs.score, "runs": len(cs.runs)}
                for cs in evaluation.cases
            ],
        }
    out(args, f"证据已归档: {evidence}")
    return payload


def _now_slug() -> str:
    import time

    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _register_report(sub: argparse.ArgumentParser) -> None:
    """report:渲染最近一次 matrix 证据为 Markdown 诊断报告。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--agent", required=True)
    sub.add_argument("--tag", default=None, help="读指定证据归档(默认最新)")
    sub.add_argument("--target", type=float, default=None, help="目标分(报告差距)")


def _run_report(args) -> dict:
    from jiuwenswarm.harness_evolve import pipeline, reports, schemas

    ev_dir = paths.benchmark_evaluations_dir(args.benchmark)
    if not ev_dir.is_dir():
        raise HarnessUsageError(f"无任何证据归档: benchmark={args.benchmark}")
    candidates = sorted(ev_dir.glob("*.yaml"), key=lambda p: p.stat().st_mtime)
    target = (
        ev_dir / f"{args.tag}.yaml"
        if args.tag else (candidates[-1] if candidates else None)
    )
    if target is None or not target.is_file():
        raise HarnessUsageError(f"证据归档不存在: {target}")

    text = target.read_text(encoding="utf-8")
    evaluation = None
    try:
        raw = __import__("yaml").safe_load(text)
        if raw and raw.get("valid") is not False and "evaluations" not in raw:
            evaluation = schemas.evaluation_from_dict(raw)
    except Exception as exc:
        raise HarnessUsageError(f"证据归档解析失败: {exc}") from exc

    cells = _cells_from_evidence(target, evaluation)
    report = reports.render_matrix_report(
        args.agent, args.benchmark, evaluation, cells, target_score=args.target
    )
    out(args, report)
    return {"benchmark": args.benchmark, "source": str(target), "valid": evaluation is not None}


def _cells_from_evidence(evidence_path: Path, evaluation) -> dict:
    """从证据 YAML 还原 CellOutcome 视图(报告用;无 run 级证据时给空 cells)。"""
    import yaml

    from jiuwenswarm.harness_evolve.evaluator import RunResult

    try:
        raw = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    cells: dict[str, list] = {}
    if isinstance(raw.get("cells"), dict):
        for case_id, cell_list in raw["cells"].items():
            cells[case_id] = [
                pipeline.CellOutcome(
                    run=RunResult(
                        status=str(c.get("status", "ok")),
                        failure_code=c.get("failure"),
                        session_id=str(c.get("session_id", "")),
                        workspace=Path(str(c.get("workspace", ""))),
                        trace_file=None,
                        final_text="",
                        duration_ms=int(c.get("duration_ms", 0)),
                    ),
                    score=c.get("score"),
                    failure=c.get("failure"),
                )
                for c in cell_list
            ]
    if evaluation is not None and not cells:
        for case_score in evaluation.cases:
            cells[case_score.case] = [
                pipeline.CellOutcome(
                    run=RunResult(
                        status="ok", failure_code=None,
                        session_id=r.session_id, workspace=Path(""),
                        trace_file=None, final_text="", duration_ms=r.duration_ms,
                    ),
                    score=r.score,
                )
                for r in case_score.runs
            ]
    return cells


def register(registry: dict[str, tuple[Callable, Callable]]) -> None:
    registry["eval-run"] = (_register_eval_run, _run_eval_run)
    registry["eval-matrix"] = (_register_eval_matrix, _run_eval_matrix)
    registry["report"] = (_register_report, _run_report)
