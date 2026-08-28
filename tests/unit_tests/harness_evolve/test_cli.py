# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CLI 命令测试(happy path + 错误路径;--data-dir 隔离)。"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.harness_evolve import agent_state, benchmark_store, paths
from jiuwenswarm.harness_evolve.cli import main

RUBRIC = """# Grading

## Scoring

| item | points | description |
| ---- | ------ | ----------- |
| correctness | 60 | right output |
| clarity | 40 | readable |
"""

STATEMENT = "Write a one-line summary of this repo."

EVAL_YAML = """time: "2026-08-14T09:30:00Z"
version: 1
model_id: gpt-4o
summary_title: baseline
summary: accepted baseline
score: 80.0
cost: 0.1
duration_ms: 100
cases:
  - case: CASE-001-sum
    score: 80.0
    cost: 0.1
    duration_ms: 100
    runs:
      - score: 80.0
        cost: 0.1
        duration_ms: 100
        session_id: he-00000001-000000000001-r1-11111111
"""


@pytest.fixture
def prompt_file(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("你是摘要 agent。\n", encoding="utf-8")
    return p


@pytest.fixture
def registered(tmp_path, prompt_file):
    """注册一个 agent 并返回名字。"""
    rc = main(["agent-register", "--name", "sum-agent",
               "--description", "summarizer", "--prompt-file", str(prompt_file),
               "--data-dir", str(tmp_path / "he")])
    assert rc == 0
    return "sum-agent"


@pytest.fixture
def frozen_bench(tmp_path, registered):
    """创建 1 case 的冻结 benchmark。"""
    rc = main(["benchmark-init", "--benchmark", "sum-bench", "--agent", "sum-agent",
               "--title", "Summarizer", "--description", "summarize repos",
               "--data-dir", str(tmp_path / "he")])
    assert rc == 0
    stmt = tmp_path / "stmt.md"
    rub = tmp_path / "rubric.md"
    stmt.write_text(STATEMENT, encoding="utf-8")
    rub.write_text(RUBRIC, encoding="utf-8")
    rc = main(["case-write", "--benchmark", "sum-bench", "--case", "CASE-001-sum",
               "--statement", str(stmt), "--rubric", str(rub),
               "--data-dir", str(tmp_path / "he")])
    assert rc == 0
    rc = main(["benchmark-freeze", "--benchmark", "sum-bench",
               "--baseline-version", "1", "--data-dir", str(tmp_path / "he")])
    assert rc == 0
    return "sum-bench"


class TestAgentCommands:
    def test_register_creates_state(self, tmp_path, prompt_file):
        rc = main(["agent-register", "--name", "my-agent",
                   "--description", "d", "--prompt-file", str(prompt_file),
                   "--max-iterations", "5", "--tools", "Read,Bash",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        state = agent_state.get_agent_state("my-agent")
        assert state["version"] == 1

    def test_register_invalid_name(self, tmp_path, prompt_file):
        rc = main(["agent-register", "--name", "bad name!",
                   "--description", "d", "--prompt-file", str(prompt_file),
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_register_missing_prompt(self, tmp_path):
        rc = main(["agent-register", "--name", "my-agent",
                   "--description", "d", "--prompt-file", str(tmp_path / "nope.md"),
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_list_and_inspect(self, tmp_path, registered):
        rc = main(["agent-list", "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        rc = main(["agent-inspect", "--name", "sum-agent",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 0

    def test_list_json(self, tmp_path, registered, capsys):
        rc = main(["agent-list", "--json", "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert any(a["name"] == "sum-agent" and a["version"] == 1
                   for a in out["agents"])

    def test_inspect_unknown(self, tmp_path):
        rc = main(["agent-inspect", "--name", "nope", "--data-dir",
                   str(tmp_path / "he")])
        assert rc == 2


class TestBenchmarkCommands:
    def test_init_requires_agent(self, tmp_path):
        rc = main(["benchmark-init", "--benchmark", "sum-bench", "--agent", "ghost",
                   "--title", "t", "--description", "d",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_init_invalid_id(self, tmp_path, registered):
        rc = main(["benchmark-init", "--benchmark", "Bad_Id!",
                   "--agent", "sum-agent", "--title", "t", "--description", "d",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_case_write_and_validate(self, tmp_path, registered):
        main(["benchmark-init", "--benchmark", "b1x", "--agent", "sum-agent",
              "--title", "t", "--description", "d",
              "--data-dir", str(tmp_path / "he")])
        stmt = tmp_path / "s.md"
        rub = tmp_path / "r.md"
        stmt.write_text(STATEMENT, encoding="utf-8")
        rub.write_text(RUBRIC, encoding="utf-8")
        rc = main(["case-write", "--benchmark", "b1x", "--case", "CASE-001-x",
                   "--statement", str(stmt), "--rubric", str(rub),
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        rc = main(["benchmark-validate", "--benchmark", "b1x",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 0

    def test_case_write_bad_rubric(self, tmp_path, registered):
        main(["benchmark-init", "--benchmark", "b1x", "--agent", "sum-agent",
              "--title", "t", "--description", "d",
              "--data-dir", str(tmp_path / "he")])
        stmt = tmp_path / "s.md"
        rub = tmp_path / "r.md"
        stmt.write_text(STATEMENT, encoding="utf-8")
        rub.write_text(RUBRIC.replace("| clarity | 40", "| clarity | 30"),
                       encoding="utf-8")
        rc = main(["case-write", "--benchmark", "b1x", "--case", "CASE-001-x",
                   "--statement", str(stmt), "--rubric", str(rub),
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_leak_check(self, tmp_path, registered):
        main(["benchmark-init", "--benchmark", "b1x", "--agent", "sum-agent",
              "--title", "t", "--description", "d",
              "--data-dir", str(tmp_path / "he")])
        stmt = tmp_path / "s.md"
        rub = tmp_path / "r.md"
        stmt.write_text(STATEMENT + "\n\nrubric says right output counts",
                        encoding="utf-8")
        rub.write_text(RUBRIC, encoding="utf-8")
        main(["case-write", "--benchmark", "b1x", "--case", "CASE-001-x",
              "--statement", str(stmt), "--rubric", str(rub),
              "--data-dir", str(tmp_path / "he")])
        rc = main(["leak-check", "--benchmark", "b1x",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_freeze_unvalidated_rejected(self, tmp_path, registered):
        main(["benchmark-init", "--benchmark", "b1x", "--agent", "sum-agent",
              "--title", "t", "--description", "d",
              "--data-dir", str(tmp_path / "he")])
        rc = main(["benchmark-freeze", "--benchmark", "b1x",
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_freeze_records_baseline(self, tmp_path, frozen_bench):
        assert benchmark_store.is_frozen(frozen_bench)
        assert agent_state.baseline_version("sum-agent") == 1

    def test_freeze_baseline_mismatch(self, tmp_path, registered):
        main(["benchmark-init", "--benchmark", "b1x", "--agent", "sum-agent",
              "--title", "t", "--description", "d",
              "--data-dir", str(tmp_path / "he")])
        stmt = tmp_path / "s.md"
        rub = tmp_path / "r.md"
        stmt.write_text(STATEMENT, encoding="utf-8")
        rub.write_text(RUBRIC, encoding="utf-8")
        main(["case-write", "--benchmark", "b1x", "--case", "CASE-001-x",
              "--statement", str(stmt), "--rubric", str(rub),
              "--data-dir", str(tmp_path / "he")])
        rc = main(["benchmark-freeze", "--benchmark", "b1x",
                   "--baseline-version", "9", "--data-dir", str(tmp_path / "he")])
        assert rc == 2

    def test_scoreboard_append_and_read(self, tmp_path, frozen_bench):
        ev = tmp_path / "ev.yaml"
        ev.write_text(EVAL_YAML, encoding="utf-8")
        rc = main(["scoreboard-append", "--benchmark", frozen_bench,
                   "--file", str(ev), "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        rc = main(["scoreboard-read", "--benchmark", frozen_bench,
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        board = benchmark_store.read_scoreboard(frozen_bench)
        assert board.evaluations[0].version == 1

    def test_scoreboard_append_not_frozen(self, tmp_path, registered):
        main(["benchmark-init", "--benchmark", "b1x", "--agent", "sum-agent",
              "--title", "t", "--description", "d",
              "--data-dir", str(tmp_path / "he")])
        ev = tmp_path / "ev.yaml"
        ev.write_text(EVAL_YAML, encoding="utf-8")
        rc = main(["scoreboard-append", "--benchmark", "b1x", "--file", str(ev),
                   "--data-dir", str(tmp_path / "he")])
        assert rc == 3

    def test_export_import_cli(self, tmp_path, frozen_bench):
        ev = tmp_path / "ev.yaml"
        ev.write_text(EVAL_YAML, encoding="utf-8")
        main(["scoreboard-append", "--benchmark", frozen_bench,
              "--file", str(ev), "--data-dir", str(tmp_path / "he")])
        rc = main(["benchmark-export", "--benchmark", frozen_bench,
                   "--out", str(tmp_path / "out"), "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        rc = main(["benchmark-import", "--file", str(tmp_path / "out.zip"),
                   "--as", "copy-bench", "--data-dir", str(tmp_path / "he")])
        assert rc == 0
        assert benchmark_store.list_cases("copy-bench") == ["CASE-001-sum"]
