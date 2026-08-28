# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""benchmark_store.py 测试(纯文件逻辑)。"""

from __future__ import annotations

import zipfile

import pytest

from jiuwenswarm.harness_evolve import paths
from jiuwenswarm.harness_evolve.benchmark_store import (
    append_evaluation,
    benchmark_exists,
    export_benchmark,
    freeze_benchmark,
    import_benchmark,
    init_benchmark,
    is_frozen,
    leak_check,
    list_benchmarks,
    list_cases,
    read_config,
    read_scoreboard,
    validate_benchmark,
    write_case,
)
from jiuwenswarm.harness_evolve.errors import (
    BenchmarkError,
    BenchmarkValidationError,
)
from jiuwenswarm.harness_evolve.schemas import (
    CaseScore,
    Evaluation,
    RunScore,
)

RUBRIC = """# Grading

## Scoring

| item | points | description |
| ---- | ------ | ----------- |
| correctness | 60 | right output |
| clarity | 40 | readable |
"""

STATEMENT = "Write a one-line summary of this repo."


def _ev(version, score=80.0):
    return Evaluation(
        time="2026-08-14T09:30:00Z",
        version=version,
        model_id="m",
        summary_title=f"v{version}",
        summary=f"round {version}",
        score=score,
        cost=0.1,
        duration_ms=100,
        cases=[
            CaseScore(
                case="CASE-001-sum",
                score=score,
                cost=0.1,
                duration_ms=100,
                runs=[
                    RunScore(
                        score=score, cost=0.1, duration_ms=100,
                        session_id="he-00000001-000000000001-r1-11111111",
                    )
                ],
            )
        ],
    )


@pytest.fixture
def bid(tmp_path):
    return "summarizer-bench"


@pytest.fixture
def bench(bid):
    init_benchmark(bid, "Summarizer", "summarize repos", "sum-agent")
    return bid


class TestInit:
    def test_creates_layout(self, bench):
        assert benchmark_exists(bench)
        assert paths.scoreboard_file(bench).is_file()
        cfg = read_config(bench)
        assert cfg.title == "Summarizer"
        assert cfg.test_agent == "sum-agent"
        assert cfg.runs == 1
        assert cfg.frozen is False
        assert read_scoreboard(bench).evaluations == []

    def test_invalid_id(self):
        with pytest.raises(BenchmarkValidationError, match="id"):
            init_benchmark("Bad_Id!", "t", "d", "a")

    def test_refuse_existing(self, bench):
        with pytest.raises(BenchmarkValidationError, match="已存在"):
            init_benchmark(bench, "again", "d", "a")

    def test_empty_title_rejected(self):
        with pytest.raises(BenchmarkValidationError, match="title"):
            init_benchmark("x-bench", "  ", "d", "a")

    def test_list(self, bench):
        assert bench in list_benchmarks()


