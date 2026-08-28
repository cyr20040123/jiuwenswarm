# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""schemas.py 校验矩阵(无 LLM)。"""

from __future__ import annotations

import pytest

from jiuwenswarm.harness_evolve.errors import (
    BenchmarkValidationError,
    ScoreboardError,
)
from jiuwenswarm.harness_evolve.schemas import (
    CaseScore,
    Evaluation,
    RunScore,
    Scoreboard,
    parse_rubric_points,
    require_rubric_sum_100,
    scoreboard_from_yaml,
    scoreboard_to_yaml,
    validate_evaluation_append,
    validate_scoreboard,
)


def _eval_dict(score=76.5, version=1, time="2026-08-14T09:30:00Z", **over):
    base = {
        "time": time,
        "version": version,
        "model_id": "gpt-4o",
        "summary_title": "baseline",
        "summary": "first accepted",
        "score": score,
        "cost": 0.12,
        "duration_ms": 42000,
        "cases": [
            {
                "case": "CASE-001-summary",
                "score": 80.0,
                "cost": 0.1,
                "duration_ms": 40000,
                "runs": [
                    {
                        "score": 80.0,
                        "cost": 0.1,
                        "duration_ms": 40000,
                        "session_id": "he-abc-001-r1-00000000",
                    }
                ],
            }
        ],
    }
    base.update(over)
    return base


class TestRubricParsing:
    RUBRIC = """# Case: summarize

Task-specific instructions live here.

## Scoring

| item | points | description |
| ---- | ------ | ----------- |
| accuracy | 60 | correct facts only |
| conciseness | 25 | no fluff |
| structure | 15 | uses headers |
"""

    def test_parse_valid(self):
        points = parse_rubric_points(self.RUBRIC)
        assert points == {"accuracy": 60, "conciseness": 25, "structure": 15}

    def test_sum_exact_100_passes(self):
        require_rubric_sum_100(parse_rubric_points(self.RUBRIC))

    def test_sum_not_100(self):
        with pytest.raises(BenchmarkValidationError, match="总和必须恰好 100"):
            require_rubric_sum_100({"a": 50, "b": 30})

    def test_missing_scoring_section(self):
        with pytest.raises(BenchmarkValidationError, match="Scoring"):
            parse_rubric_points("no table here")

    def test_duplicate_item_rejected(self):
        md = self.RUBRIC + "| accuracy | 10 | dup |\n"
        with pytest.raises(BenchmarkValidationError, match="重复"):
            parse_rubric_points(md)

    def test_empty_table(self):
        with pytest.raises(BenchmarkValidationError, match="表格为空"):
            parse_rubric_points("## Scoring\n\n| item | points | description |\n| -- | -- | -- |\n")


class TestScoreValidation:
    def test_valid_evaluation(self):
        board = validate_scoreboard({"evaluations": [_eval_dict()]})
        assert board.evaluations[0].version == 1
        assert board.evaluations[0].cases[0].runs[0].session_id

    def test_score_out_of_range(self):
        with pytest.raises(ScoreboardError, match="\\[0, 100\\]"):
            validate_scoreboard({"evaluations": [_eval_dict(score=101)]})

    def test_score_three_decimals(self):
        with pytest.raises(ScoreboardError, match="两位小数"):
            validate_scoreboard({"evaluations": [_eval_dict(score=76.555)]})

    def test_negative_duration(self):
        with pytest.raises(ScoreboardError, match="duration_ms"):
            validate_scoreboard({"evaluations": [_eval_dict(duration_ms=-1)]})

    def test_non_utc_time(self):
        with pytest.raises(ScoreboardError, match="UTC"):
            validate_scoreboard(
                {"evaluations": [_eval_dict(time="2026-08-14T09:30:00+08:00")]}
            )

    def test_bad_case_id(self):
        with pytest.raises(ScoreboardError, match="CASE-"):
            validate_scoreboard(
                {"evaluations": [_eval_dict(**{"cases": [
                    {
                        "case": "not-a-case",
                        "score": 80.0,
                        "cost": None,
                        "duration_ms": 1,
                        "runs": [
                            {
                                "score": 80.0,
                                "cost": None,
                                "duration_ms": 1,
                                "session_id": "s1",
                            }
                        ],
                    }
                ]})]}
            )

    def test_forbidden_keys(self):
        with pytest.raises(ScoreboardError, match="max_score"):
            validate_scoreboard(
                {"evaluations": [_eval_dict(max_score=100)]}
            )

    def test_cost_null_allowed(self):
        board = validate_scoreboard({"evaluations": [_eval_dict(cost=None)]})
        assert board.evaluations[0].cost is None

    def test_empty_runs_rejected(self):
        with pytest.raises(ScoreboardError, match="runs"):
            validate_scoreboard(
                {"evaluations": [_eval_dict(**{"cases": [
                    {"case": "CASE-001-x", "score": 1.0, "cost": None,
                     "duration_ms": 1, "runs": []}
                ]})]}
            )

    def test_version_non_monotonic(self):
        with pytest.raises(ScoreboardError, match="单调"):
            validate_scoreboard(
                {
                    "evaluations": [
                        _eval_dict(version=2),
                        _eval_dict(version=2),
                    ]
                }
            )


class TestAppendValidation:
    def test_requires_frozen(self):
        board = Scoreboard()
        ev = Evaluation(
            time="2026-08-14T09:30:00Z", version=1, model_id="m",
            summary_title="t", summary="s", score=80.0, cost=None,
            duration_ms=100,
            cases=[CaseScore(case="CASE-001-x", score=80.0, cost=None,
                             duration_ms=100,
                             runs=[RunScore(score=80.0, cost=None,
                                            duration_ms=100,
                                            session_id="s1")])],
        )
        with pytest.raises(ScoreboardError, match="frozen"):
            validate_evaluation_append(board, ev, frozen=False)

    def test_version_must_strictly_increase(self):
        board = Scoreboard(evaluations=[
            Evaluation(time="2026-08-14T09:30:00Z", version=2, model_id="m",
                       summary_title="t", summary="s", score=80.0, cost=None,
                       duration_ms=100, cases=[]),
        ])
        new = Evaluation(time="2026-08-14T09:31:00Z", version=2, model_id="m",
                         summary_title="t", summary="s", score=90.0,
                         cost=None, duration_ms=100, cases=[])
        with pytest.raises(ScoreboardError, match="严格大于"):
            validate_evaluation_append(board, new, frozen=True)

    def test_accepts_greater_version(self):
        board = Scoreboard(evaluations=[
            Evaluation(time="2026-08-14T09:30:00Z", version=2, model_id="m",
                       summary_title="t", summary="s", score=80.0, cost=None,
                       duration_ms=100, cases=[]),
        ])
        new = Evaluation(time="2026-08-14T09:31:00Z", version=3, model_id="m",
                         summary_title="t", summary="s", score=90.0,
                         cost=None, duration_ms=100, cases=[])
        validate_evaluation_append(board, new, frozen=True)  # 不抛


class TestYamlRoundtrip:
    def test_roundtrip(self):
        raw = {"evaluations": [_eval_dict()]}
        board = validate_scoreboard(raw)
        text = scoreboard_to_yaml(board)
        again = scoreboard_from_yaml(text)
        assert again.evaluations[0].score == 76.5
        assert again.evaluations[0].cases[0].runs[0].session_id

    def test_corrupted_yaml(self):
        with pytest.raises(ScoreboardError):
            scoreboard_from_yaml("evaluations: [broken")

    def test_missing_evaluations_key(self):
        with pytest.raises(ScoreboardError, match="evaluations"):
            scoreboard_from_yaml("foo: bar\n")
