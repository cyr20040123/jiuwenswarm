# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless 单 case 单 run 评测执行器。

本模块是全包唯一直接构造 openjiuwen DeepAgent 的地方(构造收敛,便于按
openjiuwen 版本差异局部适配)。关键决策:

- **模型**:按 auto_harness/scheduler.py:62 ``_build_model_cache`` 的同一规则,
  用 ``interface_deep.build_model_from_entry``(模块级共享函数,非 adapter 类)
  从 ``models.defaults`` 条目构建。优先级:命令行/参数 > frontmatter model > 首个 default。
- **工作区**:每次评测唯一目录(``run<nn>-<uuid8>``),拷 statement 全部文件 +
  frontmatter skills 的 SKILL.md 目录;``rubric/`` **绝不拷入**。
- **工具**:文件工具由 SysOperation 自动派生(~16 个 fs/shell/code 工具,
  openjiuwen factory 对 ``sys_operation`` 的解析),其余走 tools_registry;
  per-run 唯一 sysop id + tool_owner_id,跑完注销,防进程全局注册表冲突。
- **权限**:``rails=[]`` 即无权限护栏(等价 permissions.enabled=false);
  文件操作被 ``restrict_to_sandbox=True`` 锁在本 run 工作区。
- **trace**:自生成 session_id,注册 DebugTraceLogger(interface_deep.py:9494
  同款模式)落盘 ``~/.jiuwenswarm/.agent/traces/dump-agent-<session_id>.txt``。
- **失败分类**:构造/启动/执行中异常 = LaunchFailure(不可打分);
  正常跑完但产物错/缺 = status ok(可打分,得低分);评测前后 md_sha256 漂移
  = version_changed(被测 agent 的写操作被 SysOperation 边界锁在自身工作区,
  此为防御性检查)。
- **cost**:openjiuwen 0.1.16 无货币成本 API,恒 null(schema 允许,不阻塞闭环)。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from jiuwenswarm.harness_evolve import agent_state, benchmark_store, paths
from jiuwenswarm.harness_evolve.trace_stats import extract_trace_stats
from jiuwenswarm.harness_evolve.errors import (
    HarnessUsageError,
    LaunchFailure,
)

logger = logging.getLogger(__name__)

# 被测 agent 的唯一任务入口(statement 以 README.md 形式进入工作区)
QUERY = "Read README.md in the current Workspace and complete the task exactly as specified there."


@dataclass
class RunResult:
    """一次 run 的完整结果(可打分与否的判定字段)。"""

    status: str  # "ok" | "failed"
    failure_code: str | None  # None | "launch_failed" | "version_changed"
    session_id: str
    workspace: Path
    trace_file: Path | None
    final_text: str
    duration_ms: int
    cost: float | None = None
    tokens: dict | None = None
    md_sha256_before: str | None = None
    md_sha256_after: str | None = None
    exception: str | None = None


# ---------------------------------------------------------------------------
# 模型解析
# ---------------------------------------------------------------------------


def _default_models() -> list[dict]:
    """按 auto_harness/scheduler.py:62 规则取 models.defaults 条目列表。"""
    from jiuwenswarm.common.config import get_config, get_default_models

    return list(get_default_models(get_config()))


def _legacy_model_entry() -> dict | None:
    """config 无 models.defaults 时退回 react 段(同 scheduler._build_model_cache)。"""
    from jiuwenswarm.common.config import get_config

    config = get_config()
    mcc = dict(
        (config.get("models", {}).get("default", {}).get("model_client_config")
         or config.get("react", {}).get("model_client_config") or {})
    )
    if not mcc:
        return None
    name = mcc.get("model_name") or config.get("react", {}).get("model_name") or "gpt-4"
    mcc["model_name"] = name
    return {"model_client_config": mcc, "model_config_obj": (
        config.get("models", {}).get("default", {}).get("model_config_obj")
        or config.get("react", {}).get("model_config_obj") or {})}


def _build_model(entry: dict):
    """单个 defaults 条目 -> Model。共享 interface_deep 的模块级构造函数
    (build_model_from_entry,interface_deep.py:759;非 adapter 类/实例方法)。"""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    mcc = dict(entry.get("model_client_config") or {})
    mco = entry.get("model_config_obj") or {}
    return build_model_from_entry(mcc, mco)


