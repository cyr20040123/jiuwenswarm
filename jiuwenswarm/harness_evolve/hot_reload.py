# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent 变更后的服务热重载通知(subagent 定义免重启生效)。

主 agent 实例按 channel 长命缓存,subagent 规格在实例构建时一次性读入;
web 面板的 agents.* 处理器每次操作后调 ``reload_agents_config`` 热生效,
本模块让独立进程的 harness_evolve CLI 复用同一条通路:直连 agent server
WS 端口发一条 ``agent.reload_config`` 消息(params 空 = 全量热重载,
服务器指纹相同直接跳过 = 幂等),触发运行中的服务重读磁盘定义。

设计取舍:
- **不走 gateway**:``agent.reload_config`` 不在 gateway 转发白名单,会回
  unknown method;直连 agent server 默认无认证(origin check 关闭)。
- **不复用 WebSocketAgentServerClient**:其所在 gateway 包 import 连带
  openjiuwen(实测 ~8s),CLI 每条命令多花 8s 不可接受。此处自写 ~50 行裸
  websockets 客户端(信封复用 ``e2a_from_agent_fields`` 保证线协议零偏差,
  import ~0.15s)。
- 连上→发→收→立即断开:agent server 的 ``_current_ws`` 被每个新连接抢占,
  短连接避免干扰其推送通道。
- 永不抛异常:连接失败/拒绝/超时一律降级为提示文案,主命令不受影响。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

_NO_HOT_RELOAD_ENV = "JIUWENSWARM_HARNESS_EVOLVE_NO_HOT_RELOAD"
_SUCCESS_MSG = "agent 服务已热重载,新定义立即生效"
_ACK_TIMEOUT = 5.0


def _load_env() -> None:
    """加载 ~/.jiuwenswarm/config/.env(AGENT_SERVER_HOST/PORT 来源)。"""
    try:
        from dotenv import load_dotenv

        from jiuwenswarm.common.utils import get_env_file

        load_dotenv(get_env_file(), override=False)
    except Exception:  # noqa: BLE001 — dotenv 缺失/加载失败不阻断
        logger.debug("[hot_reload] dotenv 加载跳过", exc_info=True)


def resolve_agent_server_addr() -> tuple[str, int]:
    """解析 agent server 地址:env 覆盖,缺省 127.0.0.1:18092。"""
    host = os.getenv("AGENT_SERVER_HOST", "").strip() or "127.0.0.1"
    raw = (
        os.getenv("AGENT_SERVER_PORT", "").strip()
        or os.getenv("AGENT_PORT", "").strip()
        or "18092"
    )
    try:
        port = int(raw)
    except ValueError:
        logger.warning("[hot_reload] 非法端口环境变量 %r,回落 18092", raw)
        port = 18092
    return host, port


def build_reload_envelope() -> dict:
    """构造 agent.reload_config E2A 信封(与 gateway _on_config_saved 同款)。"""
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.schema.message import ReqMethod

    envelope = e2a_from_agent_fields(
        request_id=f"agent-reload-{uuid.uuid4().hex[:8]}",
        channel_id="",
        session_id="sess_reload",
        user_id="agentos_test",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params={},
        is_stream=False,
        timestamp=time.time(),
    )
    return envelope.to_dict()


