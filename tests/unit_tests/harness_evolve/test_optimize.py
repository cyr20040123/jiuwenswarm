# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P4 optimize 闭环集成测试(全程无 LLM:fake Model / fake DeepAgent 打桩)。

对应计划集成两剧本:
- 剧本一(accept):scoreboard 基线 v1 低分 → optimize 一轮候选 v2 高分 →
  **接受** 且 append 入 scoreboard,agent 版本前移;
- 剧本二(reject):候选平分/低分(不严格大于)→ **拒绝** → restore 后 md 文件
  与 sidecar 完全回退、scoreboard 不变、被拒候选证据保留、候选版本不复用。

另覆盖:scoreboard 为空 → UsageError;运行时版本漂移 → ScoreboardError;
matrix 无效(launcher 失败)→ 拒绝+回滚;proposal 契约坏输出(非 JSON/缺 prompt)。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.harness_evolve import (
    agent_state,
    benchmark_store,
    evaluator,
    optimizer,
    paths,
    schemas,
)
from jiuwenswarm.harness_evolve.errors import HarnessUsageError, ScoreboardError
from jiuwenswarm.harness_evolve.pipeline import _model_cache

RUBRIC = """# Grading

## Scoring

| item | points | description |
| ---- | ------ | ----------- |
| correctness | 60 | right output |
| clarity | 40 | readable |
"""

STATEMENT = "Write a one-line summary of this repo."

PROMPT_V1 = "你是摘要 agent。"
PROMPT_V2 = "你是改进后的摘要 agent v2。严格遵循输出格式。"


def _grade_json(total: float) -> str:
    correctness = min(60.0, round(total - 20.0, 2))
    clarity = round(total - correctness, 2)
    return json.dumps(
        {
            "items": [
                {"item": "correctness", "score": correctness, "rationale": "ok"},
                {"item": "clarity", "score": clarity, "rationale": "ok"},
            ],
            "total": total,
        }
    )


def _proposal_json(prompt: str) -> str:
    return json.dumps(
        {
            "summary_title": "candidate",
            "summary": "proposed change",
            "prompt": prompt,
            "changes": ["强化约束"],
        }
    )


