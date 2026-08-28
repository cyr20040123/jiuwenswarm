# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""harness_evolve — Penguin 式 agent harness 自演进闭环(独立模块)。

把 Penguin Harness 的四个技能闭环(agent-creation / benchmark-design /
agent-evaluation / agent-optimization)移植进 JiuwenSwarm。本模块是全部重量级
执行能力的载体,对外以 CLI(``jiuwenswarm-harness-evolve``)和 Python API 双入口
暴露确定性原语;四个 SKILL.md 技能(对话式薄壳)通过 bash 工具调用 CLI 驱动闭环。

独立性约束(验收红线):
- 只允许 import: ``jiuwenswarm.common.utils``(路径助手)、
  ``jiuwenswarm.server.runtime.agent_config_service.AgentConfigService``、
  ``jiuwenswarm.server.runtime.debug_trace``、openjiuwen SDK 及其工厂函数,
  以及被明确标注为公开 leaf 的纯函数(sysop_builder.create_local_sysop_card、
  interface_deep.build_model_from_entry)。
- 禁止 import: ``interface_deep.JiuWenSwarmDeepAdapter`` 类及其实例方法、
  ``code_agent_rail``、``skilldev`` 模块。
- 现有模块不得 import 本包;本包整体可独立删除,不影响 jiuwenswarm 任何功能。

数据目录:``~/.jiuwenswarm/harness-evolve/``(尊重 ``JIUWENSWARM_DATA_DIR``),
布局见 ``paths.py`` 与各模块 docstring。
"""

from __future__ import annotations

__version__ = "0.1.0"