def _resolve_model(model_name: str | None) -> tuple[object, str]:
    """按名称解析 Model;返回 (model, resolved_name)。名称缺失/未命中用首个 default。"""
    entries = _default_models()
    if not entries:
        legacy = _legacy_model_entry()
        if legacy is not None:
            entries = [legacy]
    if not entries:
        raise HarnessUsageError(
            "config 未配置任何模型(models.defaults / react.model_client_config)"
        )

    def _entry_name(entry: dict) -> str:
        return str((entry.get("model_client_config") or {}).get("model_name") or "")

    entry = next((e for e in entries if _entry_name(e) == model_name), None) \
        if model_name else None
    if entry is None:
        entry = entries[0]
        if model_name:
            logger.warning(
                "[harness-evolve] 模型 %r 未在 config 中,回退到 %r",
                model_name, _entry_name(entry),
            )
    return _build_model(entry), _entry_name(entry)


# ---------------------------------------------------------------------------
# 工作区准备
# ---------------------------------------------------------------------------

def _install_skills_for(workspace: Path, skill_names: list[str] | None) -> None:
    """把被测 agent frontmatter skills 装进 ``<workspace>/skills/<name>/``。

    SkillUseRail 从 ``<workspace>/skills`` 扫描即挂载(openjiuwen factory
    ``_make_skill_rail``),故拷贝即安装,无需在 prompt 注册。
    技能源 = 主 agent 技能目录(common.utils.get_agent_skills_dir)。
    """
    if not skill_names:
        return
    from jiuwenswarm.common.utils import get_agent_skills_dir

    skills_root = Path(get_agent_skills_dir())
    for name in skill_names:
        src = skills_root / name
        if not src.is_dir():
            logger.warning("[harness-evolve] 技能 %r 不存在于 %s,跳过", name, skills_root)
            continue
        shutil.copytree(src, workspace / "skills" / name)


def _prepare_workspace_for(
    agent_name: str,
    benchmark_id: str,
    case_id: str,
    run_index: int,
    session_id: str,
    skill_names: list[str] | None,
) -> Path:
    """建唯一工作区:statement 拷贝 + skills 拷贝;rubric 绝不入内。"""
    ws = paths.run_workspace_dir(agent_name, benchmark_id, case_id, run_index, session_id)
    ws.mkdir(parents=True, exist_ok=False)

    statement_dir = paths.case_statement_dir(benchmark_id, case_id)
    if not statement_dir.is_dir():
        raise HarnessUsageError(f"statement 目录不存在: {statement_dir}")
    for item in statement_dir.iterdir():
        target = ws / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    _install_skills_for(ws, skill_names)
    return ws


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


def _make_trace_logger(session_id: str, request_id: str):
    """按 interface_deep.py:9494 模式构造 DebugTraceLogger(全 no-op 兜底)。"""
    from jiuwenswarm.server.runtime.debug_trace.config import DebugTraceSettings
    from jiuwenswarm.server.runtime.debug_trace.paths import debug_trace_file
    from jiuwenswarm.server.runtime.debug_trace.stream_logger import DebugTraceLogger

    settings = DebugTraceSettings(
        mode="agent", enabled=True, dump_enabled=True, otel_enabled=False
    )
    return DebugTraceLogger(
        file_path=debug_trace_file("agent", session_id),
        mode="agent",
        session_id=session_id,
        request_id=request_id,
        settings=settings,
    )


def _publish_trace_logger(session_id: str, logger_obj):
    """ContextVar + session 注册表双通道发布(interface_deep 同款)。"""
    from jiuwenswarm.server.runtime.debug_trace.context import (
        register_debug_trace_logger,
        set_debug_trace_logger,
    )

    token = set_debug_trace_logger(logger_obj)
    register_debug_trace_logger(session_id, logger_obj)
    return token


def _unpublish_trace_logger(token, session_id: str) -> None:
    from jiuwenswarm.server.runtime.debug_trace.context import (
        reset_debug_trace_logger,
        unregister_debug_trace_logger,
    )

    reset_debug_trace_logger(token)
    unregister_debug_trace_logger(session_id)


# ---------------------------------------------------------------------------
# 流文本提取(仅做证据,不做协议)
# ---------------------------------------------------------------------------


