# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent sidecar 状态:版本、快照、恢复、编辑(演进对象 = 自定义 agent 定义 *.md)。

版本语义(Penguin agent-optimization 规则):
- ``version`` = 当前 *.md 内容对应的版本(scoreboard 引用它);
- ``next_version`` = 下一个候选版本号,严格单调,被拒候选版本不复用
  (restore 回退后 next_version 不回退);
- 编辑前必须有 ``snapshots/v<expected_version>.tar.gz``(后端强制);
- 快照禁覆盖(同版本存在即拒绝),原子写入(tmp + rename);
- 恢复 = 还原文件字节 + sha256 校验 + version 回退,恢复后再校验。

state.json(``~/.jiuwenswarm/harness-evolve/agents/<name>/state.json``)::

    {
      "schema_version": 1,
      "version": 2,
      "next_version": 3,
      "md_path": "<agents 目录>/<name>.md",
      "md_sha256": "...",
      "baseline_version": null,
      "updated_at": "2026-08-14T09:30:00Z"
    }
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

from jiuwenswarm.harness_evolve import paths
from jiuwenswarm.harness_evolve.errors import (
    AgentStateError,
    HarnessUsageError,
    SnapshotError,
)
from jiuwenswarm.harness_evolve.schemas import now_utc_iso

__all__ = [
    "STATE_SCHEMA_VERSION",
    "state_exists",
    "get_agent_state",
    "ensure_agent_state",
    "agent_version",
    "baseline_version",
    "record_baseline",
    "md_sha256",
    "create_snapshot",
    "restore_snapshot",
    "apply_agent_edit",
]

STATE_SCHEMA_VERSION = 1

# apply_agent_edit 允许修改的字段白名单(其余 frontmatter 字段拒绝)
EDITABLE_FIELDS = (
    "prompt",
    "description",
    "model",
    "tools",
    "skills",
    "max_iterations",
    "when_to_use",
)


def md_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_state_atomic(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with open(tmp_path, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp_path, state_path)


def state_exists(name: str) -> bool:
    return paths.agent_state_file(name).is_file()


def get_agent_state(name: str) -> dict[str, Any]:
    path = paths.agent_state_file(name)
    if not path.is_file():
        raise AgentStateError(
            f"agent 无 harness_evolve 状态:{name}(先 agent-register 或 ensure 初始化)"
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentStateError(f"state.json 解析失败({name}): {exc}") from exc
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise AgentStateError(
            f"state.json schema 版本不支持({name}): {state.get('schema_version')}"
        )
    return state


def ensure_agent_state(name: str, md_path: str | Path) -> dict[str, Any]:
    """惰性初始化 state.json(version=1);已存在则原样返回。"""
    if state_exists(name):
        return get_agent_state(name)
    md_path = Path(md_path)
    if not md_path.is_file():
        raise AgentStateError(f"agent md 文件不存在: {md_path}")
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "version": 1,
        "next_version": 2,
        "md_path": str(md_path),
        "md_sha256": md_sha256(md_path),
        "baseline_version": None,
        "updated_at": now_utc_iso(),
    }
    _write_state_atomic(paths.agent_state_file(name), state)
    return state


def agent_version(name: str) -> int:
    return int(get_agent_state(name)["version"])


def baseline_version(name: str) -> int | None:
    return get_agent_state(name).get("baseline_version")


def record_baseline(name: str, version: int) -> dict[str, Any]:
    """记录 benchmark 冻结时的 agent 基线版本。"""
    state = get_agent_state(name)
    state["baseline_version"] = version
    state["updated_at"] = now_utc_iso()
    _write_state_atomic(paths.agent_state_file(name), state)
    return state


# ── 快照 ──────────────────────────────────────────────────────────────────


def create_snapshot(name: str, version: int) -> Path:
    """创建 v<version>.tar.gz(原子;存在即拒绝,禁覆盖)。

    内含 ``<name>.md``(字节原样)+ ``manifest.json``。
    """
    state = get_agent_state(name)
    if version != int(state["version"]):
        raise SnapshotError(
            f"快照版本必须等于当前内容版本 {state['version']},得到 {version}"
        )
    out = paths.snapshot_file(name, version)
    if out.exists():
        raise SnapshotError(f"快照已存在,禁止覆盖: {out}")
    md_path = Path(state["md_path"])
    if not md_path.is_file():
        raise SnapshotError(f"agent md 文件不存在: {md_path}")

    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": version,
        "md_sha256": md_sha256(md_path),
        "time": now_utc_iso(),
        "md_rel": md_path.name,
    }
    tmp_path = out.with_name(out.name + ".tmp")
    with tarfile.open(tmp_path, "w:gz") as tar:
        md_info = tar.gettarinfo(str(md_path), arcname=manifest["md_rel"])
        md_info.mtime = int(md_path.stat().st_mtime)
        with open(md_path, "rb") as fh:
            tar.addfile(md_info, fh)
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))
    os.replace(tmp_path, out)
    return out


