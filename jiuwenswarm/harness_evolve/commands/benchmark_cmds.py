# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""benchmark-design 流水线命令。

benchmark-init / case-write / benchmark-validate / leak-check / benchmark-freeze /
scoreboard-append / scoreboard-read / benchmark-export / benchmark-import。
"""

from __future__ import annotations

import argparse
from typing import Callable

import yaml

from jiuwenswarm.harness_evolve import agent_state, benchmark_store
from jiuwenswarm.harness_evolve.commands import out
from jiuwenswarm.harness_evolve.errors import HarnessUsageError
from jiuwenswarm.harness_evolve.schemas import (
    evaluation_from_dict,
    scoreboard_to_yaml,
)


def _require_agent_exists(name: str) -> None:
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    if AgentConfigService().get_agent(name) is None:
        raise HarnessUsageError(f"test_agent 不存在: {name}(先 agent-register)")


def _register_benchmark_init(sub: argparse.ArgumentParser) -> None:
    """benchmark-init:创建 benchmark 目录树(config + 空 scoreboard)。"""
    sub.add_argument("--benchmark", required=True, help="benchmark id([a-z0-9-]{3,64})")
    sub.add_argument("--agent", required=True, help="被测 agent 名")
    sub.add_argument("--title", required=True)
    sub.add_argument("--description", required=True)


def _run_benchmark_init(args) -> dict:
    _require_agent_exists(args.agent)
    bid_dir = benchmark_store.init_benchmark(
        args.benchmark, args.title, args.description, args.agent
    )
    out(args, f"benchmark '{args.benchmark}' 已创建: {bid_dir}(test_agent={args.agent})")
    return {"benchmark": args.benchmark, "test_agent": args.agent, "dir": str(bid_dir)}


def _register_case_write(sub: argparse.ArgumentParser) -> None:
    """case-write:写入一个 case(statement 公开 / rubric 私有,总和恰 100)。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--case", required=True, help="CASE-<nnn>-<semantic>")
    sub.add_argument("--statement", required=True, help="statement/README.md 文件")
    sub.add_argument("--rubric", required=True, help="rubric/README.md 文件(## Scoring 表和 == 100)")


def _run_case_write(args) -> dict:
    from pathlib import Path

    statement_path = Path(args.statement)
    rubric_path = Path(args.rubric)
    if not statement_path.is_file():
        raise HarnessUsageError(f"statement 文件不存在: {statement_path}")
    if not rubric_path.is_file():
        raise HarnessUsageError(f"rubric 文件不存在: {rubric_path}")
    case_dir = benchmark_store.write_case(
        args.benchmark,
        args.case,
        statement_path.read_text(encoding="utf-8"),
        rubric_path.read_text(encoding="utf-8"),
    )
    out(args, f"case '{args.case}' 已写入: {case_dir}")
    return {"benchmark": args.benchmark, "case": args.case, "dir": str(case_dir)}


def _register_benchmark_validate(sub: argparse.ArgumentParser) -> None:
    """benchmark-validate:校验 benchmark 完整性(缺陷清单;空 = 可冻结)。"""
    sub.add_argument("--benchmark", required=True)


def _run_benchmark_validate(args) -> dict:
    defects = benchmark_store.validate_benchmark(args.benchmark)
    if defects:
        for d in defects:
            out(args, f"- {d}")
        out(args, f"缺陷 {len(defects)} 项")
        return {"ok": False, "defects": defects}
    out(args, "校验通过,可冻结")
    return {"ok": True, "defects": []}


def _register_leak_check(sub: argparse.ArgumentParser) -> None:
    """leak-check:扫描 statement 是否泄露 rubric 内容。"""
    sub.add_argument("--benchmark", required=True)


def _run_leak_check(args) -> dict:
    issues = benchmark_store.leak_check(args.benchmark)
    if issues:
        for i in issues:
            out(args, f"- {i}")
        raise HarnessUsageError(f"发现 {len(issues)} 处泄露")
    out(args, "未发现泄露")
    return {"ok": True, "issues": []}


