# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""轨迹关键点提取:把整条 dump 压缩成结构化行为摘要(≤ ~1200 字符)。

动机:打分员此前只拿轨迹**末尾 4000 字符原文**当行为证据——长轨迹
(多轮搜索/巨型工具输出)的早期关键事件(重复核查、报错、空转)落在
窗口外,判分靠猜。本模块全量扫描后输出:
- 轮次/工具/错误概览(LLM 轮数、工具调用次数、去重后错误数)
- 工具调用分布(每工具次数;同参数重复调用压缩为 "N 次相同调用")
- 报错摘要(去重,每条截断)
- 重复动作检测(同 URL/查询连续重试 ≥3 次 → 疑似空转标记)
- 结局(最后一轮 result_type + 最终输出前若干字符)

纯本地文本解析,零模型调用;失败/文件缺失返回 None(不阻塞评测)。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from jiuwenswarm.harness_evolve.trace_stats import extract_trace_stats

_MAX_SUMMARY_CHARS = 1200
_ERR_PREFIX_CHARS = 200
_FINAL_TEXT_CHARS = 300

_TOOL_INVOKE_RE = re.compile(r'"invokeType":\s*"plugin"[^}]*?"name":\s*"([^"]+)"')
_URL_RE = re.compile(r'"url":\s*"([^"]+)"')
_QUERY_RE = re.compile(r'"query":\s*"([^"]+)"')
_FINAL_RESULT_RE = re.compile(r'"result_type":\s*"([a-z_]+)"')
_TEXT_OUTPUT_RE = re.compile(r'"output":\s*"(.*?)",\s*"result_type"', re.DOTALL)

# 报错特征行(工具输出里的失败标志)。注意不含裸 "error":论文标题
# (如 "Error Bound Conditions")等正常内容会误命中。
_ERROR_MARKERS = ("[error]", "failed", "404", "403", "not found",
                  "traceback", "exception")


def _final_text_of(line: str) -> str:
    m = _TEXT_OUTPUT_RE.search(line)
    if not m:
        return ""
    text = m.group(1)
    # 轨迹行内 JSON 的引号被转义过,简单还原
    return text.replace('\\"', '"').replace("\\n", "\n")


def extract_trace_keypoints(trace_file: Path | str | None) -> str | None:
    """把轨迹压缩为打分员可读的行为摘要;无数据 → None。"""
    if not trace_file:
        return None
    path = Path(trace_file)
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    llm_turns = 0
    tool_names: list[str] = []
    tool_args: list[str] = []  # url/query 归一化签名,用于重复检测
    errors: list[str] = []
    seen_errors: set[str] = set()
    final_lines: list[str] = []
    in_reasoning = False
    for line in lines:
        if line.startswith("["):
            in_reasoning = False  # 回到结构行(下一段 marker)
        if in_reasoning:
            continue  # 推理块续行("  | " 开头的后续行)不算报错证据
        if "category=reasoning" in line:
            in_reasoning = True
            continue
        if "category=context_usage" in line:
            llm_turns += 1
        if '"invokeType": "plugin"' in line:
            if '"status": "start"' in line:
                m = _TOOL_INVOKE_RE.search(line)
                if m:
                    tool_names.append(m.group(1))
                sig = _arg_signature(line)
                if sig:
                    tool_args.append(sig)
                continue  # start 行无结果,不是报错
        # 结局:最后一条文本输出(先捕获,再判断报错——结局行含 "error" 字样)
        if '"result_type"' in line:
            final_lines = [line]
        elif "category=text" in line and '"output"' in line:
            final_lines.append(line)
        # 报错行:工具 finish 输出的真实失败标志(去重,截断)
        lower = line.lower()
        if any(marker.lower() in lower for marker in _ERROR_MARKERS):
            if '"result_type"' in line:
                continue  # 结局行不是工具报错
            # `"error": null` 是字段占位,不是真实失败;若无其它真实标志则跳过
            if ('"error": null' in line or '"error": ""' in line) and not any(
                m in lower
                for m in ("[error]", "failed", "404", "403", "not found",
                          "traceback", "exception")
            ):
                continue
            snippet = line.strip()
            if _TOOL_INVOKE_RE.search(line):
                idx = snippet.find('"outputs"')
                if idx < 0:
                    continue
                snippet = snippet[idx:]
            if len(snippet) > _ERR_PREFIX_CHARS:
                snippet = snippet[:_ERR_PREFIX_CHARS] + "…"
            if snippet not in seen_errors:
                seen_errors.add(snippet)
                errors.append(snippet)

    stats = extract_trace_stats(path) or {}
    out_lines: list[str] = []
    tool_counter = Counter(tool_names)
    if tool_counter:
        dist = ", ".join(f"{name}×{count}" for name, count in tool_counter.most_common(8))
        out_lines.append(f"工具调用 {sum(tool_counter.values())} 次: {dist}")
    # 重复动作检测:同一签名连续出现 ≥3 次
    runs = _longest_repeat_run(tool_args)
    if runs:
        out_lines.append("疑似空转: " + "; ".join(runs))
    if llm_turns:
        out_lines.append(f"LLM 轮数: {llm_turns}")
    if stats:
        out_lines.append(
            f"输入 {stats.get('input_tokens', 0)} / 输出 {stats.get('output_tokens', 0)} tokens"
        )
    if errors:
        shown = errors[:5]
        out_lines.append(f"报错 {len(errors)} 类(示前 {len(shown)}):")
        for err in shown:
            out_lines.append(f"  - {err}")
    if final_lines:
        last = final_lines[-1]
        m = _FINAL_RESULT_RE.search(last)
        result_type = m.group(1) if m else "unknown"
        final_text = _final_text_of(last)[:_FINAL_TEXT_CHARS]
        out_lines.append(f"结局: result_type={result_type}")
        if final_text.strip():
            out_lines.append(f"  最终输出(截断): {final_text}")
    if not out_lines:
        return None
    summary = "\n".join(out_lines)
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "\n…(摘要截断)"
    return summary


def _arg_signature(line: str) -> str:
    """提取工具调用的 url/query 作为重复检测签名。"""
    m = _URL_RE.search(line) or _QUERY_RE.search(line)
    if not m:
        return ""
    value = m.group(1)
    if len(value) > 160:
        value = value[:160] + "…"
    return value


def _longest_repeat_run(args: list[str]) -> list[str]:
    """同签名连续重复 ≥3 次的区间,输出 "签名 ×N 次"。"""
    runs: list[str] = []
    if not args:
        return runs
    prev, count = args[0], 1
    for cur in args[1:]:
        if cur and cur == prev:
            count += 1
        else:
            if count >= 3 and prev:
                runs.append(f"「{prev}」连续 {count} 次")
            prev, count = cur, 1
    if count >= 3 and prev:
        runs.append(f"「{prev}」连续 {count} 次")
    return runs[:5]
