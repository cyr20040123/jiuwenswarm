# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""harness_evolve 异常层次。

CLI 退出码约定:0=成功,2=用户错误(UsageError 系),3=执行失败(HarnessError 系)。
"""

from __future__ import annotations

__all__ = [
    "HarnessError",
    "HarnessUsageError",
    "AgentStateError",
    "BenchmarkError",
    "BenchmarkValidationError",
    "ScoreboardError",
    "SnapshotError",
    "LaunchFailure",
    "GradingFailure",
    "VersionChangedError",
]


class HarnessError(Exception):
    """执行失败基类(CLI 退出码 3)。"""


class HarnessUsageError(HarnessError):
    """用户输入错误基类(CLI 退出码 2)。"""


class AgentStateError(HarnessError):
    """agent sidecar 状态错误(版本、漂移、前置快照等)。"""


class BenchmarkError(HarnessError):
    """benchmark 目录/结构错误。"""


class BenchmarkValidationError(HarnessUsageError):
    """benchmark 内容校验失败(缺 case、rubric 和 != 100、frozen 后写入等)。"""


class ScoreboardError(HarnessError):
    """scoreboard 读写/校验错误。"""


class SnapshotError(HarnessError):
    """快照创建/恢复错误。"""


class LaunchFailure(HarnessError):
    """被测 agent 启动/运行失败 → 不可打分(evaluation_failed / invalid_request)。"""


class GradingFailure(HarnessError):
    """打分过程失败(重试一次后仍失败)→ 不可打分(evaluation_failed)。"""


class VersionChangedError(HarnessError):
    """评测期间被测 agent 定义被改动(version_changed)。"""