def _register_benchmark_freeze(sub: argparse.ArgumentParser) -> None:
    """benchmark-freeze:校验通过后冻结;可选记录 agent 基线版本。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--baseline-version", type=int, default=None,
                     help="冻结时的 agent 版本(写入被测 agent 的 sidecar)")


def _run_benchmark_freeze(args) -> dict:
    benchmark_store.freeze_benchmark(args.benchmark)
    baseline = None
    if args.baseline_version is not None:
        cfg = benchmark_store.read_config(args.benchmark)
        _require_agent_exists(cfg.test_agent)
        current = agent_state.agent_version(cfg.test_agent)
        if args.baseline_version != current:
            raise HarnessUsageError(
                f"基线版本 {args.baseline_version} 与 agent 当前版本 {current} 不一致"
            )
        state = agent_state.record_baseline(cfg.test_agent, args.baseline_version)
        baseline = state["baseline_version"]
    out(args, f"benchmark '{args.benchmark}' 已冻结"
        + (f"(baseline_version={baseline})" if baseline else ""))
    return {"benchmark": args.benchmark, "frozen": True, "baseline_version": baseline}


def _register_scoreboard_append(sub: argparse.ArgumentParser) -> None:
    """scoreboard-append:追加一条已接受评估(frozen + 版本单调 + 原子写)。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--file", required=True, help="Evaluation YAML 文件(eval-matrix 产物)")