class TestCase:
    def test_write_and_read(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        assert list_cases(bench) == ["CASE-001-sum"]

    def test_bad_case_id(self, bench):
        with pytest.raises(BenchmarkValidationError, match="CASE-"):
            write_case(bench, "case-1", STATEMENT, RUBRIC)

    def test_rubric_sum_not_100(self, bench):
        bad = RUBRIC.replace("| clarity | 40", "| clarity | 30")
        with pytest.raises(BenchmarkValidationError, match="总和必须恰好 100"):
            write_case(bench, "CASE-001-sum", STATEMENT, bad)

    def test_empty_statement(self, bench):
        with pytest.raises(BenchmarkValidationError, match="statement"):
            write_case(bench, "CASE-001-sum", "   ", RUBRIC)

    def test_refuse_after_frozen(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        freeze_benchmark(bench)
        with pytest.raises(BenchmarkValidationError, match="冻结"):
            write_case(bench, "CASE-002-more", STATEMENT, RUBRIC)


class TestValidateAndFreeze:
    def test_empty_benchmark_has_defects(self, bench):
        defects = validate_benchmark(bench)
        assert any("case" in d.lower() for d in defects)

    def test_complete_is_clean(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        assert validate_benchmark(bench) == []

    def test_freeze_then_refreeze_rejected(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        freeze_benchmark(bench)
        assert is_frozen(bench)
        with pytest.raises(BenchmarkValidationError, match="已冻结"):
            freeze_benchmark(bench)

    def test_freeze_requires_valid(self, bench):
        with pytest.raises(BenchmarkValidationError, match="校验"):
            freeze_benchmark(bench)


class TestLeakCheck:
    def test_fragment_leak_detected(self, bench):
        leaked = STATEMENT + "\n\nright output appears here as rubric says"
        write_case(bench, "CASE-001-sum", leaked, RUBRIC)
        issues = leak_check(bench)
        assert any("泄露 rubric" in i and "right output" in i for i in issues)

    def test_rubric_keyword_detected(self, bench):
        leaked = "Follow the rubric/README.md instructions."
        write_case(bench, "CASE-001-sum", leaked, RUBRIC)
        issues = leak_check(bench)
        assert any("'rubric'" in i for i in issues)

    def test_clean_statement(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        assert leak_check(bench) == []


class TestScoreboard:
    def test_append_requires_frozen(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        with pytest.raises(Exception):
            append_evaluation(bench, _ev(1))

    def test_append_and_read(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        freeze_benchmark(bench)
        append_evaluation(bench, _ev(1))
        board = read_scoreboard(bench)
        assert len(board.evaluations) == 1
        assert board.evaluations[0].score == 80.0

    def test_version_monotonic_enforced(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        freeze_benchmark(bench)
        append_evaluation(bench, _ev(2))
        with pytest.raises(Exception, match="严格大于"):
            append_evaluation(bench, _ev(2))

    def test_append_after_external_edit_reparsed(self, bench):
        """写后重 parse:外部手改出坏 yaml → 后续 append 直接失败。"""
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        freeze_benchmark(bench)
        paths.scoreboard_file(bench).write_text(
            "evaluations: [broken", encoding="utf-8"
        )
        with pytest.raises(Exception):
            append_evaluation(bench, _ev(1))


class TestExportImport:
    def test_roundtrip(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        freeze_benchmark(bench)
        append_evaluation(bench, _ev(1))
        out = tmp_path_zip(bench)
        # 原 benchmark 仍在 → 改名导入
        imported = import_benchmark(out, new_id="copy-bench")
        assert imported == "copy-bench"
        assert list_cases(imported) == ["CASE-001-sum"]
        assert read_scoreboard(imported).evaluations[0].version == 1

    def test_roundtrip_same_id_after_removal(self, bench):
        """删掉原目录后可按原 id 导入(迁移场景)。"""
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        out = tmp_path_zip(bench)
        import shutil

        shutil.rmtree(paths.benchmark_dir(bench))
        imported = import_benchmark(out)
        assert imported == bench
        assert list_cases(imported) == ["CASE-001-sum"]

    def test_zip_top_level_single_dir(self, bench):
        out = tmp_path_zip(bench)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert names[0].startswith(f"{bench}/")

    def test_conflict_rejected(self, bench):
        """原 benchmark 目录仍在时,同 id 导入必须被拒绝。"""
        out = tmp_path_zip(bench)
        with pytest.raises(BenchmarkValidationError, match="已存在"):
            import_benchmark(out)

    def test_import_with_new_id(self, bench):
        write_case(bench, "CASE-001-sum", STATEMENT, RUBRIC)
        out = tmp_path_zip(bench)
        imported = import_benchmark(out, new_id="renamed-bench")
        assert imported == "renamed-bench"
        assert list_cases(imported) == ["CASE-001-sum"]

    def test_missing_config_rejected(self, tmp_path):
        bad_zip = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("x-bench/scoreboard.yaml", "evaluations: []\n")
        with pytest.raises(BenchmarkValidationError, match="config"):
            import_benchmark(bad_zip)

    def test_missing_benchmark_for_export(self, bench):
        with pytest.raises(BenchmarkError, match="不存在"):
            export_benchmark("nope-bench", str(bench + ".zip"))


def tmp_path_zip(bench):
    import os

    out = paths.benchmarks_dir().parent / "tmp-export"
    export_benchmark(bench, str(out))
    return out.with_suffix(".zip")
