# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""benchmark 版本化演化测试:benchmark-evolve / benchmark-list / case-delete。"""

from __future__ import annotations

import pytest

from jiuwenswarm.harness_evolve import benchmark_store, paths
from jiuwenswarm.harness_evolve.errors import BenchmarkValidationError

RUBRIC = (
    "## Scoring\n"
    "| item | points | description |\n"
    "| ---- | ------ | ----------- |\n"
    "| correctness | 60 | 正确性 |\n"
    "| format | 40 | 格式 |\n"
)


def _make_frozen_bench(bid="bench-1", *, title="T", desc="D") -> None:
    benchmark_store.init_benchmark(bid, title, desc, "some-agent")
    benchmark_store.write_case(bid, "CASE-001-first", "任务一", RUBRIC)
    benchmark_store.write_case(bid, "CASE-002-second", "任务二", RUBRIC)
    benchmark_store.freeze_benchmark(bid)


class TestEvolve:
    def test_evolve_requires_frozen(self, tmp_path):
        benchmark_store.init_benchmark("bench-1", "T", "D", "some-agent")
        with pytest.raises(BenchmarkValidationError, match="未冻结"):
            benchmark_store.evolve_benchmark("bench-1")

    def test_evolve_default_naming_and_fields(self, tmp_path):
        _make_frozen_bench()
        result = benchmark_store.evolve_benchmark("bench-1")
        assert result["benchmark"] == "bench-1-v2"
        assert result["version"] == 2
        assert result["parent"] == "bench-1"
        assert result["cases"] == ["CASE-001-first", "CASE-002-second"]
        cfg = benchmark_store.read_config("bench-1-v2")
        assert cfg.version == 2
        assert cfg.parent == "bench-1"
        assert cfg.frozen is False
        assert cfg.test_agent == "some-agent"
        # 源 benchmark 原样保留
        assert benchmark_store.read_config("bench-1").frozen is True

    def test_evolve_as_custom_id(self, tmp_path):
        _make_frozen_bench()
        result = benchmark_store.evolve_benchmark("bench-1", new_id="bench-2")
        assert result["benchmark"] == "bench-2"
        assert benchmark_store.read_config("bench-2").version == 2

    def test_evolve_copies_case_contents(self, tmp_path):
        _make_frozen_bench()
        benchmark_store.evolve_benchmark("bench-1")
        for case_id in ("CASE-001-first", "CASE-002-second"):
            assert benchmark_store.read_statement(
                "bench-1-v2", case_id
            ) == benchmark_store.read_statement("bench-1", case_id)
            assert benchmark_store.read_rubric(
                "bench-1-v2", case_id
            ) == benchmark_store.read_rubric("bench-1", case_id)

    def test_evolve_does_not_inherit_scoreboard(self, tmp_path):
        _make_frozen_bench()
        # 给源 scoreboard 追加一条评估,确认新版不继承
        from jiuwenswarm.harness_evolve.schemas import Evaluation
        from jiuwenswarm.harness_evolve.schemas import now_utc_iso

        benchmark_store.append_evaluation(
            "bench-1",
            Evaluation(
                time=now_utc_iso(), version=1, model_id="m",
                summary_title="t", summary="s", score=50.0,
                cost=None, duration_ms=1,
            ),
        )
        assert len(benchmark_store.read_scoreboard("bench-1").evaluations) == 1
        benchmark_store.evolve_benchmark("bench-1")
        assert benchmark_store.read_scoreboard("bench-1-v2").evaluations == []

    def test_evolve_title_override(self, tmp_path):
        _make_frozen_bench()
        benchmark_store.evolve_benchmark("bench-1", title="新标题")
        cfg = benchmark_store.read_config("bench-1-v2")
        assert cfg.title == "新标题"
        assert cfg.description == "D"  # 未覆盖则继承

    def test_evolve_target_exists_refused(self, tmp_path):
        _make_frozen_bench()
        _make_frozen_bench("bench-1-v2", title="已经存在")
        with pytest.raises(BenchmarkValidationError, match="已存在"):
            benchmark_store.evolve_benchmark("bench-1")

    def test_evolved_is_editable_then_freeze(self, tmp_path):
        _make_frozen_bench()
        benchmark_store.evolve_benchmark("bench-1")
        # 新版可增删改
        benchmark_store.write_case(
            "bench-1-v2", "CASE-003-new", "新维度任务", RUBRIC
        )
        benchmark_store.delete_case("bench-1-v2", "CASE-001-first")
        assert "CASE-003-new" in benchmark_store.list_cases("bench-1-v2")
        assert "CASE-001-first" not in benchmark_store.list_cases("bench-1-v2")
        benchmark_store.freeze_benchmark("bench-1-v2")
        # 冻结后拒绝再写
        with pytest.raises(BenchmarkValidationError):
            benchmark_store.write_case(
                "bench-1-v2", "CASE-004-x", "任务", RUBRIC
            )