def _parse_reload_response(raw: str) -> tuple[bool, str]:
    """解析响应 wire(E2A 形状为主,兼容 legacy 顶层 ok/payload)。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, "invalid response"
    if not isinstance(data, dict):
        return False, "invalid response"
    # E2A 形状:status + body.result / body.message
    status = data.get("status")
    if status == "succeeded":
        body = data.get("body") if isinstance(data.get("body"), dict) else {}
        result = body.get("result") if isinstance(body.get("result"), dict) else None
        if result and result.get("reloaded") is True:
            return True, ""
        return False, "response missing reloaded flag"
    if status == "failed":
        body = data.get("body") if isinstance(data.get("body"), dict) else {}
        return False, str(body.get("message") or body.get("details") or "server reported failure")
    # legacy 形状:顶层 ok + payload
    if data.get("ok") is True:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        if payload.get("reloaded") is True:
            return True, ""
        return False, "response missing reloaded flag"
    if data.get("ok") is False:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        return False, str(payload.get("error") or "server reported failure")
    return False, f"unexpected response: {str(data)[:120]}"


def _connect(uri: str):
    """legacy client 优先(agent server 是 legacy server,新 API 握手会失败)。"""
    from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES

    try:
        from websockets.legacy.client import connect as legacy_connect

        return legacy_connect(
            uri,
            origin=uri.replace("ws://", "http://"),
            ping_interval=None,
            close_timeout=5.0,
            max_size=AGENT_WS_MAX_MESSAGE_BYTES,
        )
    except ImportError:
        from websockets import connect as new_connect

        return new_connect(
            uri,
            ping_interval=None,
            close_timeout=5.0,
            max_size=AGENT_WS_MAX_MESSAGE_BYTES,
        )


async def _one_attempt(uri: str, envelope: dict, timeout: float) -> tuple[bool, str, bool]:
    """单次尝试:连→收 ack(可忽略)→发→收响应→断开。

    Returns:
        (ok, message, rejected):rejected = 服务器可达且明确拒绝(不再重试)。
    """
    try:
        async with _connect(uri) as ws:
            try:
                await asyncio.wait_for(ws.recv(), _ACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info("[hot_reload] %ss 内未收到 connection.ack,继续发送", _ACK_TIMEOUT)
            await ws.send(json.dumps(envelope))
            raw = await asyncio.wait_for(
                ws.recv(), max(0.5, timeout - _ACK_TIMEOUT)
            )
        ok, msg = _parse_reload_response(raw)
        if not ok:
            # 服务器可达且明确响应(失败)→ 重试无意义
            return False, msg, True
        return True, "", False
    except Exception as exc:  # noqa: BLE001 — 连接拒绝/断开/超时均按传输失败处理
        logger.info("[hot_reload] attempt failed: %s", exc)
        return False, "", False


async def _run_retries(
    uri: str, envelope: dict, retries: int, interval: float, timeout: float
) -> tuple[bool, str]:
    total = retries + 1
    for attempt in range(1, total + 1):
        ok, msg, rejected = await _one_attempt(uri, envelope, timeout)
        if ok:
            logger.info(
                "[hot_reload] ok request_id=%s attempt=%d/%d",
                envelope["request_id"], attempt, total,
            )
            return True, _SUCCESS_MSG
        if rejected:
            logger.warning("[hot_reload] 服务器拒绝热重载: %s", msg)
            return False, f"警告:agent 服务拒绝热重载: {msg};重启 agent 服务后生效"
        logger.info("[hot_reload] attempt %d/%d failed,将于 %.1fs 后重试", attempt, total, interval)
        if attempt < total:
            await asyncio.sleep(interval)
    logger.warning("[hot_reload] 重试 %d 次后放弃(uri=%s)", retries, uri)
    return (
        False,
        f"警告:无法连接 agent 服务(重试 {retries} 次后放弃);变更已保存,重启 agent 服务后生效",
    )


def notify_agent_reload(
    *, retries: int = 5, interval: float = 3.0, timeout: float = 30.0
) -> tuple[bool, str]:
    """通知运行中的 agent 服务热重载 subagent 定义。

    Args:
        retries: 连接失败后的重试次数(总尝试 retries+1 次)。
        interval: 两次尝试间隔(秒)。
        timeout: 单次尝试总超时(秒)。

    Returns:
        (是否已热生效, 面向用户的消息;空消息 = 静默跳过)。

    永不抛异常;``JIUWENSWARM_HARNESS_EVOLVE_NO_HOT_RELOAD`` 为 truthy 时
    静默跳过(测试/不希望热重载时使用)。
    """
    _load_env()
    if os.getenv(_NO_HOT_RELOAD_ENV, "").strip():
        return False, ""
    host, port = resolve_agent_server_addr()
    envelope = build_reload_envelope()
    uri = f"ws://{host}:{port}"
    return asyncio.run(_run_retries(uri, envelope, retries, interval, timeout))