def _run_scoreboard_append(args) -> dict:
    from pathlib import Path

    ev_path = Path(args.file)
    if not ev_path.is_file():
        raise HarnessUsageError(f"评估文件不存在: {ev_path}")
    try:
        raw = yaml.safe_load(ev_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HarnessUsageError(f"评估 YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise HarnessUsageError("评估文件必须是单个 Evaluation 对象")
    evaluation = evaluation_from_dict(raw)
    board = benchmark_store.append_evaluation(args.benchmark, evaluation)
    last = board.evaluations[-1]
    out(
        args,
        f"已追加 v{last.version}: score={last.score} "
        f"cost={last.cost} duration_ms={last.duration_ms}",
    )
    return {
        "benchmark": args.benchmark,
        "appended_version": last.version,
        "score": last.score,
        "total_evaluations": len(board.evaluations),
    }


def _register_scoreboard_read(sub: argparse.ArgumentParser) -> None:
    """scoreboard-read:查看 scoreboard(全部或指定版本)。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--version", type=int, default=None, help="只看某版本")


def _run_scoreboard_read(args) -> dict:
    board = benchmark_store.read_scoreboard(args.benchmark)
    if args.version is not None:
        evs = [e for e in board.evaluations if e.version == args.version]
        if not evs:
            raise HarnessUsageError(
                f"scoreboard 无版本 {args.version}(现有: "
                f"{[e.version for e in board.evaluations]})"
            )
        board.evaluations = evs
    out(args, scoreboard_to_yaml(board).rstrip("\n"))
    return {
        "benchmark": args.benchmark,
        "evaluations": [
            {
                "version": e.version,
                "time": e.time,
                "score": e.score,
                "model_id": e.model_id,
                "cases": len(e.cases),
            }
            for e in board.evaluations
        ],
    }


def _register_benchmark_export(sub: argparse.ArgumentParser) -> None:
    """benchmark-export:导出 benchmark 为 zip。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--out", required=True, help="输出 zip 路径(无需 .zip 后缀)")


def _run_benchmark_export(args) -> dict:
    zip_path = benchmark_store.export_benchmark(args.benchmark, args.out)
    out(args, f"已导出: {zip_path}")
    return {"benchmark": args.benchmark, "zip": str(out)}


def _register_benchmark_import(sub: argparse.ArgumentParser) -> None:
    """benchmark-import:从 zip 导入 benchmark。"""
    sub.add_argument("--file", required=True, help="zip 路径")
    sub.add_argument("--as", dest="new_id", default=None, help="改名导入")


def _run_benchmark_import(args) -> dict:
    bid = benchmark_store.import_benchmark(args.file, args.new_id)
    out(args, f"已导入 benchmark: {bid}")
    return {"benchmark": bid}


def _register_benchmark_list(sub: argparse.ArgumentParser) -> None:
    """benchmark-list:列出全部 benchmark(版本/冻结/被测 agent)。"""


def _run_benchmark_list(args) -> dict:
    rows = []
    for bid in benchmark_store.list_benchmarks():
        cfg = benchmark_store.read_config(bid)
        state = "frozen" if cfg.frozen else "editing"
        parent = f" <- {cfg.parent}" if cfg.parent else ""
        out(
            args,
            f"{bid:<28} v{cfg.version} {state:<8} {cfg.test_agent:<24} "
            f"{cfg.title}{parent}",
        )
        rows.append(
            {
                "benchmark": bid,
                "title": cfg.title,
                "test_agent": cfg.test_agent,
                "frozen": cfg.frozen,
                "version": cfg.version,
                "parent": cfg.parent,
            }
        )
    if not rows:
        out(args, "(无 benchmark)")
    return {"benchmarks": rows}


def _register_benchmark_evolve(sub: argparse.ArgumentParser) -> None:
    """benchmark-evolve:版本化复制(旧版只读保留,新版解冻可增删改)。"""
    sub.add_argument("--benchmark", required=True, help="已冻结的源 benchmark")
    sub.add_argument("--as", dest="as_id", default=None,
                     help="新版 id(默认 <原id>-v<N>)")
    sub.add_argument("--title", default=None, help="覆盖继承的标题")
    sub.add_argument("--description", default=None, help="覆盖继承的描述")


def _run_benchmark_evolve(args) -> dict:
    result = benchmark_store.evolve_benchmark(
        args.benchmark, new_id=args.as_id,
        title=args.title, description=args.description,
    )
    out(
        args,
        f"已创建新版 benchmark '{result['benchmark']}' "
        f"(v{result['version']},继承 {result['parent']} 的 {len(result['cases'])} 个 case)",
    )
    out(args, "  新版未冻结:case-write / case-delete 增删改 → pilot → "
              "benchmark-freeze → baseline-record")
    return result


def _register_case_delete(sub: argparse.ArgumentParser) -> None:
    """case-delete:删除 case(仅限未冻结 benchmark)。"""
    sub.add_argument("--benchmark", required=True)
    sub.add_argument("--case", required=True, help="CASE-<nnn>-<semantic>")


def _run_case_delete(args) -> dict:
    benchmark_store.delete_case(args.benchmark, args.case)
    out(args, f"case '{args.case}' 已从 {args.benchmark} 删除")
    return {"benchmark": args.benchmark, "case": args.case, "deleted": True}


def register(registry: dict[str, tuple[Callable, Callable]]) -> None:
    registry["benchmark-init"] = (_register_benchmark_init, _run_benchmark_init)
    registry["case-write"] = (_register_case_write, _run_case_write)
    registry["benchmark-validate"] = (
        _register_benchmark_validate, _run_benchmark_validate
    )
    registry["leak-check"] = (_register_leak_check, _run_leak_check)
    registry["benchmark-freeze"] = (
        _register_benchmark_freeze, _run_benchmark_freeze
    )
    registry["scoreboard-append"] = (_register_scoreboard_append, _run_scoreboard_append)
    registry["scoreboard-read"] = (_register_scoreboard_read, _run_scoreboard_read)
    registry["benchmark-export"] = (_register_benchmark_export, _run_benchmark_export)
    registry["benchmark-import"] = (_register_benchmark_import, _run_benchmark_import)
    registry["benchmark-list"] = (_register_benchmark_list, _run_benchmark_list)
    registry["benchmark-evolve"] = (_register_benchmark_evolve, _run_benchmark_evolve)
    registry["case-delete"] = (_register_case_delete, _run_case_delete)