class TestDeleteCase:
    def test_delete_works_unfrozen(self, tmp_path):
        benchmark_store.init_benchmark("bench-1", "T", "D", "a")
        benchmark_store.write_case("bench-1", "CASE-001-x", "任务", RUBRIC)
        assert benchmark_store.delete_case("bench-1", "CASE-001-x") is True
        assert benchmark_store.list_cases("bench-1") == []

    def test_delete_refuses_frozen(self, tmp_path):
        _make_frozen_bench()
        with pytest.raises(BenchmarkValidationError, match="已冻结"):
            benchmark_store.delete_case("bench-1", "CASE-001-first")

    def test_delete_missing_case(self, tmp_path):
        benchmark_store.init_benchmark("bench-1", "T", "D", "a")
        with pytest.raises(BenchmarkValidationError, match="不存在"):
            benchmark_store.delete_case("bench-1", "CASE-999-none")


class TestBackwardCompat:
    def test_old_config_without_version_fields(self, tmp_path):
        benchmark_store.init_benchmark("bench-1", "T", "D", "a")
        cfg_path = paths.benchmark_config_file("bench-1")
        # 手工抹掉 version/parent,模拟旧格式
        lines = [
            l for l in cfg_path.read_text(encoding="utf-8").splitlines()
            if not l.startswith("version") and not l.startswith("parent")
        ]
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cfg = benchmark_store.read_config("bench-1")
        assert cfg.version == 1
        assert cfg.parent is None


class TestCli:
    def test_benchmark_list_output(self, tmp_path, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        _make_frozen_bench()
        benchmark_store.evolve_benchmark("bench-1")
        capsys.readouterr()
        # 不传 --data-dir:conftest 已把数据根重定向到 tmp
        rc = main(["benchmark-list", "--json"])
        assert rc == 0
        payload = json_loads(capsys.readouterr().out)
        by_id = {b["benchmark"]: b for b in payload["benchmarks"]}
        assert by_id["bench-1"]["frozen"] is True
        assert by_id["bench-1"]["version"] == 1
        assert by_id["bench-1-v2"]["frozen"] is False
        assert by_id["bench-1-v2"]["version"] == 2
        assert by_id["bench-1-v2"]["parent"] == "bench-1"

    def test_cli_evolve_and_case_delete(self, tmp_path, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        _make_frozen_bench()
        capsys.readouterr()
        rc = main(["benchmark-evolve", "--benchmark", "bench-1", "--json"])
        assert rc == 0
        assert json_loads(capsys.readouterr().out)["benchmark"] == "bench-1-v2"
        rc = main(
            ["case-delete", "--benchmark", "bench-1-v2", "--case", "CASE-001-first", "--json"]
        )
        assert rc == 0
        assert json_loads(capsys.readouterr().out)["deleted"] is True


def json_loads(out: str) -> dict:
    import json

    idx = out.index("{")
    return json.loads(out[idx:])
