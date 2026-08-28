# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rubric -> LLM 打分器(仿 skilldev/evaluate_stage 的 prompt 约定 + JSON 后解析)。

契约(与 Penguin agent-evaluation 对齐,简化掉 run_subagent 纯 YAML 协议):
- 输入 = statement(公开) + rubric(私有,只进 grader 上下文,绝不进被测 agent)+
  工作区产物清单 + trace 末尾;输出 = 单段 JSON ``{"items": [...], "total": n}``。
- 校验:item 名必须 ⊆ rubric 项;每项 score ∈ [0, points];total 必须 == 各项
  score 之和(即所得总分,∈ [0, 100],可低于 rubric 满分 100)。
- 校验失败:用**同一份证据**重试一次(不重跑被测 agent);仍失败 -> GradingFailure。
- rubric 的解析复用 schemas.parse_rubric_points(``## Scoring`` 表 + 和 == 100)。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from jiuwenswarm.harness_evolve.errors import GradingFailure

logger = logging.getLogger(__name__)

# 思考块前缀(thinking-mode LLM 习惯):成对 <think>...</think> 或未闭合
# <think>...(截断输出);<thinking> 变体一并处理。惰性匹配到第一个闭合标签
# 或串尾——未闭合时其后内容视为思考内容,无 JSON 可救。
_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|$)", re.DOTALL)

_GRADER_PROMPT_TEMPLATE = """你是 benchmark 的独立打分员。请严格按 rubric 对被测 agent 的一次评测进行打分。

## 被测任务(statement)
{statement}

## 评分标准(rubric,保密,勿外传)
| item | points | description |
| ---- | ------ | ----------- |
{rubric_rows}

## 被测 agent 的工作区产物(workspace 中的文件清单与内容摘要)
{artifacts}

## 被测 agent 的精确统计(机器提取,可直接引用)
{run_stats}

## 被测 agent 的行为证据(关键点摘要 + 轨迹末尾原文)
{trace_evidence}

## 输出要求
只输出一个 JSON 对象,不要输出任何其它文字:
{{
  "items": [{{"item": "<rubric项名>", "score": <0到该项points的浮点数,最多2位小数>, "rationale": "<一句依据>"}}],
  "total": <各项 score 之和(所得总分),最多2位小数>
}}
"""


@dataclass
class GradingOutcome:
    """一次打分的结构化结果。"""

    total: float
    items: dict[str, float]  # rubric item -> score
    rationale: dict[str, str]  # rubric item -> 依据
    model_id: str
    cost: float | None = None
    duration_ms: int = 0


@dataclass
class _Draft:
    total: float
    items: dict[str, float]
    rationale: dict[str, str]


def _rubric_rows(rubric_points: dict[str, int]) -> str:
    return "\n".join(
        f"| {name} | {points} | 满分 {points} 分 |"
        for name, points in rubric_points.items()
    )


def _artifact_summary(workspace: Path, cap_chars: int = 3000) -> str:
    """枚举工作区文件并给出前若干字符内容摘要(用于打分依据)。"""
    lines: list[str] = []
    budget = cap_chars
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            lines.append(f"- {rel}(空)")
            continue
        chunk = text.strip()[: max(200, budget)]
        budget -= len(chunk)
        lines.append(f"- {rel}:\n{chunk}")
        if budget <= 0:
            lines.append("- ...(产物摘要已截断)")
            break
    return "\n".join(lines) or "(工作区无产物文件)"


def _trace_tail(trace_file: Path | None, max_chars: int = 2000) -> str:
    if trace_file is None or not trace_file.is_file():
        return "(无 trace)"
    try:
        text = trace_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(trace 读取失败)"
    return text[-max_chars:] or "(trace 为空)"


def _trace_evidence(trace_file: Path | None) -> str:
    """行为证据 = 全轨迹关键点摘要 + 末尾原文(细节核对)。"""
    from jiuwenswarm.harness_evolve.trace_keypoints import extract_trace_keypoints

    keypoints = extract_trace_keypoints(trace_file)
    tail = _trace_tail(trace_file)
    if keypoints:
        return f"{keypoints}\n\n--- 轨迹末尾原文(细节证据) ---\n{tail}"
    return tail


