# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""自定义 agent 跨机器迁移:导出 / 导入 / 删除(bundle = zip)。

bundle 结构(顶层单目录,与 benchmark-export 惯例一致)::

    <name>/
    ├── manifest.json   # schema_version/kind/name/description/source/
    │                   # exported_at/skills/files{相对路径: sha256}
    ├── agent.md        # 定义文件磁盘字节原样
    ├── sidecar/        # state.json 存在时才有:state.json + snapshots/v<N>.tar.gz
    └── skills/<skill>/ # frontmatter 声明的 skills(缺失告警跳过)

关键语义:
- 导入 = temp 解压(zip-slip 防护)→ 结构/完整性/语义三层校验 → 重名检查
  (create_agent 只保护内置同名,自定义同名会静默覆盖,必须自前置检查)→
  create_agent 落盘 → state.json 重写(md_path/md_sha256 指向新机)→ 快照
  commit(**改名导入重打 tar 修正身份字段**,否则 restore 会因 manifest.name
  不匹配拒绝且会写出旧名 md)→ skills 拷入(目标已存在跳过,绝不覆盖)→
  config 启用子代理(失败不阻断)。
- 删除 = --yes 确认门 + 顺序清理:config 条目最先(子代理绑定即刻失效)→
  可选 benchmark → harness 包(rmtree + rescan 隐式清元数据)→ sidecar
  目录(一棵树含 state/snapshots/workspaces)→ 定义文件最后删(保证可恢复)。

只读调用 AgentConfigService(create/delete/get);config 写盘一律走
config_registry 的动态路径函数(common.config 的 CONFIG_YAML_PATH 是模块
导入期绑定,测试隔离覆盖不到)。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from jiuwenswarm.common import utils
from jiuwenswarm.harness_evolve import agent_state, paths
from jiuwenswarm.harness_evolve.errors import HarnessUsageError
from jiuwenswarm.harness_evolve.schemas import now_utc_iso

logger = logging.getLogger(__name__)

_BUNDLE_SCHEMA_VERSION = 1
_BUNDLE_KIND = "custom-agent"
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,50}$")
_SNAPSHOT_NAME_RE = re.compile(r"^v(\d+)\.tar\.gz$")

# bundle 顶层目录名(与 benchmark-export 同约定:导入时须恰好一个)
_MANIFEST_NAME = "manifest.json"
_AGENT_MD_NAME = "agent.md"
_SIDECAR_DIR = "sidecar"


