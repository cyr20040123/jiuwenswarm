# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""harness_evolve 测试公共 fixture。

每个测试把 harness_evolve 数据根目录与用户级 agent 目录都重定向到
``tmp_path``(不触碰真实 ``~/.jiuwenswarm``),测试结束后恢复。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.harness_evolve import paths


@pytest.fixture(autouse=True)
def _he_isolated_home(tmp_path, monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.delenv("JIUWENSWARM_HOME", raising=False)
    # set_user_home 不重置 _workspace_base_dir 缓存(utils 内部不一致),这里手动清
    monkeypatch.setattr(utils, "_workspace_base_dir", None)
    utils.set_user_home(tmp_path)
    paths.set_base_dir(tmp_path / "harness-evolve")
    yield
    paths.set_base_dir(None)


@pytest.fixture(autouse=True)
def _he_no_hot_reload(monkeypatch):
    """CLI 测试默认不真连 agent server;hot_reload 单测自行 delenv 解除。"""
    monkeypatch.setenv("JIUWENSWARM_HARNESS_EVOLVE_NO_HOT_RELOAD", "1")

