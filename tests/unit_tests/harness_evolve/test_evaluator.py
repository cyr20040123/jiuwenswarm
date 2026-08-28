# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P3 评测执行器单元测试(无 LLM:fake Model / fake DeepAgent 打桩)。

覆盖(与计划 test_evaluator_classification 对应):
- 工作区隔离:statement 拷贝、rubric 绝不入内
- run 分类:构造失败 -> LaunchFailure;流异常 -> failed/launch_failed;
  正常跑完 -> ok(即使产物差,可打分);版本漂移 -> version_changed
- trace:run 后 dump 落盘
- grader:JSON 契约校验、坏输出同证据重试一次、两次皆坏 -> GradingFailure
- 工具注册表:未知工具名告警跳过
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.harness_evolve import agent_state, benchmark_store, evaluator, paths
from jiuwenswarm.harness_evolve.errors import (
    GradingFailure,
    HarnessUsageError,
    LaunchFailure,
)
from jiuwenswarm.harness_evolve.grader import grade_run
from jiuwenswarm.harness_evolve.tools_registry import build_named_tools

RUBRIC = """# Grading

## Scoring

| item | points | description |
| ---- | ------ | ----------- |
| correctness | 60 | right output |
| clarity | 40 | readable |
"""

STATEMENT = "Write a one-line summary of this repo."

RUBRIC_POINTS = {"correctness": 60, "clarity": 40}


def _valid_grade_json(total=100.0):
    """items 之和恒等于 total 且不超单项上限(correctness<=60, clarity<=40);
    调用方传非 100 的总分测 rejected。"""
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