def _render_run_stats(run_stats: dict | None) -> str:
    """把 trace_stats 的机器统计渲染进打分员上下文(无统计则显式标注)。"""
    if not isinstance(run_stats, dict):
        return "(无统计)"
    parts: list[str] = []
    if isinstance(run_stats.get("input_tokens"), int):
        parts.append(f"- 输入 tokens: {run_stats['input_tokens']}")
    if isinstance(run_stats.get("output_tokens"), int):
        parts.append(f"- 输出 tokens: {run_stats['output_tokens']}")
    if isinstance(run_stats.get("tool_calls"), int):
        parts.append(f"- 工具调用次数: {run_stats['tool_calls']}")
    if isinstance(run_stats.get("final_text_chars"), int):
        parts.append(
            f"- 最终回复字符数(约等于 tokens,中英混合): {run_stats['final_text_chars']}"
        )
    if not parts:
        return "(无统计)"
    return "\n".join(parts) + "\n(若 rubric 含 token/长度上限或效率项,请按上述数字精确执行阶梯扣分)"


def _build_prompt(
    rubric_points: dict[str, int],
    statement: str,
    workspace: Path,
    trace_file: Path | None,
    run_stats: dict | None = None,
) -> str:
    return _GRADER_PROMPT_TEMPLATE.format(
        statement=statement,
        rubric_rows=_rubric_rows(rubric_points),
        artifacts=_artifact_summary(workspace),
        run_stats=_render_run_stats(run_stats),
        trace_evidence=_trace_evidence(trace_file),
    )


