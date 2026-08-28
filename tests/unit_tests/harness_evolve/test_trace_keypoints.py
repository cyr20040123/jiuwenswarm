# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""trace_keypoints 测试:轨迹行为摘要提取与打分员集成。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jiuwenswarm.harness_evolve.trace_keypoints import extract_trace_keypoints

TRACE = """========== run start ==========
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=100 output_tokens=10 total_tokens=110 model_name=MiniMax-M3
[INFO] mode=agent source=main category=other
  | {"invokeId": "a1", "status": "start", "invokeType": "plugin", "name": "fetch_webpage", "inputs": {"url": "https://api.crossref.org/works/10.5555/3295222.3295349"}}
[INFO] mode=agent source=main category=other
  | {"invokeId": "a1", "status": "finish", "invokeType": "plugin", "name": "fetch_webpage", "outputs": {"outputs": "[ERROR]: failed to fetch webpage, reason='HTTP 404'"}}
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=200 output_tokens=20 total_tokens=220 model_name=MiniMax-M3
[INFO] mode=agent source=main category=other
  | {"invokeId": "a2", "status": "start", "invokeType": "plugin", "name": "fetch_webpage", "inputs": {"url": "https://api.crossref.org/works/10.5555/3295222.3295349"}}
[INFO] mode=agent source=main category=other
  | {"invokeId": "a2", "status": "finish", "invokeType": "plugin", "name": "fetch_webpage", "outputs": {"outputs": "[ERROR]: failed to fetch webpage, reason='HTTP 404'"}}
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=300 output_tokens=30 total_tokens=330 model_name=MiniMax-M3
[INFO] mode=agent source=main category=other
  | {"invokeId": "a3", "status": "start", "invokeType": "plugin", "name": "fetch_webpage", "inputs": {"url": "https://api.crossref.org/works/10.5555/3295222.3295349"}}
[INFO] mode=agent source=main category=other
  | {"invokeId": "a3", "status": "finish", "invokeType": "plugin", "name": "fetch_webpage", "outputs": {"outputs": "[ERROR]: failed to fetch webpage, reason='HTTP 404'"}}
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=400 output_tokens=15 total_tokens=415 model_name=MiniMax-M3
[INFO] mode=agent source=main category=other
  | {"invokeId": "a4", "status": "start", "invokeType": "plugin", "name": "fetch_webpage", "inputs": {"url": "https://api.semanticscholar.org/graph/v1/paper/DOI:10.5555/3295222.3295349"}}
[INFO] mode=agent source=main category=other
  | {"invokeId": "a4", "status": "finish", "invokeType": "plugin", "name": "fetch_webpage", "outputs": {"outputs": "[ERROR]: failed to fetch webpage, reason='Paper not found'"}}
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=500 output_tokens=200 total_tokens=700 model_name=MiniMax-M3
[INFO] mode=agent source=main category=text
  | {"output": "Max iterations reached without completion", "result_type": "error"}
========== run end ==========
"""


class TestExtractKeypoints:
    def test_summary_covers_distribution_repeat_errors_final(self, tmp_path):
        trace = tmp_path / "dump.txt"
        trace.write_text(TRACE, encoding="utf-8")
        summary = extract_trace_keypoints(trace)
        assert summary is not None
        assert "fetch_webpage×4" in summary
        assert "疑似空转" in summary  # 同一 crossref URL 连续 3 次
        assert "报错" in summary
        assert "result_type=error" in summary
        assert "Max iterations reached without completion" in summary
        assert "LLM 轮数: 5" in summary

    def test_missing_file_returns_none(self, tmp_path):
        assert extract_trace_keypoints(tmp_path / "nope.txt") is None

    def test_none_input_returns_none(self):
        assert extract_trace_keypoints(None) is None

    def test_empty_trace_returns_none(self, tmp_path):
        trace = tmp_path / "dump.txt"
        trace.write_text("nothing here\n", encoding="utf-8")
        assert extract_trace_keypoints(trace) is None


class TestGraderIntegration:
    def test_grader_prompt_contains_keypoints(self, tmp_path):
        """打分员 prompt 的行为证据段包含关键点摘要(而非仅尾巴原文)。"""
        from jiuwenswarm.harness_evolve.grader import grade_run
        from tests.unit_tests.harness_evolve.test_evaluator import (
            FakeModel,
            RUBRIC_POINTS,
            STATEMENT,
            _valid_grade_json,
        )

        trace = tmp_path / "dump.txt"
        trace.write_text(TRACE, encoding="utf-8")
        model = FakeModel([_valid_grade_json()])
        asyncio.run(grade_run(
            RUBRIC_POINTS, STATEMENT, Path("."), trace,
            model=model, model_id="fake-model",
        ))
        prompt = model.calls[0]
        assert "行为证据" in prompt
        assert "疑似空转" in prompt
        assert "轨迹末尾原文" in prompt