def _extract_stream_text(chunk) -> str:
    """从 stream chunk 提取最终文本(answer/llm_output/content_chunk 的 content)。"""
    try:
        if not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
            return ""
        chunk_type = chunk.type
        payload = chunk.payload
        if chunk_type == "answer" and isinstance(payload, dict):
            output = payload.get("output", {})
            content = output.get("output", "") if isinstance(output, dict) else str(output)
            return str(content or "")
        if chunk_type in ("llm_output", "content_chunk") and isinstance(payload, dict):
            return str(payload.get("content", "") or "")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# sysop
# ---------------------------------------------------------------------------


def _register_sysop(sysop_id: str):
    """注册并取得本 run 专属 SysOperation(restrict_to_sandbox 锁死工作区)。"""
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.sys_operation import (
        LocalWorkConfig,
        OperationMode,
        SysOperation,
        SysOperationCard,
    )

    card = SysOperationCard(
        id=sysop_id,
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None, restrict_to_sandbox=True),
    )
    result = Runner.resource_mgr.add_sys_operation(card)
    if result.is_err():
        raise LaunchFailure(f"sysop 注册失败: {result.msg()}")
    sysop = Runner.resource_mgr.get_sys_operation(sysop_id)
    if sysop is None:
        raise LaunchFailure(f"sysop 注册后取回失败: {sysop_id}")
    return sysop


def _unregister_sysop(sysop_id: str) -> None:
    from openjiuwen.core.runner import Runner

    try:
        Runner.resource_mgr.remove_sys_operation(sysop_id)
    except Exception as exc:
        logger.warning("[harness-evolve] sysop 注销失败 %s: %s", sysop_id, exc)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _make_session_id(benchmark_id: str, case_id: str, run_index: int) -> str:
    """格式 he-<bid8>-<case12>-r<nn>-<uuid8>。"""
    bid = benchmark_id.replace("-", "_")[:8]
    case = case_id.replace("-", "_")[:12]
    return f"he-{bid}-{case}-r{run_index:02d}-{uuid.uuid4().hex[:8]}"


