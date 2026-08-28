# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent 打包导出为 Harness 包(注入主 agent)。

自定义 agent(AgentConfigService 定义)除作为子代理被主 agent 调度外,
还可打包成 ``~/.jiuwenswarm/auto-harness/runtime_extensions/<hash8>/<name>/``
形式的 Harness 包:构建期(interface_deep 的 _get_active_package_config_paths)
会自动加载激活包,web 端"Harness Package 管理"面板即可管理。

语义(与现有 harness 包机制一致):
- ``prompt_sections`` **叠加**到主 agent 的 system prompt,不是替换主 agent
- ``tools`` **不打包**:热加载绑定语义是"新增工具"(extension_binder 对同名言
  硬性抛错整体失败),而文件工具/WebSearch/WebFetch 主 agent 默认自带——
  声明必然冲突。原始工具列表记入 metadata 溯源
- ``skills`` 拷入包内(legacy 解析要求 dir 严格存在)
- frontmatter model 仅记入 metadata 溯源,不注入

激活语义(实测确认):web 面板热加载是唯一生效途径;agent 服务重启时
``reset_harness_packages_state``(agent_ws_server.py)会把激活状态清回 Native,
包文件保留、激活标记丢失,须重新到面板激活。

说明:元数据同步不依赖 AutoHarnessService(import 链 ~28s,CLI/测试不可接受),
此处实现等价的 ``scan_runtime_extensions`` 最小逻辑(照抄 service.py 的
按 runtime_path 保留 id/激活状态语义),写入同一个 harness-packages.json。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.common import utils
from jiuwenswarm.harness_evolve import agent_state
from jiuwenswarm.harness_evolve.errors import HarnessUsageError

logger = logging.getLogger(__name__)

# 主 agent 默认自带的工具名(文件工具 = SysOperation;web 工具 = free_search/fetch_webpage)。
# 热加载绑定语义是"新增工具",同名言硬冲突(extension_binder._bind_tool 整体抛错)——
# 因此这些一律不打包,仅记入 metadata 溯源。
_FILE_TOOL_NAMES = frozenset(
    ["Read", "Write", "Edit", "Bash", "LS", "Grep", "Glob", "LSP"]
)
_WEB_TOOL_NAMES = frozenset(["WebSearch", "WebFetch"])

_HARNESS_DATA_SUBDIR = "auto-harness"
_MANIFEST_NAME = "harness_config.yaml"


def _data_dir() -> Path:
    """auto-harness 数据根(每次调用时求值,尊重 set_user_home/JIUWENSWARM_DATA_DIR)。"""
    return utils.get_user_workspace_dir() / _HARNESS_DATA_SUBDIR


def _packages_file() -> Path:
    return _data_dir() / "harness-packages.json"


def package_dir(name: str) -> Path:
    """目标包目录:auto-harness/runtime_extensions/<sha256(name)[:8]>/<name>/。

    parent 目录名与 ``generate_package_id`` 的 parent_dir[:8] 规则对齐。
    """
    parent = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return _data_dir() / "runtime_extensions" / parent / name


# ---------------------------------------------------------------------------
# 元数据(harness-packages.json)等价最小实现 —— 照抄 AutoHarnessService 语义:
# scan 按 runtime_path 保留已有包条目(id/激活状态)→ 同名覆盖重导出 id 稳定。
# ---------------------------------------------------------------------------


def _default_metadata() -> dict[str, Any]:
    return {
        "packages": [],
        "active_package_ids": [],
        "native_version": {
            "id": "native",
            "extension_name": "Native Agent",
            "is_active": True,
        },
    }


def _load_packages_metadata() -> dict[str, Any]:
    path = _packages_file()
    if not path.is_file():
        return _default_metadata()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("[package_exporter] harness-packages.json 解析失败: %s", exc)
    return _default_metadata()


