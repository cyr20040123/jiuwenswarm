# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""harness_evolve 数据目录推导(纯函数,唯一依赖 common/utils.py 的路径助手)。

数据根目录默认为 ``get_user_workspace_dir()/"harness-evolve"``,即
``~/.jiuwenswarm/harness-evolve/``(尊重 ``JIUWENSWARM_DATA_DIR`` 环境变量,
以及测试/CLI 通过 ``set_user_workspace_dir`` 或 ``--data-dir`` 注入的基目录)。

布局::

    <base>/harness-evolve/
    ├── agents/<name>/
    │   ├── state.json               # sidecar 版本状态
    │   ├── snapshots/v<N>.tar.gz    # 编辑前快照(禁覆盖)
    │   └── workspaces/<bid>/<case>/run<nn>-<uuid8>/   # 每次评测唯一工作区
    └── benchmarks/<id>/
        ├── benchmark_config.toml
        ├── scoreboard.yaml
        ├── CASE-<nnn>-<semantic>/{statement,rubric}/README.md
        └── evaluations/*.yaml       # 每 cell 证据归档
"""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common.utils import get_user_workspace_dir

__all__ = [
    "harness_evolve_dir",
    "set_base_dir",
    "agents_dir",
    "agent_dir",
    "agent_state_file",
    "snapshots_dir",
    "snapshot_file",
    "agent_workspaces_dir",
    "agent_benchmark_workspace_dir",
    "run_workspace_dir",
    "case_statement_dir",
    "case_rubric_dir",
    "trace_path_for",
    "benchmarks_dir",
    "benchmark_dir",
    "benchmark_config_file",
    "scoreboard_file",
    "benchmark_cases_dir",
    "benchmark_case_dir",
    "benchmark_evaluations_dir",
]

_BASE_DIR_OVERRIDE: Path | None = None


def set_base_dir(path: Path | str | None) -> None:
    """覆盖 harness_evolve 数据根目录(供 CLI ``--data-dir`` 与测试使用)。

    传 ``None`` 恢复默认(``get_user_workspace_dir()/"harness-evolve"``)。
    """
    global _BASE_DIR_OVERRIDE
    _BASE_DIR_OVERRIDE = None if path is None else Path(path).resolve()


def harness_evolve_dir() -> Path:
    """harness_evolve 数据根目录(``--data-dir`` 覆盖 > 默认用户目录)。"""
    if _BASE_DIR_OVERRIDE is not None:
        return _BASE_DIR_OVERRIDE
    return get_user_workspace_dir() / "harness-evolve"


# ── agent 侧 --------------------------------------------------------------


def agents_dir() -> Path:
    """所有 agent sidecar 数据的父目录。"""
    return harness_evolve_dir() / "agents"


def agent_dir(name: str) -> Path:
    """单个 agent 的 sidecar 数据目录(与 AgentConfigService 的 agent 名同名)。"""
    return agents_dir() / name


def agent_state_file(name: str) -> Path:
    """agent 版本 sidecar 文件(state.json)。"""
    return agent_dir(name) / "state.json"


def snapshots_dir(name: str) -> Path:
    """agent 快照目录。"""
    return agent_dir(name) / "snapshots"


def snapshot_file(name: str, version: int) -> Path:
    """第 version 版快照文件(v<version>.tar.gz)。"""
    return snapshots_dir(name) / f"v{version}.tar.gz"


def agent_workspaces_dir(name: str) -> Path:
    """agent 的评测工作区根目录(按 benchmark/case 分层)。"""
    return agent_dir(name) / "workspaces"


def agent_benchmark_workspace_dir(name: str, benchmark_id: str) -> Path:
    """某 benchmark 的工作区目录。"""
    return agent_workspaces_dir(name) / benchmark_id


def run_workspace_dir(
    name: str, benchmark_id: str, case_id: str, run_index: int, session_id: str
) -> Path:
    """单次评测唯一工作区目录。

    session_id 由调用方生成(``he-<bid8>-<case12>-r<nn>-<uuid8>``),保证目录唯一。
    """
    return (
        agent_benchmark_workspace_dir(name, benchmark_id)
        / case_id
        / f"run{run_index}-{session_id.split('-')[-1]}"
    )


def case_statement_dir(benchmark_id: str, case_id: str) -> Path:
    """case 的公开 statement 目录(评测工作区拷贝源)。"""
    return benchmark_case_dir(benchmark_id, case_id) / "statement"


def case_rubric_dir(benchmark_id: str, case_id: str) -> Path:
    """case 的私有 rubric 目录(只进 grader,绝不拷入工作区)。"""
    return benchmark_case_dir(benchmark_id, case_id) / "rubric"


def trace_path_for(session_id: str) -> Path:
    """某 session 的 trace dump 路径(与 debug_trace.paths 的落盘规则一致)。

    session_id 来自本模块生成(``he-*``),不带路径分隔符,安全。
    """
    from jiuwenswarm.server.runtime.debug_trace.paths import debug_trace_file

    return debug_trace_file("agent", session_id)


# ── benchmark 侧 ----------------------------------------------------------


def benchmarks_dir() -> Path:
    """所有 benchmark 的父目录。"""
    return harness_evolve_dir() / "benchmarks"


def benchmark_dir(benchmark_id: str) -> Path:
    """单个 benchmark 目录。"""
    return benchmarks_dir() / benchmark_id


def benchmark_config_file(benchmark_id: str) -> Path:
    """benchmark_config.toml。"""
    return benchmark_dir(benchmark_id) / "benchmark_config.toml"


def scoreboard_file(benchmark_id: str) -> Path:
    """scoreboard.yaml。"""
    return benchmark_dir(benchmark_id) / "scoreboard.yaml"


def benchmark_cases_dir(benchmark_id: str) -> Path:
    """case 目录(CASE-<nnn>-<semantic>/)的父目录。"""
    return benchmark_dir(benchmark_id)


def benchmark_case_dir(benchmark_id: str, case_id: str) -> Path:
    """单个 case 目录。"""
    return benchmark_dir(benchmark_id) / case_id


def benchmark_evaluations_dir(benchmark_id: str) -> Path:
    """每 cell 证据归档目录(evaluations/)。"""
    return benchmark_dir(benchmark_id) / "evaluations"