class FakeModel:
    """grader/proposal 用假模型:依次吐出预设响应,记录 prompt。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def invoke(self, prompt: str):
        self.calls.append(prompt)
        content = self.responses.pop(0) if self.responses else ""
        return SimpleNamespace(content=content)


class FakeDeepAgent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def ensure_initialized(self):
        return None

    async def stream(self, inputs):
        for chunk in self._chunks:
            yield chunk

    def get_context_usage(self, session_id=None, context_id="default_context_id"):
        return {"total_tokens": 10}


def _answer_chunk(text: str = "summary output"):
    return SimpleNamespace(type="answer", payload={"output": {"output": text}})


def _eval(version: int, score: float, case_id: str) -> schemas.Evaluation:
    """构造一条可直接 append 的 Evaluation(scoreboard 引用 agent 版本)。"""
    return schemas.Evaluation(
        time=schemas.now_utc_iso(),
        version=version,
        model_id="fake-model",
        summary_title=f"v{version}",
        summary="baseline",
        score=score,
        cost=None,
        duration_ms=10,
        cases=[
            schemas.CaseScore(
                case=case_id,
                score=score,
                cost=None,
                duration_ms=10,
                runs=[
                    schemas.RunScore(
                        score=score, cost=None, duration_ms=10, session_id="s1"
                    )
                ],
            )
        ],
    )


@pytest.fixture
def agent_def():
    """真实注册被测 agent(opt-agent,v1 内容=PROMPT_V1)。"""
    from jiuwenswarm.server.runtime.agent_config_service import (
        AgentConfigService,
        CreateAgentParams,
    )

    created = AgentConfigService().create_agent(
        CreateAgentParams(
            name="opt-agent", description="optimize target",
            prompt=PROMPT_V1, location="user",
        )
    )
    agent_state.ensure_agent_state(created.name, created.file_path)
    return created.name, Path(created.file_path)


@pytest.fixture
def bench_with_baseline(agent_def):
    """冻结 benchmark + scoreboard 已有一条 v1=60 基线。"""
    name, _ = agent_def
    benchmark_store.init_benchmark("opt-bench", "Opt Bench", "optimize me", name)
    benchmark_store.write_case("opt-bench", "CASE-001-sum", STATEMENT, RUBRIC)
    benchmark_store.freeze_benchmark("opt-bench")
    benchmark_store.append_evaluation("opt-bench", _eval(1, 60.0, "CASE-001-sum"))
    return "opt-bench"


@pytest.fixture
def stub_runtime(monkeypatch, bench_with_baseline):
    """把 evaluator 模型/资源打桩并注入 FakeDeepAgent;清除模型缓存。"""

    def _install(grade_totals, chunks=None, construct_error=None):
        # grade_totals 是期望的打分总分数值,转换成 grader 需要的 JSON 响应串
        grade_responses = [_grade_json(total) for total in grade_totals]
        monkeypatch.setattr(
            evaluator, "_resolve_model",
            lambda name=None: (FakeModel(grade_responses), "fake-model"),
        )
        monkeypatch.setattr(
            evaluator, "_register_sysop",
            lambda sysop_id: SimpleNamespace(id=sysop_id),
        )
        monkeypatch.setattr(evaluator, "_unregister_sysop", lambda sysop_id: None)
        _model_cache.clear()

        def _fake_factory(**kwargs):
            if construct_error is not None:
                raise construct_error
            return FakeDeepAgent(chunks or [_answer_chunk()])

        monkeypatch.setattr(
            "openjiuwen.harness.factory.create_deep_agent", _fake_factory
        )
        return _fake_factory

    return _install


def _run_optimize(agent_name, benchmark_id, *, target=95.0, rounds=1,
                  proposal_responses=None, **kwargs):
    prop_model = FakeModel(proposal_responses or [])
    return asyncio.run(
        optimizer.run_optimize(
            agent_name, benchmark_id, target_score=target, rounds=rounds,
            debug_trace=False, proposal_model=prop_model, **kwargs
        )
    )


class TestOptimizeAccept:
    def test_one_round_accept_appends_scoreboard(self, agent_def, bench_with_baseline,
                                                 stub_runtime):
        """剧本一:候选 v2 高分(90 > 60)→ 接受并 append,agent 前移到 v2。"""
        name, md_path = agent_def
        stub_runtime(grade_totals=[90.0])
        reports = _run_optimize(
            name, bench_with_baseline, proposal_responses=[_proposal_json(PROMPT_V2)]
        )
        assert len(reports) == 1
        r = reports[0]
        assert r.accepted and not r.rolled_back
        assert r.reference_version == 1 and r.candidate_version == 2
        assert r.matrix_score == 90.0 and r.reference_score == 60.0
        assert r.evaluation_valid

        # scoreboard:两条(v1=60 → v2=90),版本单调
        board = benchmark_store.read_scoreboard(bench_with_baseline)
        assert [e.version for e in board.evaluations] == [1, 2]
        assert board.evaluations[-1].score == 90.0

        # agent 定义与 sidecar 前移
        assert agent_state.agent_version(name) == 2
        assert PROMPT_V2 in md_path.read_text(encoding="utf-8")

        # 快照:只有 Reference v1 被创建(v2 是候选,不做快照)
        assert paths.snapshot_file(name, 1).is_file()
        assert not paths.snapshot_file(name, 2).exists()

        # 证据归档(accepted 也留档)
        evidence = paths.benchmark_evaluations_dir(bench_with_baseline) / "optimize-r1.yaml"
        assert evidence.is_file()

    def test_target_reached_early_stop(self, agent_def, bench_with_baseline, stub_runtime):
        """v2 达 target → 只跑一轮就停(rounds=3 也不续跑)。"""
        name, _ = agent_def
        stub_runtime(grade_totals=[92.0])
        reports = _run_optimize(name, bench_with_baseline, target=90.0, rounds=3,
                                proposal_responses=[_proposal_json(PROMPT_V2)])
        assert len(reports) == 1
        assert reports[0].accepted
        assert "早停" in reports[0].reason

    def test_second_round_after_reject_uses_fresh_version(self, agent_def,
                                                          bench_with_baseline, stub_runtime):
        """剧本二续:首轮被拒(50 ≤ 60)→ 回滚;第二轮新候选 90 → 接受。
        被拒候选版本 2 不复用,scoreboard 版本序列 1 → 3。"""
        name, md_path = agent_def
        stub_runtime(grade_totals=[50.0, 90.0])
        reports = _run_optimize(
            name, bench_with_baseline, rounds=2,
            proposal_responses=[
                _proposal_json("坏候选 prompt"),
                _proposal_json(PROMPT_V2),
            ],
        )
        assert [r.accepted for r in reports] == [False, True]
        assert reports[0].rolled_back and reports[1].candidate_version == 3
        board = benchmark_store.read_scoreboard(bench_with_baseline)
        assert [e.version for e in board.evaluations] == [1, 3]
        assert board.evaluations[-1].score == 90.0
        # 回滚后 md 是 v1 内容,二轮接受后是 v2 内容
        assert PROMPT_V2 in md_path.read_text(encoding="utf-8")
        assert agent_state.agent_version(name) == 3
        # 被拒候选与接受候选的证据都独立保留
        ev_dir = paths.benchmark_evaluations_dir(bench_with_baseline)
        assert (ev_dir / "optimize-r1.yaml").is_file()
        assert (ev_dir / "optimize-r2.yaml").is_file()


class TestOptimizeReject:
    def test_equal_score_rejected_and_rolled_back(self, agent_def, bench_with_baseline,
                                                  stub_runtime):
        """剧本二:候选 60 == Reference 60(非严格大于)→ 拒绝+完全回退。"""
        name, md_path = agent_def
        stub_runtime(grade_totals=[60.0])
        reports = _run_optimize(name, bench_with_baseline,
                                proposal_responses=[_proposal_json(PROMPT_V2)])
        assert len(reports) == 1
        r = reports[0]
        assert not r.accepted and r.rolled_back
        assert "未严格大于" in r.reason

        # scoreboard 不变(只有 v1 基线)
        board = benchmark_store.read_scoreboard(bench_with_baseline)
        assert [e.version for e in board.evaluations] == [1]

        # md 文件与 sidecar 完全回退(文件 hash == sidecar 记录的 v1 hash)
        assert PROMPT_V1 in md_path.read_text(encoding="utf-8")
        state = agent_state.get_agent_state(name)
        assert state["version"] == 1
        assert agent_state.md_sha256(md_path) == state["md_sha256"]

        # 被拒候选版本不复用:next_version 已越过 2
        assert state["next_version"] >= 3

        # 回滚后快照 v1 仍在(供下一轮复用,不覆盖)
        assert paths.snapshot_file(name, 1).is_file()

        # 被拒候选证据保留
        evidence = paths.benchmark_evaluations_dir(bench_with_baseline) / "optimize-r1.yaml"
        assert evidence.is_file()

    def test_matrix_invalid_rejected(self, agent_def, bench_with_baseline, stub_runtime):
        """matrix 无效(launcher 失败,不可打分)→ 拒绝+回滚。"""
        name, md_path = agent_def
        stub_runtime(grade_totals=[], construct_error=RuntimeError("launch exploded"))
        reports = _run_optimize(name, bench_with_baseline,
                                proposal_responses=[_proposal_json(PROMPT_V2)])
        assert len(reports) == 1
        r = reports[0]
        assert not r.accepted and r.rolled_back
        assert "评测失败" in r.reason
        assert agent_state.agent_version(name) == 1
        assert PROMPT_V1 in md_path.read_text(encoding="utf-8")
        assert [e.version for e in benchmark_store.read_scoreboard(
            bench_with_baseline).evaluations] == [1]


class TestOptimizeGuardRails:
    def test_empty_scoreboard_usage_error(self, agent_def):
        """scoreboard 无已接受基线 → 不可 optimize。"""
        name, _ = agent_def
        benchmark_store.init_benchmark("fresh-bench", "Fresh", "no baseline", name)
        benchmark_store.write_case("fresh-bench", "CASE-001-sum", STATEMENT, RUBRIC)
        with pytest.raises(HarnessUsageError, match="scoreboard 为空"):
            asyncio.run(optimizer.run_optimize(name, "fresh-bench", target_score=90.0))

    def test_runtime_version_mismatch_stops(self, agent_def, bench_with_baseline):
        """scoreboard 末条版本 ≠ agent 当前版本 → ScoreboardError。"""
        name, _ = agent_def
        # 伪造:agent 在 v1,但 scoreboard 末条声明 v2
        benchmark_store.append_evaluation(
            bench_with_baseline, _eval(2, 70.0, "CASE-001-sum")
        )
        with pytest.raises(ScoreboardError, match="运行时不一致"):
            asyncio.run(optimizer.run_optimize(name, bench_with_baseline, target_score=90.0))


class TestProposalParsing:
    def test_bad_json_raises_usage_error(self):
        with pytest.raises(HarnessUsageError, match="非 JSON"):
            optimizer._parse_proposal("not json at all")

    def test_fenced_json_accepted(self):
        proposal = optimizer._parse_proposal(
            "```json\n" + _proposal_json(PROMPT_V2) + "\n```"
        )
        assert proposal.prompt == PROMPT_V2
        assert proposal.changes == ["强化约束"]

    def test_missing_prompt_rejected(self):
        with pytest.raises(HarnessUsageError, match="prompt"):
            optimizer._parse_proposal('{"summary": "no prompt here"}')
