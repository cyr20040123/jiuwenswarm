# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""评测用工具注册表:frontmatter 工具名 -> openjiuwen 工具实例的声明性映射。

设计约束:
- **不复制** interface_deep 的 ``_get_tool_cards``(它绑定 adapter 状态:已注册实例、
  vision/audio 配置、付费 key 探测),抽取即重构热文件。这里只做纯声明式映射,
  工具名清单抄自 ``agent_config_service._TOOL_DESCRIPTIONS``(见
  jiuwenswarm/server/runtime/agent_config_service.py:34),构造模式抄自
  ``interface_deep._get_tool_cards``(``tool_cls(agent_id=...)``,见
  interface_deep.py:5212)。若这两个源头新增工具,需同步本文件。
- 文件/Shell 工具(Read/Write/Edit/Bash/LS/Grep/Glob)由被测 agent 的
  SysOperation 自动派生(约 16 个 fs/shell/code 工具,见
  openjiuwen.factory 对 ``sys_operation`` 的解析),**绝不手工构造**,命中即跳过。
- 其余工具能简单构造的(web 搜索/网页抓取)映射到 openjiuwen 工具类;
  依赖 adapter 级配置的(记忆/Cron/视觉/音频/LSP/SkillTool)与未知名字
  **告警并跳过**——评测闭环只承诺文件工具 + 网页工具,缺失项影响的是能力上限,
  不是正确性。
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# 抄自 jiuwenswarm/server/runtime/agent_config_service.py:34 _TOOL_DESCRIPTIONS
# (仅名字,description 文案对评测无意义)。文件工具名前缀列表用于跳过。
_FILE_TOOL_NAMES = frozenset(
    ["Read", "Write", "Edit", "Bash", "LS", "Grep", "Glob", "LSP"]
)

# 工具名 -> 构造器。构造器接收 agent_id(工具级资源归属,per-run 唯一,
# 避免与全局注册表冲突;见 interface_deep.py:5212 的 ``tool_cls(agent_id=...)``)。
# 构造失败(缺依赖/导入错误)时记告警并跳过该工具,不阻塞评测。
_TOOL_BUILDERS: dict[str, Callable[[str], object]] = {
    "WebSearch": lambda agent_id: _web_tool("WebFreeSearchTool", agent_id),
    "WebFetch": lambda agent_id: _web_tool("WebFetchWebpageTool", agent_id),
}


def _web_tool(class_name: str, agent_id: str) -> object:
    """惰性 import 工具类(openjiuwen 为命名空间包,顶层 import 太重)。"""
    from openjiuwen.harness import tools as _tools

    return getattr(_tools, class_name)(agent_id=agent_id)


def build_named_tools(
    tool_names: Iterable[str] | None, *, agent_id: str
) -> list[object]:
    """按 frontmatter ``tools`` 白名单构造非文件工具实例列表。

    Args:
        tool_names: frontmatter 的 tools 字段;``None`` 或 ``["*"]`` 表示全部。
        agent_id: per-run 唯一工具归属 id。

    Returns:
        构造成功的工具实例列表(调用方取 ``.card`` 传给 create_deep_agent)。
    """
    wanted = set(tool_names or ["*"])
    if "*" in wanted:
        wanted = set(_FILE_TOOL_NAMES) | set(_TOOL_BUILDERS)

    built: list[object] = []
    for name in sorted(wanted):
        if name in _FILE_TOOL_NAMES:
            continue  # 由 SysOperation 自动派生
        builder = _TOOL_BUILDERS.get(name)
        if builder is None:
            logger.warning("[harness-evolve] 未知工具名 %r,跳过", name)
            continue
        try:
            built.append(builder(agent_id))
        except Exception as exc:  # 工具构造失败不阻塞评测
            logger.warning("[harness-evolve] 工具 %s 构造失败,跳过: %s", name, exc)
    return built


__all__ = ["build_named_tools", "_FILE_TOOL_NAMES", "_TOOL_BUILDERS"]
