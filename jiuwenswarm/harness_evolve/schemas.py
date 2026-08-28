# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""数据 schema 与校验(独立定义,不依赖 skilldev/schema.py)。

语义对齐 Penguin:rubric 打分项总和必须恰好 100;score ∈ [0,100] 两位小数;
cost 可为 null(原始精度 float);duration_ms 非负整数;time 为 UTC ISO-8601;
scoreboard 条目无 max_score/aggregate 字段;append 仅允许 frozen benchmark,
且 version 严格大于最后一条。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import yaml

from jiuwenswarm.harness_evolve.errors import BenchmarkValidationError, ScoreboardError

__all__ = [
    "AGENT_NAME_RE",
    "BENCHMARK_ID_RE",
    "CASE_ID_RE",
    "RunScore",
    "CaseScore",
    "Evaluation",
    "Scoreboard",
    "BenchmarkConfig",
    "parse_rubric_points",
    "require_rubric_sum_100",
    "run_score_from_dict",
    "case_score_from_dict",
    "evaluation_from_dict",
    "scoreboard_from_dict",
    "validate_scoreboard",
    "validate_evaluation_append",
    "scoreboard_to_yaml",
    "scoreboard_from_yaml",
    "now_utc_iso",
]

AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,50}$")
BENCHMARK_ID_RE = re.compile(r"^[a-z0-9-]{3,64}$")
CASE_ID_RE = re.compile(r"^CASE-\d{3}-[a-zA-Z0-9_-]+$")

_FORBIDDEN_EVALUATION_KEYS = ("max_score", "aggregate")
_RUBRIC_SECTION_RE = re.compile(r"^##\s+Scoring\b", re.MULTILINE | re.IGNORECASE)
_RUBRIC_TABLE_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d+(?:\.\d+)?)\s*\|")