def _service():
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    return AgentConfigService()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def export_agent_bundle(name: str, out_path: str | Path) -> dict:
    """把自定义 agent 导出为迁移 bundle(zip)。

    Raises:
        HarnessUsageError: agent 不存在 / 内置 agent / 定义文件缺失。
    """
    agent = _service().get_agent(name)
    if agent is None:
        raise HarnessUsageError(f"agent 不存在: {name}")
    if agent.source == "builtin":
        raise HarnessUsageError(f"内置 agent 无需导出(主 agent 自带): {name}")
    md_path = Path(agent.file_path)
    if not md_path.is_file():
        raise HarnessUsageError(f"agent 定义文件缺失: {md_path}")

    out = Path(out_path)
    with tempfile.TemporaryDirectory(prefix="he-agent-export-") as tmp:
        staging = Path(tmp) / name
        staging.mkdir()
        # 1. 定义文件磁盘字节原样
        shutil.copy2(md_path, staging / _AGENT_MD_NAME)
        # 2. sidecar(state.json + 全部快照)
        snapshot_versions: list[int] = []
        if agent_state.state_exists(name):
            sidecar = staging / _SIDECAR_DIR
            sidecar.mkdir()
            shutil.copy2(paths.agent_state_file(name), sidecar / "state.json")
            snaps_dir = paths.snapshots_dir(name)
            if snaps_dir.is_dir():
                snaps_out = sidecar / "snapshots"
                snaps_out.mkdir()
                for snap in sorted(snaps_dir.glob("v*.tar.gz")):
                    shutil.copy2(snap, snaps_out / snap.name)
                    m = _SNAPSHOT_NAME_RE.fullmatch(snap.name)
                    if m:
                        snapshot_versions.append(int(m.group(1)))
        # 3. skills(frontmatter 声明的;缺失告警跳过)
        skills_root = utils.get_agent_skills_dir()
        skills_copied: list[str] = []
        skills_missing: list[str] = []
        for skill in agent.skills or []:
            src = skills_root / skill
            if not src.is_dir():
                skills_missing.append(skill)
                logger.warning("[agent_transfer] 技能目录不存在,跳过: %s", src)
                continue
            shutil.copytree(src, staging / "skills" / skill)
            skills_copied.append(skill)
        # 4. manifest(先算全文件 sha256,最后写 manifest.json 自身)
        files: dict[str, str] = {}
        for p in staging.rglob("*"):
            if p.is_file():
                rel = p.relative_to(staging).as_posix()
                files[rel] = agent_state.md_sha256(p)
        manifest = {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "kind": _BUNDLE_KIND,
            "name": name,
            "description": agent.description or "",
            "source": {"location": agent.source, "md_path": str(md_path)},
            "exported_at": now_utc_iso(),
            "skills": skills_copied,
            "files": files,
        }
        (staging / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        archive = Path(
            shutil.make_archive(
                str(out.with_suffix("")),
                "zip",
                root_dir=str(staging.parent),
                base_dir=name,
            )
        )
    return {
        "name": name,
        "zip": str(archive),
        "snapshots": snapshot_versions,
        "skills": skills_copied,
        "skills_missing": skills_missing,
        "source_location": agent.source,
        "source_md_path": str(md_path),
        "exported_at": manifest["exported_at"],
        "files_count": len(files),
    }


# ---------------------------------------------------------------------------
# 导入:bundle 校验
# ---------------------------------------------------------------------------


def _read_tar_member(tar: tarfile.TarFile, member: str | None) -> bytes:
    if not member:
        raise HarnessUsageError("bundle 损坏: 快照 manifest 缺 md_rel")
    try:
        info = tar.getmember(member)
    except KeyError as exc:
        raise HarnessUsageError(f"bundle 损坏: 快照缺成员 {member}") from exc
    fh = tar.extractfile(info)
    if fh is None:
        raise HarnessUsageError(f"bundle 损坏: 快照成员不可读 {member}")
    return fh.read()


def _validate_snapshot_tar(tar_path: Path, name: str, version: int) -> None:
    """校验快照 tar 内部一致性(镜像 agent_state._read_manifest 语义,路径参数化)。"""
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            manifest = json.loads(_read_tar_member(tar, "manifest.json"))
            if manifest.get("name") != name or manifest.get("version") != version:
                raise HarnessUsageError(
                    f"bundle 损坏: 快照 v{version} manifest 身份不符"
                )
            md_bytes = _read_tar_member(tar, manifest.get("md_rel"))
            if _sha256_bytes(md_bytes) != manifest.get("md_sha256"):
                raise HarnessUsageError(
                    f"bundle 损坏: 快照 v{version} md sha256 不符"
                )
    except HarnessUsageError:
        raise
    except (tarfile.TarError, json.JSONDecodeError) as exc:
        raise HarnessUsageError(f"bundle 损坏: 快照 v{version} 不可读({exc})") from exc


def _validate_bundle_structure(extract_dir: Path) -> tuple[Path, dict]:
    """结构层校验:顶层单目录 + manifest 可解析 + 目录名/name 一致。"""
    top_dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(top_dirs) != 1:
        raise HarnessUsageError("bundle 顶层必须恰好一个目录")
    bundle_dir = top_dirs[0]
    manifest_path = bundle_dir / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise HarnessUsageError("bundle 缺少 manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessUsageError(f"bundle 损坏: manifest.json 解析失败({exc})") from exc
    if manifest.get("schema_version") != _BUNDLE_SCHEMA_VERSION or manifest.get(
        "kind"
    ) != _BUNDLE_KIND:
        raise HarnessUsageError("bundle 损坏: 不支持的格式版本")
    if not isinstance(manifest.get("name"), str) or bundle_dir.name != manifest["name"]:
        raise HarnessUsageError("bundle 损坏: 顶层目录名与 manifest.name 不符")
    if not _AGENT_NAME_RE.fullmatch(manifest["name"]):
        raise HarnessUsageError(f"bundle 损坏: 非法 agent 名 {manifest['name']!r}")
    if not (bundle_dir / _AGENT_MD_NAME).is_file():
        raise HarnessUsageError("bundle 缺少 agent.md")
    return bundle_dir, manifest


def _verify_files(bundle_dir: Path, manifest: dict) -> None:
    """完整性层校验:manifest.files 每个条目存在且 sha256 一致。"""
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise HarnessUsageError("bundle 损坏: manifest.files 为空")
    bundle_root = bundle_dir.resolve()
    for rel, sha in files.items():
        p = (bundle_dir / rel).resolve()
        if not p.is_relative_to(bundle_root) or not p.is_file():
            raise HarnessUsageError(f"bundle 损坏: 文件缺失 {rel}")
        if agent_state.md_sha256(p) != sha:
            raise HarnessUsageError(f"bundle 损坏: sha256 不符 {rel}")


def _validate_sidecar_semantics(
    bundle_dir: Path, manifest: dict
) -> list[int]:
    """语义层校验:state↔快照一致性;逐快照 tar 内部校验。返回快照版本列表。"""
    sidecar = bundle_dir / _SIDECAR_DIR
    state_file = sidecar / "state.json"
    snaps_dir = sidecar / "snapshots"
    snap_versions: list[int] = []
    if snaps_dir.is_dir():
        for p in sorted(snaps_dir.glob("*.tar.gz")):
            m = _SNAPSHOT_NAME_RE.fullmatch(p.name)
            if not m:
                raise HarnessUsageError(f"bundle 损坏: 非法快照文件名 {p.name}")
            snap_versions.append(int(m.group(1)))
    if not snap_versions:
        return []
    if not state_file.is_file():
        raise HarnessUsageError("bundle 损坏: 有快照但缺 sidecar/state.json")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessUsageError(f"bundle 损坏: state.json 解析失败({exc})") from exc
    if state.get("schema_version") != 1:
        raise HarnessUsageError("bundle 损坏: state.json schema 版本不支持")
    version = state.get("version")
    if not isinstance(version, int) or version < 1:
        raise HarnessUsageError("bundle 损坏: state.json version 非法")
    if not isinstance(state.get("next_version"), int) or state["next_version"] < version + 1:
        raise HarnessUsageError("bundle 损坏: state.json next_version 非法")
    for v in snap_versions:
        if not 1 <= v <= version:
            raise HarnessUsageError(
                f"bundle 损坏: 快照 v{v} 超出 state.version={version}"
            )
        _validate_snapshot_tar(snaps_dir / f"v{v}.tar.gz", manifest["name"], v)
    return snap_versions


# ---------------------------------------------------------------------------
# 导入:落地
# ---------------------------------------------------------------------------


def _rewrite_frontmatter_name(md_bytes: bytes, new_name: str) -> bytes:
    """只替换首个 frontmatter 块内的 ``name:`` 行;找不到 → 判损坏。"""
    text = md_bytes.decode("utf-8")
    if not text.startswith("---"):
        raise HarnessUsageError("bundle 损坏: 快照内 md 无 frontmatter,无法改名")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise HarnessUsageError("bundle 损坏: 快照内 md frontmatter 不完整,无法改名")
    frontmatter, count = re.subn(
        r"(?m)^name:.*$", f"name: {new_name}", parts[1], count=1
    )
    if count == 0:
        raise HarnessUsageError("bundle 损坏: 快照内 md 的 frontmatter 无 name 字段")
    return ("---" + frontmatter + "---" + parts[2]).encode("utf-8")


def _repack_snapshot_for_rename(
    src_tar: Path, version: int, new_name: str
) -> Path:
    """改名导入时重打快照 tar:修正 manifest.name/md_rel 与 md 内 frontmatter name。

    源 tar 已通过 _validate_snapshot_tar 校验;重打后写入最终位置(原子),
    再用同一校验器对结果回读自校验。
    """
    with tarfile.open(src_tar, "r:gz") as tar:
        manifest = json.loads(_read_tar_member(tar, "manifest.json"))
        md_bytes = _read_tar_member(tar, manifest.get("md_rel"))
        orig_mtime = tar.getmember(manifest["md_rel"]).mtime
    new_md = _rewrite_frontmatter_name(md_bytes, new_name)
    new_manifest = {
        **manifest,
        "name": new_name,
        "md_rel": f"{new_name}.md",
        "md_sha256": _sha256_bytes(new_md),
    }
    dst = paths.snapshot_file(new_name, version)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst.with_name(dst.name + ".tmp")
    with tarfile.open(tmp_path, "w:gz") as tar:
        md_info = tarfile.TarInfo(f"{new_name}.md")
        md_info.size = len(new_md)
        md_info.mtime = orig_mtime
        tar.addfile(md_info, io.BytesIO(new_md))
        manifest_bytes = (
            json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        m_info = tarfile.TarInfo("manifest.json")
        m_info.size = len(manifest_bytes)
        tar.addfile(m_info, io.BytesIO(manifest_bytes))
    os.replace(tmp_path, dst)
    _validate_snapshot_tar(dst, new_name, version)
    return dst


def _build_create_params(parsed: Any, target: str):
    """AgentDefinition(来自 service 自己的 _parse_agent_file)→ CreateAgentParams。"""
    from jiuwenswarm.server.runtime.agent_config_service import CreateAgentParams

    return CreateAgentParams(
        name=target,
        description=parsed.description or "",
        prompt=parsed.prompt,
        location="user",
        model=parsed.model,
        tools=parsed.tools,
        color=parsed.color,
        permission_mode=parsed.permission_mode,
        memory_scope=parsed.memory_scope,
        disallowed_tools=parsed.disallowed_tools or None,
        when_to_use=parsed.when_to_use,
        max_iterations=parsed.max_iterations,
        skills=parsed.skills,
    )


def _install_skills(bundle_dir: Path, manifest: dict) -> tuple[list[str], list[str], list[str]]:
    """bundle 内 skills 拷入用户技能目录;目标已存在 → 跳过,绝不覆盖。"""
    skills_root = utils.get_agent_skills_dir()
    copied: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for skill in manifest.get("skills") or []:
        src = bundle_dir / "skills" / skill
        if not src.is_dir():
            missing.append(skill)
            logger.warning("[agent_transfer] bundle 缺技能目录,跳过: %s", src)
            continue
        dst = skills_root / skill
        if dst.exists():
            skipped.append(skill)
            logger.warning("[agent_transfer] 目标机已有同名技能,跳过不覆盖: %s", dst)
            continue
        shutil.copytree(src, dst)
        copied.append(skill)
    return copied, skipped, missing


def import_agent_bundle(zip_path: str | Path, new_name: str | None = None) -> dict:
    """导入迁移 bundle;同名已存在 → 拒绝(可用 --as 改名导入)。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise HarnessUsageError(f"bundle 文件不存在: {zip_path}")
    target = (new_name or "").strip() or None

    with tempfile.TemporaryDirectory(prefix="he-agent-import-") as tmp:
        extract = Path(tmp) / "extract"
        extract.mkdir()
        # zip-slip 防护:每个成员解析后必须落在 extract 内
        extract_root = extract.resolve()
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                dest = (extract / member.filename).resolve()
                if not dest.is_relative_to(extract_root):
                    raise HarnessUsageError(
                        f"bundle 损坏: 非法路径 {member.filename}"
                    )
            zf.extractall(extract)
        bundle_dir, manifest = _validate_bundle_structure(extract)
        _verify_files(bundle_dir, manifest)
        snap_versions = _validate_sidecar_semantics(bundle_dir, manifest)

        if target is None:
            target = manifest["name"]
        if not _AGENT_NAME_RE.fullmatch(target):
            raise HarnessUsageError(f"名称格式无效: {target!r}")

        # 重名检查(create_agent 只保护内置同名,自定义同名会静默覆盖)
        service = _service()
        if service.get_agent(target) is not None:
            raise HarnessUsageError(f"agent 已存在: {target}(可用 --as 改名导入)")

        # 用 service 自己的解析器读 bundle 内 md(保证与官方格式同构)
        from jiuwenswarm.server.runtime.agent_config_service import _parse_agent_file

        parsed = _parse_agent_file(bundle_dir / _AGENT_MD_NAME, source="user")
        if parsed is None or parsed.name != manifest["name"]:
            raise HarnessUsageError(
                "bundle 损坏: agent.md 无法解析或 name 与 manifest 不符"
            )
        try:
            created = service.create_agent(_build_create_params(parsed, target))
        except ValueError as exc:
            raise HarnessUsageError(str(exc)) from exc

        cleanup: list = [lambda: service.delete_agent(target)]
        try:
            new_md_path = Path(created.file_path)
            # state.json:重写 md_path/md_sha256 指向新机,版本原样保留
            bundle_state_file = bundle_dir / _SIDECAR_DIR / "state.json"
            if bundle_state_file.is_file():
                state = json.loads(bundle_state_file.read_text(encoding="utf-8"))
                state["md_path"] = str(new_md_path)
                state["md_sha256"] = agent_state.md_sha256(new_md_path)
                state["updated_at"] = now_utc_iso()
                agent_state._write_state_atomic(paths.agent_state_file(target), state)
            else:
                agent_state.ensure_agent_state(target, new_md_path)
            cleanup.append(
                lambda: shutil.rmtree(paths.agent_dir(target), ignore_errors=True)
            )
            # 快照 commit:同名字节原样拷贝;改名重打 tar 修正身份
            renamed = target != manifest["name"]
            for version in snap_versions:
                src_tar = bundle_dir / _SIDECAR_DIR / "snapshots" / f"v{version}.tar.gz"
                if renamed:
                    _repack_snapshot_for_rename(src_tar, version, target)
                else:
                    paths.snapshots_dir(target).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_tar, paths.snapshot_file(target, version))
                    _validate_snapshot_tar(
                        paths.snapshot_file(target, version), target, version
                    )
            # skills(已存在跳过,绝不覆盖)
            skills_copied, skills_skipped, skills_missing = _install_skills(
                bundle_dir, manifest
            )
            # config 启用子代理(失败不阻断导入)
            from jiuwenswarm.harness_evolve.config_registry import (
                enable_subagent_in_config,
            )

            enabled, enable_msg = enable_subagent_in_config(target)
        except Exception:
            for fn in reversed(cleanup):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 — 回滚尽力而为
                    logger.warning("[agent_transfer] 导入回滚失败: %s", exc)
            raise

    final_state = agent_state.get_agent_state(target)
    return {
        "name": target,
        "source_name": manifest["name"],
        "renamed": renamed,
        "file_path": str(new_md_path),
        "version": final_state["version"],
        "snapshots": snap_versions,
        "skills": skills_copied,
        "skills_skipped_existing": skills_skipped,
        "skills_missing": skills_missing,
        "subagent_enabled": enabled,
        "subagent_enable_msg": enable_msg,
    }


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def _scan_referencing_benchmarks(name: str) -> list[str]:
    """扫描 benchmark_config.toml 中 test_agent == name 的 benchmark。"""
    from jiuwenswarm.harness_evolve import benchmark_store

    refs: list[str] = []
    benchmarks_dir = paths.benchmarks_dir()
    if not benchmarks_dir.is_dir():
        return refs
    for cfg in sorted(benchmarks_dir.glob("*/benchmark_config.toml")):
        bid = cfg.parent.name
        try:
            if benchmark_store.read_config(bid).test_agent == name:
                refs.append(bid)
        except Exception as exc:  # noqa: BLE001 — 单个 benchmark 损坏不阻断
            logger.warning("[agent_transfer] benchmark %s 读取失败: %s", bid, exc)
    return refs


def collect_delete_plan(name: str) -> dict:
    """只读枚举删除将影响的所有项;不存在/内置 → HarnessUsageError。"""
    agent = _service().get_agent(name)
    if agent is None:
        raise HarnessUsageError(f"agent 不存在: {name}")
    if agent.source == "builtin":
        raise HarnessUsageError(f"不能删除内置 agent: {name}")

    sidecar: dict[str, Any] = {"exists": False}
    agent_dir = paths.agent_dir(name)
    if agent_dir.is_dir():
        sidecar["exists"] = True
        sidecar["state"] = paths.agent_state_file(name).is_file()
        sidecar["snapshots"] = sorted(
            int(m.group(1))
            for p in paths.snapshots_dir(name).glob("v*.tar.gz")
            if (m := _SNAPSHOT_NAME_RE.fullmatch(p.name))
        ) if paths.snapshots_dir(name).is_dir() else []
        ws = paths.agent_workspaces_dir(name)
        sidecar["workspaces"] = (
            sum(1 for _ in ws.glob("*")) if ws.is_dir() else 0
        )

    from jiuwenswarm.harness_evolve import package_exporter

    pkg_dir = package_exporter.package_dir(name)
    # config 条目(只读检查,不写盘)
    config_entry = False
    from jiuwenswarm.harness_evolve.config_registry import user_config_path
    from jiuwenswarm.common.config import load_yaml_round_trip

    cfg_path = user_config_path()
    if cfg_path.is_file():
        try:
            data = load_yaml_round_trip(cfg_path)
            react = data.get("react") if isinstance(data, dict) else None
            if (
                isinstance(react, dict)
                and isinstance(react.get("subagents"), dict)
                and name in react["subagents"]
            ):
                config_entry = True
        except Exception as exc:  # noqa: BLE001 — 只读检查失败不阻断
            logger.warning("[agent_transfer] config 读取失败: %s", exc)

    return {
        "name": name,
        "md": {"exists": bool(agent.file_path), "path": agent.file_path or ""},
        "sidecar": sidecar,
        "package": {"exists": pkg_dir.is_dir(), "path": str(pkg_dir)},
        "config_entry": config_entry,
        "benchmarks": _scan_referencing_benchmarks(name),
    }


def execute_delete(name: str, *, with_benchmarks: bool) -> dict:
    """执行删除(config → benchmark → 包 → sidecar → md 最后)。

    单步失败不中止其余;定义文件最后删,保证任何前置失败时 agent 仍可恢复。
    """
    plan = collect_delete_plan(name)
    result: dict[str, Any] = {
        "name": name,
        "deleted": {},
        "removed_benchmarks": [],
        "benchmarks_kept": list(plan["benchmarks"]),
        "warnings": [],
    }

    # 1. config 条目最先:子代理绑定即刻失效
    from jiuwenswarm.harness_evolve.config_registry import remove_subagent_from_config

    ok, msg = remove_subagent_from_config(name)
    result["deleted"]["config_entry"] = ok
    if not ok:
        result["warnings"].append(msg)

    # 2. benchmark(默认保留 + 告警;--with-benchmarks 一并删除)
    for bid in plan["benchmarks"]:
        if not with_benchmarks:
            result["warnings"].append(f"benchmark '{bid}' 仍引用已删除的 agent(已保留)")
            continue
        try:
            shutil.rmtree(paths.benchmark_dir(bid))
            result["removed_benchmarks"].append(bid)
            result["benchmarks_kept"].remove(bid)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"删除 benchmark '{bid}' 失败: {exc}")

    # 3. harness 包:rmtree + rescan(隐式清 harness-packages.json 条目)
    from jiuwenswarm.harness_evolve import package_exporter

    pkg_dir = package_exporter.package_dir(name)
    if pkg_dir.is_dir():
        try:
            shutil.rmtree(pkg_dir)
            data = package_exporter._scan_packages()
            package_exporter._save_packages_metadata(data)
            result["deleted"]["package"] = True
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"删除 harness 包失败: {exc}")

    # 4. sidecar 一棵树(state/snapshots/workspaces)
    try:
        if paths.agent_dir(name).is_dir():
            shutil.rmtree(paths.agent_dir(name))
        result["deleted"]["sidecar"] = True
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(f"删除 sidecar 失败: {exc}")

    # 5. 定义文件最后删
    try:
        result["deleted"]["md"] = _service().delete_agent(name)
    except ValueError as exc:
        raise HarnessUsageError(str(exc)) from exc
    if not result["deleted"].get("md"):
        result["warnings"].append("定义文件不存在或未删除")

    return result