def _save_packages_metadata(data: dict[str, Any]) -> None:
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    _packages_file().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _generate_package_id(runtime_path: Path) -> str:
    """pkg_<parent_dir[:8]>_<name>_<timestamp>,与 service.generate_package_id 同格式。"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"pkg_{runtime_path.parent.name[:8]}_{runtime_path.name}_{timestamp}"


def _get_created_time(path: Path) -> str:
    """同 service._get_created_time:Windows 用 st_ctime,其余平台用 st_mtime。"""
    try:
        stat = path.stat()
        ts = stat.st_ctime if os.name == "nt" else stat.st_mtime
        return datetime.fromtimestamp(ts).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _scan_packages() -> dict[str, Any]:
    """扫描 runtime_extensions 并同步元数据(等价 service.scan_runtime_extensions)。"""
    runtime_root = _data_dir() / "runtime_extensions"
    existing = _load_packages_metadata()
    existing_packages = existing.get("packages", [])
    active_package_ids = existing.get("active_package_ids", [])

    by_path: dict[str, dict[str, Any]] = {}
    for pkg in existing_packages:
        path_key = pkg.get("runtime_path", "")
        if path_key:
            by_path[path_key] = deepcopy(pkg)

    packages: list[dict[str, Any]] = []
    discovered: set[str] = set()
    if runtime_root.is_dir():
        for path in runtime_root.glob("*/*"):
            if path.is_dir() and (path / _MANIFEST_NAME).is_file():
                resolved = str(path.resolve())
                discovered.add(resolved)
                existing_pkg = by_path.get(resolved)
                if existing_pkg:
                    # 已跟踪的包:保留全部既有元数据(id/激活状态)
                    packages.append(existing_pkg)
                else:
                    packages.append(
                        {
                            "id": _generate_package_id(path),
                            "extension_name": path.name,
                            "runtime_path": resolved,
                            "config_path": str((path / _MANIFEST_NAME).resolve()),
                            "created_at": _get_created_time(path),
                            "is_active": False,
                            "version_label": "",
                            "description": "",
                        }
                    )

    valid_active_ids = [
        pkg_id
        for pkg_id in active_package_ids
        if any(pkg.get("id") == pkg_id for pkg in packages)
    ]
    return {
        "packages": packages,
        "active_package_ids": valid_active_ids,
        "native_version": {
            "id": "native",
            "extension_name": "Native Agent",
            "is_active": len(valid_active_ids) == 0,
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------


def _build_manifest(agent: Any, state: dict[str, Any], skills: list[str]) -> dict[str, Any]:
    """构造 legacy harness_config.yaml 内容(顶层键须在 canonical 白名单内)。

    tools 恒为空:文件/web 工具主 agent 默认自带,声明会导致热加载绑定冲突
    (extension_binder 同名言硬抛错);原始工具列表记入 metadata 溯源。
    """
    declared_tools = list(agent.tools or [])
    skipped = []
    for name in declared_tools:
        if name in _FILE_TOOL_NAMES:
            skipped.append(name)
        elif name in _WEB_TOOL_NAMES:
            skipped.append(name)
        else:
            logger.warning(
                "[package_exporter] 未知工具名跳过(不入包): %s", name
            )
    return {
        "id": f"{agent.name}-harness",
        "name": agent.name,
        "description": agent.description or "",
        "prompt_sections": [
            {
                "name": f"{agent.name}-prompt",
                "content": {"cn": agent.prompt, "en": agent.prompt},
                "priority": 100,
            }
        ],
        "tools": [],
        "skills": [{"dir": f"skills/{skill}"} for skill in skills],
        "metadata": {
            "source": "harness_evolve",
            "agent_name": agent.name,
            "agent_version": state.get("version"),
            "agent_sha256": state.get("md_sha256"),
            "model": agent.model,
            "declared_tools": declared_tools,
            "skipped_tools": skipped,
        },
    }


def _copy_skills(skill_names: list[str], package: Path) -> list[str]:
    """把 frontmatter skills 拷入包内 skills/ 目录;目录缺失告警跳过。"""
    skills_root = utils.get_agent_skills_dir()
    copied: list[str] = []
    for skill in skill_names:
        src = skills_root / skill
        if not src.is_dir():
            logger.warning("[package_exporter] 技能目录不存在,跳过: %s", src)
            continue
        dst = package / "skills" / skill
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        copied.append(skill)
    return copied


def _self_check(package: Path) -> None:
    """用 openjiuwen 官方 loader 解析生成的 manifest,失败即抛错(旧包保留)。"""
    from openjiuwen.harness.resources.extension_loader import load_plugin_package

    spec = load_plugin_package(package / _MANIFEST_NAME)
    if not spec.prompt_sections:
        raise ValueError("解析出的 prompt_sections 为空")


def export_agent_package(name: str, *, activate: bool = False) -> dict:
    """把 agent 打包为 Harness 包(同名覆盖重导出,包 id 稳定)。

    Returns:
        包路径 / 包 id / 是否标记激活 / 生效提示等信息的 dict。
    """
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    agent = AgentConfigService().get_agent(name)
    if agent is None:
        raise HarnessUsageError(f"agent 不存在: {name}")
    state: dict[str, Any] = {}
    if agent_state.state_exists(name):
        state = agent_state.get_agent_state(name)

    target = package_dir(name)
    # 先在 staging(点前缀目录,pathlib glob 不匹配)构建+自校验,通过后再原子替换,
    # 保证自校验失败时旧包内容不被破坏。
    staging = target.parent / f".{target.name}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        skills = _copy_skills(agent.skills or [], staging)
        manifest = _build_manifest(agent, state, skills)
        (staging / _MANIFEST_NAME).write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        _self_check(staging)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise HarnessUsageError(f"打包失败(旧包未动): {exc}") from exc

    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)

    data = _scan_packages()
    pkg_id = next(
        (p["id"] for p in data["packages"] if p["runtime_path"] == str(target.resolve())),
        None,
    )
    if activate and pkg_id and pkg_id not in data["active_package_ids"]:
        data["active_package_ids"].append(pkg_id)
        data["native_version"]["is_active"] = False
    _save_packages_metadata(data)

    return {
        "name": agent.name,
        "package_dir": str(target),
        "manifest_path": str(target / _MANIFEST_NAME),
        "package_id": pkg_id,
        "activated": bool(activate),
        "skills": skills,
        "prompt_sections": len(manifest["prompt_sections"]),
        "tools": [],
        "skipped_tools": manifest["metadata"]["skipped_tools"],
        "note": "到 web 端 Harness Package 管理面板激活即热加载生效;"
        "agent 服务重启会清空激活回到 Native,需重新激活",
    }