async def run_single_case(
    agent_name: str,
    benchmark_id: str,
    case_id: str,
    run_index: int,
    *,
    model_name: str | None = None,
    debug_trace: bool = True,
    session_id: str | None = None,
) -> RunResult:
    """headless 执行一个 case 的单个 run。

    Args:
        agent_name: 被测自定义 agent(定义来自 AgentConfigService)。
        benchmark_id / case_id: benchmark 与 case(不要求 frozen,支持 pilot)。
        run_index: 从 1 计。

    Returns:
        RunResult(status="ok" 即表示可打分,与答案好坏无关;
        status="failed" 时 failure_code 为 launch_failed/version_changed)。
    """
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    agent = AgentConfigService().get_agent(agent_name)
    if agent is None:
        raise HarnessUsageError(f"被测 agent 不存在: {agent_name}")
    benchmark_store.read_config(benchmark_id)  # 存在性/可解析性检查
    if case_id not in benchmark_store.list_cases(benchmark_id):
        raise HarnessUsageError(f"case 不存在: {case_id}")

    model, resolved_model_name = _resolve_model(
        model_name or (agent.model if agent.model else None)
    )
    sid = session_id or _make_session_id(benchmark_id, case_id, run_index)
    request_id = f"he-req-{uuid.uuid4().hex[:8]}"
    sysop_id = f"he-{sid}"

    md_before = None
    if agent.file_path:
        try:
            md_before = agent_state.md_sha256(Path(agent.file_path))
        except OSError:
            md_before = None

    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.factory import create_deep_agent
    from openjiuwen.harness.workspace.workspace import Workspace
    from openjiuwen.core.single_agent import AgentCard

    ws = _prepare_workspace_for(
        agent_name, benchmark_id, case_id, run_index, sid, agent.skills
    )
    trace_file = None
    token = None
    sysop = None
    started = time.monotonic()
    instance: DeepAgent | None = None
    try:
        sysop = _register_sysop(sysop_id)
        card_id = f"he-eval-{sid}"
        tools = _build_tools(agent.tools, card_id)
        instance = create_deep_agent(
            model=model,
            card=AgentCard(name=agent.name, id=card_id),
            tool_owner_id=card_id,
            system_prompt=agent.prompt,
            tools=tools,
            skills=list(agent.skills or []),
            max_iterations=agent.max_iterations or 15,
            workspace=Workspace(root_path=str(ws), language="cn"),
            rails=[_build_sysop_rail()],
            sys_operation=sysop,
            language="cn",
            restrict_to_work_dir=True,
        )
        await instance.ensure_initialized()

        trace_logger = _make_trace_logger(sid, request_id) if debug_trace else None
        if trace_logger is not None:
            trace_logger.start_run(input_text=QUERY)
            token = _publish_trace_logger(sid, trace_logger)

        final_parts: list[str] = []
        try:
            async for chunk in instance.stream({"query": QUERY}):
                if trace_logger is not None:
                    trace_logger.feed(chunk)
                text = _extract_stream_text(chunk)
                if text:
                    final_parts.append(text)
        except asyncio.CancelledError:
            if trace_logger is not None:
                trace_logger.end_run(status="cancelled")
            raise
        except Exception as exc:
            if trace_logger is not None:
                trace_logger.end_run(status="error", error=exc)
            return RunResult(
                status="failed",
                failure_code="launch_failed",
                session_id=sid,
                workspace=ws,
                trace_file=trace_file,
                final_text="".join(final_parts),
                duration_ms=int((time.monotonic() - started) * 1000),
                md_sha256_before=md_before,
                md_sha256_after=None,
                exception=f"{type(exc).__name__}: {exc}",
            )
        if trace_logger is not None:
            trace_logger.end_run(status="ok")
            trace_file = paths.trace_path_for(sid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise LaunchFailure(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if token is not None:
            _unpublish_trace_logger(token, sid)
        if sysop is not None:
            _unregister_sysop(sysop_id)

    md_after = None
    if agent.file_path:
        try:
            md_after = agent_state.md_sha256(Path(agent.file_path))
        except OSError:
            md_after = None

    tokens = None
    if instance is not None:
        try:
            tokens = instance.get_context_usage(session_id=sid)
        except Exception:
            tokens = None

    # 轨迹机器统计:input/output tokens 精确求和 + 工具调用次数(打分员精算用)
    final_text = "".join(final_parts)
    stats = extract_trace_stats(trace_file)
    if stats:
        tokens = dict(tokens or {})
        tokens.update(stats)
    if tokens is not None:
        tokens["final_text_chars"] = len(final_text)

    if md_before is not None and md_after is not None and md_before != md_after:
        return RunResult(
            status="failed",
            failure_code="version_changed",
            session_id=sid,
            workspace=ws,
            trace_file=trace_file,
            final_text=final_text,
            duration_ms=int((time.monotonic() - started) * 1000),
            md_sha256_before=md_before,
            md_sha256_after=md_after,
            exception="评测期间被测 agent 定义被修改",
        )
    return RunResult(
        status="ok",
        failure_code=None,
        session_id=sid,
        workspace=ws,
        trace_file=trace_file,
        final_text=final_text,
        duration_ms=int((time.monotonic() - started) * 1000),
        tokens=tokens,
        md_sha256_before=md_before,
        md_sha256_after=md_after,
    )


def _build_tools(tool_names: list[str] | None, card_id: str) -> list:
    """frontmatter tools -> Tool 实例列表(文件工具由 SysOperationRail 派生,不构造)。

    必须传 Tool **实例**而非卡片:openjiuwen factory 只把 Tool 实例注册进
    Runner.resource_mgr(apply_deep_agent_parts 的 ability_manager.add_ability
    (card, tool)),传纯 ToolCard 会得到"可见但不可调用"的工具,调用时报
    "Tool instance not found in resource_mgr"。
    """
    from jiuwenswarm.harness_evolve.tools_registry import build_named_tools

    return list(build_named_tools(tool_names or ["*"], agent_id=card_id))


def _build_sysop_rail():
    """SysOperationRail:文件工具(Read/Bash/…)的宿主,绑定 live sys_operation。

    生产侧 interface_deep._build_filesystem_rail 同款做法;openjiuwen factory
    的默认 rail 列表不含它——不显式传 rails 时评测 agent 将没有任何文件工具,
    无法读取工作区里的 statement(README.md)。
    """
    from openjiuwen.harness.rails import SysOperationRail

    return SysOperationRail()


__all__ = [
    "RunResult",
    "QUERY",
    "run_single_case",
    "_make_session_id",
    "_resolve_model",
]