class FakeModel:
    """grader 用假模型:依次吐出预设响应,记录 prompt。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def invoke(self, prompt: str):
        self.calls.append(prompt)
        content = self.responses.pop(0) if self.responses else ""
        return SimpleNamespace(content=content)


class FakeDeepAgent:
    """构造/流式接口打桩(stream 由 chunk 生成器驱动,可注异常/副作用)。"""

    def __init__(self, chunks, raise_on_stream=None, side_effect=None):
        self._chunks = chunks
        self._raise_on_stream = raise_on_stream
        self._side_effect = side_effect
        self.initialized = False
        self.usage_calls = 0

    async def ensure_initialized(self):
        self.initialized = True

    async def stream(self, inputs):
        for chunk in self._chunks:
            if self._side_effect is not None:
                self._side_effect()
            yield chunk
        if self._raise_on_stream is not None:
            raise self._raise_on_stream

    def get_context_usage(self, session_id=None, context_id="default_context_id"):
        self.usage_calls += 1
        return {"total_tokens": 10}


def _answer_chunk(text: str = "final answer"):
    return SimpleNamespace(type="answer", payload={"output": {"output": text}})


def _delta_chunk(text: str = "partial"):
    return SimpleNamespace(type="llm_output", payload={"content": text})


@pytest.fixture
def agent_def():
    """真实注册一个被测 agent,返回 (name, md_path)。"""
    from jiuwenswarm.server.runtime.agent_config_service import (
        AgentConfigService,
        CreateAgentParams,
    )

    created = AgentConfigService().create_agent(
        CreateAgentParams(
            name="sum-agent", description="summarizer",
            prompt="你是摘要 agent。", location="user",
        )
    )
    agent_state.ensure_agent_state(created.name, created.file_path)
    return created.name, Path(created.file_path)


@pytest.fixture
def bench(agent_def):
    """1 case 的未冻结 benchmark(评测不要求 frozen)。"""
    name, _ = agent_def
    benchmark_store.init_benchmark(
        "sum-bench", "Summarizer", "summarize repos", name
    )
    benchmark_store.write_case(
        "sum-bench", "CASE-001-sum", STATEMENT, RUBRIC
    )
    return "sum-bench"


@pytest.fixture
def stub_runtime(monkeypatch, bench):
    """把 evaluator 的模型/资源注册打桩,注入 FakeDeepAgent 构造器。

    返回 (agent_name, {chunks/raise_on_stream/side_effect} 配置器)。
    """

    def _install(chunks=None, *, raise_on_stream=None, side_effect=None, construct_error=None):
        def _fake_factory(**kwargs):
            if construct_error is not None:
                raise construct_error
            return FakeDeepAgent(chunks or [_answer_chunk()],
                                 raise_on_stream=raise_on_stream,
                                 side_effect=side_effect)

        monkeypatch.setattr(
            evaluator, "_resolve_model", lambda name=None: (FakeModel([]), "fake-model")
        )
        monkeypatch.setattr(
            evaluator, "_register_sysop", lambda sysop_id: SimpleNamespace(id=sysop_id)
        )
        monkeypatch.setattr(evaluator, "_unregister_sysop", lambda sysop_id: None)
        monkeypatch.setattr(
            "openjiuwen.harness.factory.create_deep_agent", _fake_factory
        )
        return _fake_factory

    return _install


def _run(agent_name, benchmark_id, case_id, **kwargs):
    return asyncio.run(
        evaluator.run_single_case(
            agent_name, benchmark_id, case_id, 1, debug_trace=False, **kwargs
        )
    )


class TestWorkspaceIsolation:
    def test_statement_copied_rubric_absent(self, agent_def, bench):
        name, _ = agent_def
        ws = evaluator._prepare_workspace_for(
            name, "sum-bench", "CASE-001-sum", 1, "he-test-r1-00000001", None
        )
        assert (ws / "README.md").is_file()
        assert STATEMENT in (ws / "README.md").read_text(encoding="utf-8")
        # rubric 内容绝不能出现在工作区(含文件名与文件内容)
        for path in ws.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                assert "## Scoring" not in text
                assert "right output" not in text
        assert not any("rubric" in p.name.lower() for p in ws.rglob("*"))

    def test_unknown_skill_warned_skipped(self, agent_def, bench, tmp_path, caplog):
        name, _ = agent_def
        ws = evaluator._prepare_workspace_for(
            name, "sum-bench", "CASE-001-sum", 1, "he-test-r1-00000002",
            ["no-such-skill"],
        )
        assert not (ws / "skills").exists()


class TestRunClassification:
    def test_ok_even_with_poor_output(self, agent_def, bench, stub_runtime):
        """正常跑完(哪怕产物差)就是可打分:status ok。"""
        name, _ = agent_def
        stub_runtime([_answer_chunk("not helpful at all")])
        result = _run(name, "sum-bench", "CASE-001-sum")
        assert result.status == "ok"
        assert result.failure_code is None
        assert result.final_text == "not helpful at all"
        assert result.duration_ms >= 0

    def test_construct_failure_is_launch_failure(self, agent_def, bench, stub_runtime):
        name, _ = agent_def
        stub_runtime(construct_error=RuntimeError("no model config"))
        with pytest.raises(LaunchFailure, match="no model config"):
            _run(name, "sum-bench", "CASE-001-sum")

    def test_stream_exception_is_failed_launch_failed(self, agent_def, bench, stub_runtime):
        name, _ = agent_def
        stub_runtime(raise_on_stream=RuntimeError("boom"))
        result = _run(name, "sum-bench", "CASE-001-sum")
        assert result.status == "failed"
        assert result.failure_code == "launch_failed"
        assert "boom" in (result.exception or "")

    def test_version_changed_detected(self, agent_def, bench, stub_runtime):
        name, md_path = agent_def
        stub_runtime(side_effect=lambda: md_path.write_text(
            "被外部改坏了\n", encoding="utf-8"))
        result = _run(name, "sum-bench", "CASE-001-sum")
        assert result.status == "failed"
        assert result.failure_code == "version_changed"

    def test_unknown_agent_and_case(self, agent_def, bench, stub_runtime):
        stub_runtime()
        with pytest.raises(HarnessUsageError, match="不存在"):
            _run("ghost", "sum-bench", "CASE-001-sum")
        name, _ = agent_def
        with pytest.raises(HarnessUsageError, match="不存在"):
            _run(name, "sum-bench", "CASE-999-nope")


class TestTrace:
    def test_trace_dump_written(self, agent_def, bench, stub_runtime):
        name, _ = agent_def
        stub_runtime([_answer_chunk("ok")])
        result = asyncio.run(
            evaluator.run_single_case(
                name, "sum-bench", "CASE-001-sum", 1, debug_trace=True
            )
        )
        assert result.trace_file is not None
        assert result.trace_file.is_file()
        assert "run start" in result.trace_file.read_text(encoding="utf-8")


class TestGrader:
    def test_valid_grade(self):
        outcome = asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=FakeModel([_valid_grade_json()]), model_id="fake-model",
        ))
        assert outcome.total == 100.0
        assert outcome.items == {"correctness": 60.0, "clarity": 40.0}
        assert outcome.model_id == "fake-model"

    def test_retry_once_then_valid(self):
        model = FakeModel(["not json at all", _valid_grade_json()])
        outcome = asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=model, model_id="fake-model",
        ))
        assert outcome.total == 100.0
        assert len(model.calls) == 2  # 同证据重试一次,不重跑被测 agent

    def test_twice_invalid_raises_grading_failure(self):
        model = FakeModel(["garbage", "still garbage"])
        with pytest.raises(GradingFailure):
            asyncio.run(grade_run(
                RUBRIC_POINTS, STATEMENT, Path("."), None,
                model=model, model_id="fake-model",
            ))

    def test_item_outside_rubric_rejected(self):
        bad = json.dumps({
            "items": [
                {"item": "correctness", "score": 50.0, "rationale": ""},
                {"item": "hallucinated", "score": 10.0, "rationale": ""},
            ],
            "total": 60.0,
        })
        with pytest.raises(GradingFailure, match="不在 rubric"):
            asyncio.run(grade_run(
                RUBRIC_POINTS, STATEMENT, Path("."), None,
                model=FakeModel([bad, bad]), model_id="fake-model",
            ))

    def test_low_score_is_valid(self):
        """低分(60)是合法打分:所得总分可低于 rubric 满分 100(接受判据需要)。"""
        outcome = asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=FakeModel([_valid_grade_json(60.0)]), model_id="fake-model",
        ))
        assert outcome.total == 60.0
        assert outcome.items == {"correctness": 40.0, "clarity": 20.0}

    def test_total_inconsistent_with_items_rejected(self):
        """total ≠ items 之和 → 拒(items 55+20=75,声明 total 80)。"""
        bad = json.dumps({
            "items": [
                {"item": "correctness", "score": 55.0, "rationale": ""},
                {"item": "clarity", "score": 20.0, "rationale": ""},
            ],
            "total": 80.0,
        })
        with pytest.raises(GradingFailure, match="不一致"):
            asyncio.run(grade_run(
                RUBRIC_POINTS, STATEMENT, Path("."), None,
                model=FakeModel([bad, bad]), model_id="fake-model",
            ))

    def test_score_exceeds_points_rejected(self):
        bad = json.dumps({
            "items": [
                {"item": "correctness", "score": 90.0, "rationale": ""},
                {"item": "clarity", "score": 10.0, "rationale": ""},
            ],
            "total": 100.0,
        })
        with pytest.raises(GradingFailure, match="超出"):
            asyncio.run(grade_run(
                RUBRIC_POINTS, STATEMENT, Path("."), None,
                model=FakeModel([bad, bad]), model_id="fake-model",
            ))

    def test_rubric_never_leaks_to_statement_context(self, tmp_path):
        """grader 校验依赖 rubric 内容,但被测 agent 的 run 上下文不含 rubric
        (工作区隔离已在 TestWorkspaceIsolation 断言;此处仅确认 prompt 结构)。"""
        (tmp_path / "README.md").write_text(STATEMENT, encoding="utf-8")
        model = FakeModel([_valid_grade_json()])
        asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, tmp_path, None,
            model=model, model_id="fake-model",
        ))
        prompt = model.calls[0]
        assert "correctness | 60" in prompt  # rubric 表只进 grader
        assert "| item | points |" in prompt
        assert STATEMENT in prompt

    def test_think_prefixed_fenced_json_parsed(self):
        """<think>...</think> 前缀 + ```json 围栏 → 正常解析(thinking-mode LLM)。"""
        raw = "<think>先想想评分依据……</think>\n```json\n" + _valid_grade_json() + "\n```"
        outcome = asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=FakeModel([raw]), model_id="fake-model",
        ))
        assert outcome.total == 100.0

    def test_unpaired_think_block_fails_cleanly(self):
        """未闭合 <think>(截断输出)剥除后无 JSON → GradingFailure,不误解析。"""
        raw = "<thinking>还没想完就断了\n" + _valid_grade_json()
        with pytest.raises(GradingFailure):
            asyncio.run(grade_run(
                RUBRIC_POINTS, STATEMENT, Path("."), None,
                model=FakeModel([raw, raw]), model_id="fake-model",
            ))

    def test_think_inside_fence_then_json(self):
        """<think> 出现在 ```json 围栏内、JSON 之前 → 同样剥除后解析。"""
        raw = "```json\n<think>想一下</think>\n" + _valid_grade_json() + "\n```"
        outcome = asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=FakeModel([raw]), model_id="fake-model",
        ))
        assert outcome.total == 100.0

    def test_run_stats_rendered_in_prompt(self):
        """trace_stats 的机器统计注入打分员上下文(token 效率 rubric 精算)。"""
        model = FakeModel([_valid_grade_json()])
        asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=model, model_id="fake-model",
            run_stats={
                "input_tokens": 1000,
                "output_tokens": 250,
                "tool_calls": 3,
                "final_text_chars": 620,
            },
        ))
        prompt = model.calls[0]
        assert "精确统计" in prompt
        assert "输入 tokens: 1000" in prompt
        assert "输出 tokens: 250" in prompt
        assert "工具调用次数: 3" in prompt
        assert "最终回复字符数" in prompt
        assert "620" in prompt

    def test_run_stats_absent_renders_placeholder(self):
        model = FakeModel([_valid_grade_json()])
        asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), None,
            model=model, model_id="fake-model",
        ))
        assert "(无统计)" in model.calls[0]


class TestToolsRegistry:
    def test_unknown_warned_and_skipped(self, caplog):
        built = build_named_tools(["Read", "Bash", "NoSuchTool"],
                                  agent_id="he-test-tools")
        assert built == []
        assert any("NoSuchTool" in r.getMessage() for r in caplog.records)

    def test_file_tools_auto_skipped(self):
        # 文件工具由 SysOperation 派生,注册表不构造
        assert build_named_tools(["Read", "Write", "Edit", "Bash"],
                                 agent_id="he-test-tools") == []


class TestAgentToolRegistration:
    """评测 agent 工具注册回归:曾因传 ToolCard 而非实例 + rails 缺
    SysOperationRail,导致被测 agent 只有 fetch_webpage 且调用报
    "Tool instance not found in resource_mgr"、读不到工作区 README。"""

    def test_factory_receives_tool_instances_and_sysop_rail(
        self, monkeypatch, bench, agent_def
    ):
        from openjiuwen.core.foundation.tool import Tool, ToolCard
        from openjiuwen.harness.rails import SysOperationRail

        name, _ = agent_def
        captured = {}

        def _fake_factory(**kwargs):
            captured.update(kwargs)
            return FakeDeepAgent([_answer_chunk()])

        monkeypatch.setattr(
            evaluator, "_resolve_model", lambda name=None: (FakeModel([]), "fake-model")
        )
        monkeypatch.setattr(evaluator, "_register_sysop", lambda sid: SimpleNamespace(id=sid))
        monkeypatch.setattr(evaluator, "_unregister_sysop", lambda sid: None)
        monkeypatch.setattr("openjiuwen.harness.factory.create_deep_agent", _fake_factory)
        _run(name, "sum-bench", "CASE-001-sum")

        # 工具必须是 Tool 实例(factory 只注册实例进 resource_mgr);
        # 传纯 ToolCard 会得到"可见但不可调用"的工具
        tools = captured["tools"]
        assert tools, "tools 不应为空(frontmatter '*' 应构造出 web 工具实例)"
        assert all(isinstance(t, Tool) and not isinstance(t, ToolCard) for t in tools)
        # rails 必须含 SysOperationRail(文件工具宿主;factory 默认列表不含它)
        assert any(isinstance(r, SysOperationRail) for r in captured["rails"])


class TestEvalRunRubricParse:
    """eval-run 路径回归:传给 grade_run 的必须是解析后的 rubric points dict。

    曾有的缺陷:eval-run 把 raw rubric 文本直接传给期望 dict 的 grade_run,
    打分阶段 AttributeError(str 无 items);pipeline 路径已修复,此路径为
    独立实现,须同样解析(PR 顺带修复第 2 条)。
    """

    def test_eval_run_passes_parsed_rubric(self, tmp_path, agent_def, monkeypatch):
        from jiuwenswarm.harness_evolve import grader as grader_module
        from jiuwenswarm.harness_evolve import pipeline
        from jiuwenswarm.harness_evolve.commands.eval_cmds import _run_eval_run

        name, _ = agent_def
        bid, cid = "bench-1", "CASE-001-fmt"
        benchmark_store.init_benchmark(bid, "t", "d", name)
        rubric_md = (
            "## Scoring\n"
            "| item | points | description |\n"
            "| ---- | ------ | ----------- |\n"
            "| correctness | 60 | 正确性 |\n"
            "| format | 40 | 格式 |\n"
        )
        benchmark_store.write_case(bid, cid, "阅读 README 后完成审查任务。", rubric_md)

        async def fake_run(*args, **kwargs):
            return SimpleNamespace(
                status="ok", failure_code=None, session_id="sess-1",
                duration_ms=1, cost=None, final_text="产物",
                workspace=str(tmp_path), trace_file=None, exception=None,
                tokens={"output_tokens": 42},
            )

        captured = {}

        async def spy_grade(rubric_points, statement, workspace, trace_file, *,
                            model, model_id, run_stats=None):
            captured["rubric_points"] = rubric_points
            captured["run_stats"] = run_stats
            return SimpleNamespace(
                total=100.0,
                items={"correctness": 60.0, "format": 40.0},
                model_id=model_id,
            )

        monkeypatch.setattr(evaluator, "run_single_case", fake_run)
        monkeypatch.setattr(pipeline, "_effective_model_name", lambda a, m: "fake")
        monkeypatch.setattr(pipeline, "_resolve_model_cached", lambda m: (None, "fake-model"))
        monkeypatch.setattr(grader_module, "grade_run", spy_grade)

        result = _run_eval_run(SimpleNamespace(
            agent=name, benchmark=bid, case=cid, model=None, no_trace=True,
        ))
        # 核心断言:raw rubric 文本被解析成了 dict,而不是原样传 str
        assert isinstance(captured["rubric_points"], dict)
        assert set(captured["rubric_points"]) == {"correctness", "format"}
        # token 统计透传给打分员(精算 rubric 用)
        assert captured["run_stats"] == {"output_tokens": 42}
        assert result["status"] == "ok"
        assert result["score"] == 100.0
