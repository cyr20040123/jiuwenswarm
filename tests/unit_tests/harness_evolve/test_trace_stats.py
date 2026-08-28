# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""trace_stats 测试:从轨迹 dump 提取 token/工具调用机器统计。"""

from __future__ import annotations

from jiuwenswarm.harness_evolve.trace_stats import extract_trace_stats

TRACE = """========== run start ==========
input=Read README.md in the current Workspace
[DEBUG] mode=agent source=main category=reasoning
  | 让我先读取 README。
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=100 output_tokens=20 total_tokens=120 model_name=MiniMax-M3
[INFO] mode=agent source=main category=other
  | {"invokeId": "a1", "status": "start", "invokeType": "plugin", "name": "fetch_webpage"}
[INFO] mode=agent source=main category=other
  | {"invokeId": "a1", "status": "finish", "invokeType": "plugin", "name": "fetch_webpage"}
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=300 output_tokens=55 total_tokens=355 model_name=MiniMax-M3
[INFO] mode=agent source=main category=other
  | {"invokeId": "b1", "status": "start", "invokeType": "plugin", "name": "fetch_webpage"}
[INFO] mode=agent source=main category=other
  | {"invokeId": "b1", "status": "finish", "invokeType": "plugin", "name": "fetch_webpage"}
[DEBUG] mode=agent source=main category=context_usage
  | input_tokens=200 output_tokens=15 total_tokens=215 model_name=MiniMax-M3
========== run end ==========
"""


class TestExtractTraceStats:
    def test_sums_tokens_and_counts_tool_calls(self, tmp_path):
        trace = tmp_path / "dump.txt"
        trace.write_text(TRACE, encoding="utf-8")
        stats = extract_trace_stats(trace)
        assert stats == {
            "input_tokens": 600,
            "output_tokens": 90,
            "tool_calls": 2,
        }

    def test_missing_file_returns_none(self, tmp_path):
        assert extract_trace_stats(tmp_path / "nope.txt") is None

    def test_none_input_returns_none(self):
        assert extract_trace_stats(None) is None

    def test_no_markers_returns_none(self, tmp_path):
        trace = tmp_path / "dump.txt"
        trace.write_text("nothing useful here\n", encoding="utf-8")
        assert extract_trace_stats(trace) is None

    def test_malformed_counts_ignored(self, tmp_path):
        trace = tmp_path / "dump.txt"
        trace.write_text(
            "[DEBUG] category=context_usage\n"
            "  | input_tokens=abc output_tokens=12\n",
            encoding="utf-8",
        )
        stats = extract_trace_stats(trace)
        # 非法 input 被忽略,合法 output 计入
        assert stats == {"input_tokens": 0, "output_tokens": 12, "tool_calls": 0}
