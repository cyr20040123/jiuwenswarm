# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""evaluate_matrix_isolated 测试:子进程隔离并发聚合(不真 spawn 子进程)。"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.harness_evolve import benchmark_store, pipeline
from jiuwenswarm.harness_evolve.schemas import Evaluation

RUBRIC = (
    "## Scoring\n"
    "| item | points | description |\n"
    "| ---- | ------ | ----------- |\n"
    "| correctness | 60 | 正确性 |\n"
    "| format | 40 | 格式 |\n"
)


def _register_agent(tmp_path, name="test-agent"):
    from jiuwenswarm.harness_evolve.cli import main

    prompt = tmp_path / "p.md"
    prompt.write_text("你是测试 agent。\n", encoding="utf-8")
    assert main(
        ["agent-register", "--name", name, "--description", "t",
         "--prompt-file", str(prompt), "--json"]
    ) == 0
    return name


def _mk_bench(name, bid="bench-1"):
    benchmark_store.init_benchmark(bid, "T", "D", name)
    benchmark_store.write_case(bid, "CASE-001-a", "任务一", RUBRIC)
    benchmark_store.write_case(bid, "CASE-002-b", "任务二", RUBRIC)


def _payload(case: str, score: float, *, status="ok") -> dict:
    return {
        "case": case, "status": status, "failure_code": None,
        "session_id": f"sess-{case}", "duration_ms": 100, "cost": None,
        "model_id": "fake-model", "final_text": "产物",
        "workspace": "/tmp/ws", "trace_file": None, "exception": None,
        "score": score, "tokens": {"output_tokens": 10},
    }


class TestIsolatedMatrix:
    def test_parallel_aggregation(self, tmp_path, monkeypatch):
        name = _register_agent(tmp_path)
        _mk_bench(name)
        scores = {"CASE-001-a": 80.0, "CASE-002-b": 60.0}

        def fake_cell(cmd):
            case = next(a for a in cmd if a.startswith("CASE-"))
            return _payload(case, scores[case])

        monkeypatch.setattr(pipeline, "_run_cell_subprocess", fake_cell)
        evaluation, cells = asyncio.run(
            pipeline.evaluate_matrix_isolated(name, "bench-1", parallel=3)
        )
        assert evaluation is not None
        assert evaluation.score == 70.0  # (80+60)/2
        assert evaluation.version == 1
        assert set(cells) == {"CASE-001-a", "CASE-002-b"}
        assert cells["CASE-001-a"][0].score == 80.0

    def test_invalid_cell_returns_none(self, tmp_path, monkeypatch):
        name = _register_agent(tmp_path)
        _mk_bench(name)

        def fake_cell(cmd):
            case = next(a for a in cmd if a.startswith("CASE-"))
            if case == "CASE-002-b":
                return _payload(case, None, status="failed")
            return _payload(case, 80.0)

        monkeypatch.setattr(pipeline, "_run_cell_subprocess", fake_cell)
        evaluation, cells = asyncio.run(
            pipeline.evaluate_matrix_isolated(name, "bench-1", parallel=2)
        )
        assert evaluation is None
        assert len(cells) == 2

    def test_parallel_one_delegates_to_sequential(self, tmp_path, monkeypatch):
        name = _register_agent(tmp_path)
        _mk_bench(name)
        called = {}

        async def fake_sequential(*args, **kwargs):
            called.update(kwargs)
            return None, {}

        monkeypatch.setattr(pipeline, "evaluate_matrix", fake_sequential)
        monkeypatch.setattr(
            pipeline, "_run_cell_subprocess",
            lambda cmd: pytest.fail("parallel=1 不应走子进程"),
        )
        result = asyncio.run(
            pipeline.evaluate_matrix_isolated(
                name, "bench-1", parallel=1, runs=2, debug_trace=False,
            )
        )
        assert result == (None, {})
        assert called["runs"] == 2
        assert called["debug_trace"] is False

    def test_build_cell_command_shape(self):
        cmd = pipeline._build_cell_command(
            "agent-x", "bench-1", "CASE-001-a", "MiniMax-M3",
            no_trace=True, data_dir="/tmp/he",
        )
        assert cmd[0].endswith("python")
        assert "-m" in cmd and "jiuwenswarm.harness_evolve.cli" in cmd
        assert "eval-run" in cmd
        assert "CASE-001-a" in cmd
        assert "--model" in cmd and "MiniMax-M3" in cmd
        assert "--no-trace" in cmd
        assert "--data-dir" in cmd and "/tmp/he" in cmd
        assert "--json" in cmd

    def test_payload_to_cell_outcome(self):
        payload = _payload("CASE-001-a", 70.0)
        cell = pipeline._cell_outcome_from_payload(payload)
        assert cell.score == 70.0
        assert cell.run.session_id == "sess-CASE-001-a"
        assert cell.run.tokens == {"output_tokens": 10}


class TestOptimizeCliPassesParallel:
    def test_optimize_passes_parallel(self, tmp_path, monkeypatch, capsys):
        from jiuwenswarm.harness_evolve import optimizer
        from jiuwenswarm.harness_evolve.cli import main

        captured = {}

        async def fake_run_optimize(*args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(optimizer, "run_optimize", fake_run_optimize)
        capsys.readouterr()
        rc = main(
            ["optimize", "--agent", "some-agent", "--benchmark", "bench-1",
             "--target", "95", "--parallel", "3", "--json"]
        )
        assert rc == 0
        assert captured["parallel"] == 3
        assert captured["target_score"] == 95.0
