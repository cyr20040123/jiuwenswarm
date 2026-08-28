# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CLI 子命令装配。

每个子模块暴露 ``register(registry: dict)`` 函数,把子命令登记到 CLI 注册表。
注册表项:名称 -> (parser 装配函数, 执行函数)。

惰性 import:只有 ``register_commands`` 被调用时才加载各命令模块,因此 CLI
``--help`` 不引入 openjiuwen 等重依赖。
"""

from __future__ import annotations

import argparse
from typing import Callable


def out(args: argparse.Namespace, message: str) -> None:
    """打印人读文本;``--json`` 模式下抑制,保持 stdout 纯净可解析。"""
    if not getattr(args, "json", False):
        print(message)


def register_commands(registry: dict[str, tuple[Callable, Callable]]) -> None:
    """注册全部子命令。"""
    from jiuwenswarm.harness_evolve.commands import (
        agent_cmds,
        benchmark_cmds,
        eval_cmds,
        optimize_cmds,
    )

    agent_cmds.register(registry)
    benchmark_cmds.register(registry)
    eval_cmds.register(registry)
    optimize_cmds.register(registry)
    return None
