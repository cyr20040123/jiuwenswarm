# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""自定义 agent → 主 agent 子代理注册(config.yaml react.subagents)。

jiuwenswarm 的 ``_load_custom_subagents``(server/runtime/agent_adapter/
interface_deep.py)只把 ``config.yaml`` 中 ``react.subagents.<name>.enabled:
true`` 显式启用的自定义 agent 暴露为主 agent 可调度的子代理——仅创建
``~/.jiuwenswarm/agents/<name>.md`` 定义还不够。本模块在 ``agent-register``
成功后自动补上这一步(幂等),用户无需手改配置。

写盘复用 common.config 的 round-trip 读写(保留注释与格式)与 portalocker
文件锁(与 update_config 同款,防与 AgentServer 并发写冲突)。只写用户级配置
``~/.jiuwenswarm/config/config.yaml``;文件不存在时跳过,绝不写包内 resources。
"""

from __future__ import annotations

import logging
from pathlib import Path

import portalocker

from jiuwenswarm.common import utils
from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip

logger = logging.getLogger(__name__)

# react.subagents 段在配置中的键名
_REACT_KEY = "react"
_SUBAGENTS_KEY = "subagents"
_ENABLED_KEY = "enabled"


def user_config_path() -> Path:
    """用户级 config.yaml 路径(不受包内 resources fallback 影响)。"""
    return utils.get_user_workspace_dir() / "config" / "config.yaml"


def enable_subagent_in_config(name: str) -> tuple[bool, str]:
    """幂等地把 agent 注册进用户 config.yaml 的 ``react.subagents``。

    已存在的条目只把 ``enabled`` 置为 true,其余字段(如 max_iterations)
    原样保留;整个写入在文件锁内完成,保证并发安全。

    Args:
        name: agent 名(须与 AgentConfigService 定义一致)。

    Returns:
        (是否写入成功, 面向用户的状态消息)。
    """
    config_path = user_config_path()
    if not config_path.is_file():
        return False, f"config.yaml 不存在({config_path}),跳过子代理启用"
    try:
        with portalocker.Lock(
            str(config_path.with_name(config_path.stem + ".lock")), timeout=10.0
        ):
            data = load_yaml_round_trip(config_path)
            if data is None:
                data = {}
            result = _set_enabled(data, name)
            if not result[0]:
                return result
            dump_yaml_round_trip(config_path, data)
        return True, f"已注册进 config: react.subagents.{name}.enabled = true"
    except Exception as exc:
        # 锁超时/IO/格式错误:agent 注册主流程不受影响,提示手动启用
        logger.warning("[config_registry] enable %s failed: %s", name, exc)
        return False, f"写入 config.yaml 失败({exc});可手动在 react.subagents 下添加 {name}: enabled: true"


def ensure_custom_agents_enabled() -> list[str]:
    """按 agents/*.md 存量定义自愈 react.subagents 注册(幂等)。

    背景:上游启动期的配置模板迁移(migrate_config_from_template → _deep_merge)
    会把模板中不存在的 ``react.subagents.<name>`` 条目当废弃键清除,导致已注册
    的自定义 agent 在重启后从子代理列表消失。本函数在迁移之后执行:凡存在
    非 builtin 定义就补上 ``enabled: true``,使"md 存在 ⇒ 子代理可用"成为不变量。

    语义约定与 agent-register/agent-delete 一致:不支持"定义存在但停用"的中间态
    (CLI 只提供注册即启用与删除即清理,无单独 disable);手动把 enabled 改为 false
    会在下次启动时被本函数翻转回 true。

    Returns:
        本次实际补写启用的 agent 名列表(已启用者跳过,不写盘)。
    """
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    already_enabled = _enabled_subagent_names()
    restored: list[str] = []
    for agent_def in AgentConfigService().list_agents():
        if agent_def.source == "builtin":
            continue
        if agent_def.name in already_enabled:
            continue
        enabled, _msg = enable_subagent_in_config(agent_def.name)
        if enabled:
            restored.append(agent_def.name)
    if restored:
        logger.info("[config_registry] self-heal re-enabled: %s", ", ".join(restored))
    return restored


def _enabled_subagent_names() -> set[str]:
    """读取用户 config.yaml,返回 react.subagents 下 enabled 为真的名字集合。

    只读判定用,不加锁(enable 内部写盘时自带锁);读失败按空集合处理,
    让后续 enable 走幂等补写。
    """
    try:
        react = (load_yaml_round_trip(user_config_path()) or {}).get(_REACT_KEY)
        subagents = (react or {}).get(_SUBAGENTS_KEY)
        if not isinstance(subagents, dict):
            return set()
        return {
            name
            for name, entry in subagents.items()
            if isinstance(entry, dict) and bool(entry.get(_ENABLED_KEY, False))
        }
    except Exception:  # noqa: BLE001
        logger.warning("[config_registry] read current subagents failed", exc_info=True)
        return set()


def remove_subagent_from_config(name: str) -> tuple[bool, str]:
    """从用户 config.yaml 的 ``react.subagents`` 删除 agent 条目(镜像 enable 模式)。

    条目不存在 → (False, 提示) 不写盘;整个写入在文件锁内完成,并发安全。
    供 agent-delete 清理用。

    Args:
        name: agent 名(须与 AgentConfigService 定义一致)。

    Returns:
        (是否写入成功, 面向用户的状态消息)。
    """
    config_path = user_config_path()
    if not config_path.is_file():
        return False, f"config.yaml 不存在({config_path}),无需清理子代理条目"
    try:
        with portalocker.Lock(
            str(config_path.with_name(config_path.stem + ".lock")), timeout=10.0
        ):
            data = load_yaml_round_trip(config_path)
            if data is None:
                data = {}
            result = _remove_entry(data, name)
            if not result[0]:
                return result
            dump_yaml_round_trip(config_path, data)
        return True, f"已从 config 移除: react.subagents.{name}"
    except Exception as exc:
        # 锁超时/IO/格式错误:删除主流程不受影响,提示手动清理
        logger.warning("[config_registry] remove %s failed: %s", name, exc)
        return False, f"写入 config.yaml 失败({exc});可手动删除 react.subagents 下的 {name}"


def _remove_entry(data: dict, name: str) -> tuple[bool, str]:
    """在已加载的 config 数据上删除 react.subagents.<name>(不存在则不动)。"""
    react = data.get(_REACT_KEY)
    if not isinstance(react, dict):
        return False, "config.yaml 无 react.subagents 段,无需清理"
    subagents = react.get(_SUBAGENTS_KEY)
    if not isinstance(subagents, dict):
        return False, "config.yaml 无 react.subagents 段,无需清理"
    if name not in subagents:
        return False, f"config.yaml 无 react.subagents.{name} 条目,无需清理"
    del subagents[name]
    return True, ""


def _set_enabled(data: dict, name: str) -> tuple[bool, str]:
    """在已加载的 config 数据上设置 react.subagents.<name>.enabled = true。

    逐段校验类型;任何一段不是映射都视为配置损坏,返回失败消息(不写盘)。
    """
    react = data.get(_REACT_KEY)
    if react is None:
        react = data[_REACT_KEY] = {}
    if not isinstance(react, dict):
        return False, f"config.yaml 的 {_REACT_KEY} 段不是映射,跳过子代理启用"
    subagents = react.get(_SUBAGENTS_KEY)
    if subagents is None:
        subagents = react[_SUBAGENTS_KEY] = {}
    if not isinstance(subagents, dict):
        return False, f"config.yaml 的 {_REACT_KEY}.{_SUBAGENTS_KEY} 段不是映射,跳过子代理启用"
    entry = subagents.get(name)
    if entry is None:
        entry = subagents[name] = {}
    if not isinstance(entry, dict):
        return False, f"config.yaml 的 {_REACT_KEY}.{_SUBAGENTS_KEY}.{name} 段不是映射,跳过子代理启用"
    entry[_ENABLED_KEY] = True
    return True, ""