def now_utc_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串(以 Z 结尾)。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_score(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreboardError(f"{where}: score 必须是数字,得到 {value!r}")
    value = float(value)
    if round(value, 2) != value:
        raise ScoreboardError(f"{where}: score 最多两位小数,得到 {value}")
    if not 0.0 <= value <= 100.0:
        raise ScoreboardError(f"{where}: score 必须 ∈ [0, 100],得到 {value}")
    return value


def _check_cost(value, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreboardError(f"{where}: cost 必须是数字或 null,得到 {value!r}")
    return float(value)


def _check_duration(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScoreboardError(f"{where}: duration_ms 必须是非负整数,得到 {value!r}")
    return value


def _check_time(value, where: str) -> str:
    if not isinstance(value, str):
        raise ScoreboardError(f"{where}: time 必须是 UTC ISO-8601 字符串")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScoreboardError(f"{where}: time 不是合法 ISO-8601: {value!r}") from exc
    offset = dt.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ScoreboardError(f"{where}: time 必须是 UTC: {value!r}")
    return value


# ── dataclass ──────────────────────────────────────────────────────────────


@dataclass
class RunScore:
    """单次 run 的评分。"""

    score: float
    cost: float | None
    duration_ms: int
    session_id: str


@dataclass
class CaseScore:
    """单个 case 的聚合评分。"""

    case: str
    score: float
    cost: float | None
    duration_ms: int
    runs: list[RunScore] = field(default_factory=list)


@dataclass
class Evaluation:
    """一次完整评估记录(scoreboard 条目;顶层 score = case 均分)。"""

    time: str
    version: int
    model_id: str
    summary_title: str
    summary: str
    score: float
    cost: float | None
    duration_ms: int
    cases: list[CaseScore] = field(default_factory=list)


@dataclass
class Scoreboard:
    """scoreboard.yaml 的解析结果。"""

    evaluations: list[Evaluation] = field(default_factory=list)


@dataclass
class BenchmarkConfig:
    """benchmark_config.toml(对齐 Penguin:title/description/runs=1 + 简化项)。

    ``version``/``parent`` 为版本化演化字段(旧 benchmark 无此字段时
    version 默认 1、parent 为 None,向后兼容)。
    """

    title: str
    description: str
    test_agent: str
    runs: int = 1
    frozen: bool = False
    version: int = 1
    parent: str | None = None


# ── rubric 解析 ────────────────────────────────────────────────────────────


def parse_rubric_points(rubric_md: str) -> dict[str, int]:
    """解析 rubric 的 ``## Scoring`` 表格为 {item: points}。

    ``| item | points | description |`` 行格式;无表格或列不可解析 → 校验失败。
    总和是否 == 100 由 :func:`require_rubric_sum_100` 单独校验。
    """
    match = _RUBRIC_SECTION_RE.search(rubric_md)
    if not match:
        raise BenchmarkValidationError("rubric 缺少 '## Scoring' 段落")
    section = rubric_md[match.end():]
    points: dict[str, int] = {}
    for line in section.splitlines():
        row = _RUBRIC_TABLE_ROW_RE.match(line.strip())
        if not row:
            continue
        item = row.group(1).strip()
        raw = row.group(2).strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise BenchmarkValidationError(
                f"Scoring 表格 points 必须是整数,得到 {raw!r}"
            ) from exc
        if item in points:
            raise BenchmarkValidationError(f"Scoring 表格存在重复 item: {item!r}")
        points[item] = value
    if not points:
        raise BenchmarkValidationError(
            "Scoring 表格为空:需要 '| item | points | description |' 格式的行"
        )
    return points


def require_rubric_sum_100(points: dict[str, int]) -> None:
    """校验 rubric 打分项总和恰好 == 100。"""
    total = sum(points.values())
    if total != 100:
        raise BenchmarkValidationError(
            f"rubric 打分项总和必须恰好 100,当前 {total}(items: {points})"
        )


# ── dict 解析与校验 ────────────────────────────────────────────────────────


def _reject_forbidden_keys(raw: dict, where: str) -> None:
    for key in _FORBIDDEN_EVALUATION_KEYS:
        if key in raw:
            raise ScoreboardError(f"{where}: 不允许字段 {key!r}")


def run_score_from_dict(raw: dict, where: str = "run") -> RunScore:
    if not isinstance(raw, dict):
        raise ScoreboardError(f"{where}: 必须是对象")
    _reject_forbidden_keys(raw, where)
    try:
        session_id = raw["session_id"]
    except KeyError as exc:
        raise ScoreboardError(f"{where}: 缺少 session_id") from exc
    if not isinstance(session_id, str) or not session_id:
        raise ScoreboardError(f"{where}: session_id 必须是非空字符串")
    return RunScore(
        score=_check_score(raw["score"], f"{where}.score"),
        cost=_check_cost(raw.get("cost"), f"{where}.cost"),
        duration_ms=_check_duration(raw["duration_ms"], f"{where}.duration_ms"),
        session_id=session_id,
    )


def case_score_from_dict(raw: dict, where: str = "case") -> CaseScore:
    if not isinstance(raw, dict):
        raise ScoreboardError(f"{where}: 必须是对象")
    _reject_forbidden_keys(raw, where)
    try:
        case = raw["case"]
    except KeyError as exc:
        raise ScoreboardError(f"{where}: 缺少 case") from exc
    if not isinstance(case, str) or not CASE_ID_RE.match(case):
        raise ScoreboardError(
            f"{where}: case 必须匹配 {CASE_ID_RE.pattern!r},得到 {case!r}"
        )
    runs_raw = raw.get("runs", [])
    if not isinstance(runs_raw, list) or not runs_raw:
        raise ScoreboardError(f"{where}: runs 必须是非空数组")
    runs = [
        run_score_from_dict(r, f"{where}.{case}.runs[{i}]")
        for i, r in enumerate(runs_raw)
    ]
    return CaseScore(
        case=case,
        score=_check_score(raw["score"], f"{where}.score"),
        cost=_check_cost(raw.get("cost"), f"{where}.cost"),
        duration_ms=_check_duration(raw["duration_ms"], f"{where}.duration_ms"),
        runs=runs,
    )


def evaluation_from_dict(raw: dict, where: str = "evaluation") -> Evaluation:
    if not isinstance(raw, dict):
        raise ScoreboardError(f"{where}: 必须是对象")
    _reject_forbidden_keys(raw, where)
    try:
        version = raw["version"]
    except KeyError as exc:
        raise ScoreboardError(f"{where}: 缺少 version") from exc
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ScoreboardError(f"{where}: version 必须是正整数,得到 {version!r}")
    try:
        model_id = raw["model_id"]
    except KeyError as exc:
        raise ScoreboardError(f"{where}: 缺少 model_id") from exc
    if not isinstance(model_id, str):
        raise ScoreboardError(f"{where}: model_id 必须是字符串")
    cases_raw = raw.get("cases", [])
    if not isinstance(cases_raw, list):
        raise ScoreboardError(f"{where}: cases 必须是数组")
    cases = [
        case_score_from_dict(c, f"{where}.cases[{i}]")
        for i, c in enumerate(cases_raw)
    ]
    return Evaluation(
        time=_check_time(raw.get("time", ""), f"{where}.time"),
        version=version,
        model_id=model_id,
        summary_title=str(raw.get("summary_title", "")),
        summary=str(raw.get("summary", "")),
        score=_check_score(raw["score"], f"{where}.score"),
        cost=_check_cost(raw.get("cost"), f"{where}.cost"),
        duration_ms=_check_duration(raw["duration_ms"], f"{where}.duration_ms"),
        cases=cases,
    )


def validate_scoreboard(raw: dict) -> Scoreboard:
    """校验 scoreboard 原始 dict,返回解析结果。"""
    if not isinstance(raw, dict) or "evaluations" not in raw:
        raise ScoreboardError("scoreboard 必须含 'evaluations' 键")
    evals_raw = raw["evaluations"]
    if not isinstance(evals_raw, list):
        raise ScoreboardError("'evaluations' 必须是数组")
    board = Scoreboard()
    for i, e in enumerate(evals_raw):
        board.evaluations.append(evaluation_from_dict(e, f"evaluations[{i}]"))
    # 全局版本单调(按追加顺序)
    last_version = 0
    for ev in board.evaluations:
        if ev.version <= last_version:
            raise ScoreboardError(
                f"evaluations 中版本不单调: {ev.version} 不严格大于 {last_version}"
            )
        last_version = ev.version
    return board


def validate_evaluation_append(
    board: Scoreboard, new: Evaluation, *, frozen: bool
) -> None:
    """校验一次 append:frozen 门槛 + version 严格大于最后一条。"""
    if not frozen:
        raise ScoreboardError("scoreboard 只允许追加到已冻结(frozen)的 benchmark")
    if board.evaluations:
        last = board.evaluations[-1]
        if new.version <= last.version:
            raise ScoreboardError(
                f"version 必须严格大于最后一条 {last.version},得到 {new.version}"
            )


def _evaluation_dict(ev: Evaluation) -> dict:
    """Evaluation -> dict(与 scoreboard_to_yaml/evaluation_to_yaml 共享)。"""
    return {
        "time": ev.time,
        "version": ev.version,
        "model_id": ev.model_id,
        "summary_title": ev.summary_title,
        "summary": ev.summary,
        "score": ev.score,
        "cost": ev.cost,
        "duration_ms": ev.duration_ms,
        "cases": [
            {
                "case": cs.case,
                "score": cs.score,
                "cost": cs.cost,
                "duration_ms": cs.duration_ms,
                "runs": [
                    {
                        "score": r.score,
                        "cost": r.cost,
                        "duration_ms": r.duration_ms,
                        "session_id": r.session_id,
                    }
                    for r in cs.runs
                ],
            }
            for cs in ev.cases
        ],
    }


def evaluation_to_yaml(ev: Evaluation) -> str:
    """序列化单条 Evaluation 为 YAML(证据归档 / scoreboard-append --file 输入)。"""
    return yaml.safe_dump(_evaluation_dict(ev), allow_unicode=True, sort_keys=False)


def scoreboard_to_yaml(board: Scoreboard) -> str:
    """序列化 scoreboard 为 YAML(键排序稳定,便于 diff)。"""
    return yaml.safe_dump(
        {"evaluations": [_evaluation_dict(ev) for ev in board.evaluations]},
        allow_unicode=True,
        sort_keys=False,
    )


def scoreboard_from_yaml(text: str) -> Scoreboard:
    """解析并校验 scoreboard YAML 文本。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScoreboardError(f"scoreboard YAML 解析失败: {exc}") from exc
    if raw is None:
        raw = {}
    return validate_scoreboard(raw)


def as_dict(obj) -> dict:
    """dataclass → dict(供 --json 输出)。"""
    return asdict(obj)