def _read_manifest(name: str, version: int) -> tuple[dict, bytes]:
    out = paths.snapshot_file(name, version)
    if not out.is_file():
        raise SnapshotError(f"快照不存在: {out}")
    try:
        with tarfile.open(out, "r:gz") as tar:
            manifest_member = tar.extractfile("manifest.json")
            if manifest_member is None:
                raise SnapshotError(f"快照缺少 manifest.json: {out}")
            manifest = json.loads(manifest_member.read().decode("utf-8"))
            md_member = tar.extractfile(manifest["md_rel"])
            if md_member is None:
                raise SnapshotError(
                    f"快照缺少 {manifest['md_rel']}: {out}"
                )
            md_bytes = md_member.read()
    except (tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"快照损坏({name} v{version}): {exc}") from exc
    if manifest.get("version") != version or manifest.get("name") != name:
        raise SnapshotError(f"快照 manifest 与请求不一致({name} v{version})")
    return manifest, md_bytes


def restore_snapshot(name: str, version: int) -> dict[str, Any]:
    """回滚到 v<version> 快照:还原字节 + sha256 校验 + 版本回退。

    ``next_version`` 不回退(被拒候选版本不复用)。
    """
    manifest, md_bytes = _read_manifest(name, version)
    state = get_agent_state(name)
    md_path = Path(state["md_path"])
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "wb") as fh:
        fh.write(md_bytes)
        os.fsync(fh.fileno())

    # 恢复后再校验(Penguin 回滚验证)
    if md_sha256(md_path) != manifest["md_sha256"]:
        raise SnapshotError(f"恢复后 sha256 校验失败({name} v{version})")

    state["version"] = version
    state["next_version"] = max(int(state["next_version"]), version + 1)
    state["md_sha256"] = manifest["md_sha256"]
    state["updated_at"] = now_utc_iso()
    _write_state_atomic(paths.agent_state_file(name), state)
    return state


# ── 编辑 ──────────────────────────────────────────────────────────────────


def apply_agent_edit(
    name: str,
    *,
    expected_version: int,
    force: bool = False,
    prompt: str | None = None,
    description: str | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    max_iterations: int | None = None,
    when_to_use: str | None = None,
) -> dict[str, Any]:
    """以候选版本方式修改 agent 定义(前置快照 + 漂移检测 + 版本单调)。

    - 前置:``snapshots/v<expected_version>.tar.gz`` 必须存在(Penguin "编辑前
      必须有快照" 的后端强制);
    - md 文件相对 state 记录被外部修改(sha256 漂移)→ 拒绝,``force=True``
      时按当前文件重新对齐后继续;
    - 经 ``AgentConfigService.update_agent`` 写回(canonical 格式),不改
      任何现有模块行为;
    - 新版本号 = 当前 ``next_version``,之后单调 +1。
    """
    from jiuwenswarm.server.runtime.agent_config_service import (
        AgentConfigService,
        UpdateAgentParams,
    )

    if not any(
        v is not None
        for v in (
            prompt,
            description,
            model,
            tools,
            skills,
            max_iterations,
            when_to_use,
        )
    ):
        raise HarnessUsageError("agent-edit 未提供任何修改字段")

    state = get_agent_state(name)
    if int(state["version"]) != expected_version:
        raise AgentStateError(
            f"期望基于版本 {expected_version},当前版本 {state['version']}"
        )
    snapshot = paths.snapshot_file(name, expected_version)
    if not snapshot.is_file():
        raise AgentStateError(
            f"编辑前必须存在快照 {snapshot} (snapshot-create)"
        )

    md_path = Path(state["md_path"])
    if not md_path.is_file():
        raise AgentStateError(f"agent md 文件不存在: {md_path}")
    if md_sha256(md_path) != state["md_sha256"]:
        if not force:
            raise AgentStateError(
                f"agent md 文件被外部修改过(sha256 漂移),需 --force 对齐后重试"
            )

    params = UpdateAgentParams(
        prompt=prompt,
        description=description,
        model=model,
        tools=tools,
        skills=skills,
        max_iterations=max_iterations,
        when_to_use=when_to_use,
    )
    try:
        updated = AgentConfigService().update_agent(name, params)
    except ValueError as exc:
        # get_agent 解析失败 / 内置 agent / 无文件路径 等
        raise AgentStateError(f"agent-edit 失败(agent 可能已被外部破坏): {exc}") from exc

    new_md_path = Path(updated.file_path)
    new_sha = md_sha256(new_md_path)
    if prompt is not None and new_sha == state["md_sha256"]:
        # prompt 修改未生效(例如服务端规范化吞掉)→ 视为错误而非静默接受
        raise AgentStateError("agent-edit 后 md 内容未变化,写入未生效")

    new_version = int(state["next_version"])
    state["version"] = new_version
    state["next_version"] = new_version + 1
    state["md_path"] = str(new_md_path)
    state["md_sha256"] = new_sha
    state["updated_at"] = now_utc_iso()
    _write_state_atomic(paths.agent_state_file(name), state)
    return state
