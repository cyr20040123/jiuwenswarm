# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""轨迹统计提取:从 debug_trace dump 解析机器可算的精确指标。

用途:token 效率类 rubric(如 CASE-009 的"总输出 ≤ 600 tokens")不再靠
打分员目测——评估器跑完后用本模块从轨迹里**精确求和** input/output tokens、
统计工具调用次数,作为结构化数字注入打分员 prompt 与评测证据。

轨迹格式(server/runtime/debug_trace 落盘):每轮 LLM 调用有一行
``category=context_usage``(含 input_tokens=… output_tokens=…),每次工具
调用有 ``"invokeType": "plugin"`` 的 start/finish JSON 行。解析失败/文件
缺失一律返回 None(不阻塞评测)。
"""

from __future__ import annotations

import re
from pathlib import Path

_CONTEXT_USAGE_MARKER = "category=context_usage"
_TOOL_START_MARKER = '"invokeType": "plugin"'
_STATUS_START_MARKER = '"status": "start"'
_INPUT_RE = re.compile(r"input_tokens=(\d+)")
_OUTPUT_RE = re.compile(r"output_tokens=(\d+)")


def extract_trace_stats(trace_file: Path | str | None) -> dict | None:
    """从轨迹 dump 提取 {input_tokens, output_tokens, tool_calls}。

    Returns:
        dict;文件缺失/不可读/无有效数据 → None。永不抛异常。
    """
    if not trace_file:
        return None
    path = Path(trace_file)
    if not path.is_file():
        return None
    input_tokens = 0
    output_tokens = 0
    tool_calls = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    def _accum(line: str) -> None:
        nonlocal input_tokens, output_tokens
        m_in = _INPUT_RE.search(line)
        m_out = _OUTPUT_RE.search(line)
        if m_in:
            input_tokens += int(m_in.group(1))
        if m_out:
            output_tokens += int(m_out.group(1))

    # 轨迹格式:``category=context_usage`` 行后跟一行 ``| input_tokens=… output_tokens=…``;
    # 同一行内带数字的格式也一并兼容。
    prev_usage = False
    for line in lines:
        if _CONTEXT_USAGE_MARKER in line:
            _accum(line)
            prev_usage = True
            continue
        if prev_usage:
            _accum(line)
            prev_usage = False
            continue
        if _TOOL_START_MARKER in line and _STATUS_START_MARKER in line:
            tool_calls += 1
    if input_tokens == 0 and output_tokens == 0 and tool_calls == 0:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
    }