def _validate_draft_data(data, rubric_points: dict[str, int]) -> _Draft:
    """校验已解析的 dict;失败抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("打分 JSON 顶层必须是对象")
    items_raw = data.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("items 缺失或为空")
    total_raw = data.get("total")
    if not isinstance(total_raw, (int, float)):
        raise ValueError("total 缺失或非数字")

    items: dict[str, float] = {}
    rationale: dict[str, str] = {}
    for entry in items_raw:
        if not isinstance(entry, dict):
            raise ValueError("item 条目必须是对象")
        name = entry.get("item")
        if not isinstance(name, str) or name not in rubric_points:
            raise ValueError(f"item 不在 rubric 中: {name!r}")
        points = rubric_points[name]
        score = entry.get("score")
        if not isinstance(score, (int, float)):
            raise ValueError(f"{name} 的 score 非数字")
        if not (0 <= score <= points):
            raise ValueError(f"{name} 的 score {score} 超出 [0, {points}]")
        items[name] = round(float(score), 2)
        reason = entry.get("rationale")
        rationale[name] = str(reason) if reason else ""

    total = round(float(total_raw), 2)
    if abs(total - round(sum(items.values()), 2)) > 0.01:
        raise ValueError(
            f"total {total} 与 items 之和 {round(sum(items.values()), 2)} 不一致"
        )
    if not 0.0 <= total <= 100.0:
        raise ValueError(f"total {total} 必须 ∈ [0, 100]")
    if len(items) != len(rubric_points):
        missing = set(rubric_points) - set(items)
        raise ValueError(f"漏打分项: {sorted(missing)}")
    return _Draft(total=total, items=items, rationale=rationale)


def _extract_via_regex(text: str, rubric_points: dict[str, int]):
    """JSON parse 失败时的回退:逐项用正则提取 item/score/rationale。

    适用于 LLM 在字符串值内未转义 " 导致 JSON 整体解析失败的场景
    (如 rationale 字段里出现违反"简洁无冗余"要求 这种 ASCII 嵌套引号)。
    rationale 可能因 " 被截断(尽力抽取)。
    """
    items: dict[str, float] = {}
    rationale: dict[str, str] = {}
    for name in rubric_points:
        # 抽 score
        pat = rf'"item"\s*:\s*"{re.escape(name)}"\s*,\s*"score"\s*:\s*([\d.]+)'
        m = re.search(pat, text)
        if m:
            try:
                score = float(m.group(1))
                if 0 <= score <= rubric_points[name]:
                    items[name] = round(score, 2)
            except ValueError:
                pass
        # 抽 rationale(尽力,可能因未转义 " 被截断)
        pat2 = (
            rf'"item"\s*:\s*"{re.escape(name)}"[\s\S]*?'
            r'"rationale"\s*:\s*"([\s\S]*?)"\s*[,}]'
        )
        m2 = re.search(pat2, text)
        if m2:
            rationale[name] = m2.group(1)[:500]

    if len(items) != len(rubric_points):
        # 缺项视为失败,保持原严格行为
        return None
    total_m = re.search(r'"total"\s*:\s*([\d.]+)', text)
    if not total_m:
        return None
    total = round(float(total_m.group(1)), 2)
    if not 0.0 <= total <= 100.0:
        return None
    # total 与 items 之和差距大时,以 items 之和为准
    items_sum = round(sum(items.values()), 2)
    if abs(total - items_sum) > 0.01:
        total = items_sum

    return _Draft(total=total, items=items, rationale=rationale)


def _parse_draft(raw: str, rubric_points: dict[str, int]) -> _Draft:
    """解析并校验打分 JSON;任何不符 -> ValueError(由调用方重试)。

    解析策略(逐步退化):
    1. 剥离 <think>...</think> 块(thinking-mode LLM)
    2. 尝试多种文本提取形态后 json.loads
    3. 若 JSON 整体解析失败(常见原因:字符串值内未转义 "),
       用正则按 rubric 项名逐项提取 score 作为回退
    """
    text = raw.strip()
    # 容忍带 <think>...</think> 前缀的输出(thinking-mode LLM 习惯)
    # 注意:此函数只处理 raw 输出字符串,与 reasoning_content 字段分离;不影响后者抽取。
    text = _THINK_BLOCK_RE.sub("", text).strip()

    # 策略 1:多种文本提取形态后 json.loads
    candidates = [text]
    if text.startswith("```"):
        stripped = text.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        candidates.append(stripped)
    last_err: Exception | None = None
    for cand in candidates:
        try:
            data = json.loads(cand.strip())
        except json.JSONDecodeError as e:
            last_err = e
            continue
        # JSON 结构合法:语义校验失败直接抛,不进入正则回退——正则回退会
        # 静默丢弃越界项/把 total 修正为 items 之和,破坏严格契约
        # (item ⊆ rubric、score ∈ [0, points]、total == items 之和)。
        return _validate_draft_data(data, rubric_points)

    # 策略 2:正则回退(仅 JSON 整体解析失败时,如字符串值内未转义 ")
    fallback = _extract_via_regex(text, rubric_points)
    if fallback is not None:
        return fallback

    # 都失败
    if last_err is not None:
        raise last_err
    raise ValueError("no parseable JSON output")


async def _call_model(model, prompt: str) -> str:
    """单次打分调用;返回模型原始输出文本。"""
    message = await model.invoke(prompt)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


async def grade_run(
    rubric_points: dict[str, int],
    statement: str,
    workspace: Path,
    trace_file: Path | None,
    *,
    model,
    model_id: str,
    run_stats: dict | None = None,
) -> GradingOutcome:
    """对一次 run 打分:rubric 私有,JSON 契约,坏输出同证据重试一次。

    Args:
        rubric_points: ``parse_rubric_points`` 的产物(和必须 == 100)。
        statement: case 的公开 statement 文本。
        workspace: 被测 run 的工作区(产物证据,只读)。
        trace_file: trace dump 路径(可为 None)。
        model: openjiuwen Model 实例(测试可注入 recorder 假模型)。
        model_id: 记录到 Evaluation 的模型标识。
        run_stats: trace_stats 的机器统计(input/output tokens、工具调用次数、
            最终回复字符数);注入打分员上下文供 token 效率类 rubric 精算。

    Returns:
        GradingOutcome。

    Raises:
        GradingFailure: 两次尝试输出均不合法(被测 agent 不重跑)。
    """
    prompt = _build_prompt(rubric_points, statement, workspace, trace_file, run_stats)
    draft: _Draft | None = None
    last_error = ""
    started = time.monotonic()
    for attempt in range(2):
        raw = await _call_model(model, prompt)
        try:
            draft = _parse_draft(raw, rubric_points)
            break
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"attempt {attempt + 1}: {exc}"
            logger.warning("[harness-evolve] grader 输出非法(%s),同证据重试", last_error)
    if draft is None:
        raise GradingFailure(f"grader 两次输出均不合法: {last_error}")
    return GradingOutcome(
        total=draft.total,
        items=draft.items,
        rationale=draft.rationale,
        model_id=model_id,
        cost=None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


__all__ = ["GradingOutcome", "grade_run"]
