# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent_transfer 测试:自定义 agent 跨机迁移(bundle 导入/导出/删除)。

conftest 的 autouse fixture 已把 user home 重定向到 tmp_path,因此
``utils.get_user_workspace_dir()`` 返回 ``tmp_path/.jiuwenswarm``,
bundle 的 sidecar/config/skills 全部落在 tmp 内。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import utils
from jiuwenswarm.harness_evolve import agent_state, paths
from jiuwenswarm.harness_evolve.agent_transfer import (
    collect_delete_plan,
    execute_delete,
    export_agent_bundle,
    import_agent_bundle,
)
from jiuwenswarm.harness_evolve.errors import HarnessUsageError

PROMPT = """你是测试 agent。

# 职责
只做一件事:回答输入。
"""

PROMPT_V2 = """你是测试 agent v2。

# 职责
回答输入并给出证据。
"""


def _register(
    tmp_path,
    name="test-agent",
    *,
    tools=None,
    skills=None,
    model=None,
) -> None:
    """走 agent-register 完整链路创建自定义 agent(sidecar version=1)。"""
    from jiuwenswarm.harness_evolve.cli import main

    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT, encoding="utf-8")
    args = [
        "agent-register",
        "--name",
        name,
        "--description",
        "test agent",
        "--prompt-file",
        str(prompt),
        "--data-dir",
        str(tmp_path / "he"),
        "--json",
    ]
    if tools:
        args += ["--tools", ",".join(tools)]
    if skills:
        args += ["--skills", ",".join(skills)]
    if model:
        args += ["--model", model]
    assert main(args) == 0


def _make_skill(skill: str, content: str | None = None) -> None:
    d = utils.get_agent_skills_dir() / skill
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        content or f"# {skill}\n", encoding="utf-8"
    )


def _make_snapshot(name: str, version: int) -> None:
    """为 agent 创建编辑前快照并迭代到 v2(模拟已演进状态)。"""
    from jiuwenswarm.harness_evolve.cli import main

    assert agent_state.create_snapshot(name, version).is_file()


def _seed_config() -> None:
    """预置最小用户 config.yaml(使 enable/remove 真正写盘)。"""
    from jiuwenswarm.harness_evolve.config_registry import user_config_path

    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("react:\n  agent_name: main_agent\n", encoding="utf-8")


def _iterate(tmp_path, name: str, *, expected: int = 1) -> None:
    """snapshot-create + agent-edit 把 agent 推进到 v2。"""
    from jiuwenswarm.harness_evolve.cli import main

    assert (
        main(
            [
                "snapshot-create",
                "--name",
                name,
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        == 0
    )
    prompt_v2 = tmp_path / f"{name}-v2.md"
    prompt_v2.write_text(PROMPT_V2, encoding="utf-8")
    assert (
        main(
            [
                "agent-edit",
                "--name",
                name,
                "--expected-version",
                str(expected),
                "--prompt-file",
                str(prompt_v2),
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        == 0
    )


def _export(tmp_path, name="test-agent") -> dict:
    out = tmp_path / f"{name}-bundle"
    result = export_agent_bundle(name, out)
    return result


def _zip_names(zip_path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


class TestExport:
    def test_export_bundle_structure(self, tmp_path):
        _make_skill("sum-skill")
        _register(tmp_path, skills=["sum-skill"])
        _make_snapshot("test-agent", 1)
        result = _export(tmp_path)

        assert result["zip"].endswith(".zip")
        names = _zip_names(result["zip"])
        assert "test-agent/manifest.json" in names
        assert "test-agent/agent.md" in names
        assert "test-agent/sidecar/state.json" in names
        assert "test-agent/sidecar/snapshots/v1.tar.gz" in names
        assert "test-agent/skills/sum-skill/SKILL.md" in names
        assert result["snapshots"] == [1]
        assert result["skills"] == ["sum-skill"]

    def test_export_manifest_integrity_map(self, tmp_path):
        _register(tmp_path)
        result = _export(tmp_path)
        with zipfile.ZipFile(result["zip"]) as zf:
            manifest = json.loads(zf.read("test-agent/manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["kind"] == "custom-agent"
        assert manifest["name"] == "test-agent"
        # files 覆盖 agent.md + state.json(manifest 自身不在内)
        assert "agent.md" in manifest["files"]
        assert "sidecar/state.json" in manifest["files"]
        assert "manifest.json" not in manifest["files"]

    def test_export_missing_skill_skipped(self, tmp_path):
        _register(tmp_path, skills=["ghost-skill"])
        result = _export(tmp_path)
        assert result["skills"] == []
        assert result["skills_missing"] == ["ghost-skill"]
        names = _zip_names(result["zip"])
        assert not any("skills/" in n for n in names)

    def test_export_unknown_agent(self, tmp_path):
        with pytest.raises(HarnessUsageError):
            export_agent_bundle("no-such-agent", tmp_path / "x")

    def test_export_builtin_refused(self, tmp_path):
        with pytest.raises(HarnessUsageError):
            export_agent_bundle("general-purpose", tmp_path / "x")


class TestImport:
    def test_roundtrip_preserves_state(self, tmp_path):
        _make_skill("sum-skill")
        _register(tmp_path, skills=["sum-skill"])
        _iterate(tmp_path, "test-agent")  # v2
        bundle = _export(tmp_path)

        # 删掉原始,模拟"另一台机器"
        execute_delete("test-agent", with_benchmarks=False)
        # 目标机没有该 skill(删除后原 skill 还在,跳过场景见冲突测试)
        result = import_agent_bundle(bundle["zip"])
        assert result["renamed"] is False
        assert result["version"] == 2
        assert result["snapshots"] == [1]
        # state.json 重写:md_path/md_sha256 指向新文件
        state = agent_state.get_agent_state("test-agent")
        assert state["version"] == 2
        assert state["md_path"].startswith(str(utils.get_user_workspace_dir()))
        assert state["md_sha256"] == agent_state.md_sha256(state["md_path"])
        # 快照可恢复(同名导入字节原样)
        agent_state.restore_snapshot("test-agent", 1)

    def test_import_bare_agent_gets_v1(self, tmp_path):
        """bundle 无 sidecar(如 web 直接创建的裸定义)→ 导入自动建 v1。"""
        _register(tmp_path)
        # 模拟无 sidecar 的裸定义:删掉 sidecar 后再导出
        shutil.rmtree(paths.agent_dir("test-agent"))
        bundle = _export(tmp_path)
        assert bundle["snapshots"] == []
        execute_delete("test-agent", with_benchmarks=False)
        result = import_agent_bundle(bundle["zip"])
        assert result["version"] == 1
        assert result["snapshots"] == []
        assert paths.agent_state_file("test-agent").is_file()

    def test_import_as_rename(self, tmp_path):
        _register(tmp_path)
        _iterate(tmp_path, "test-agent")  # v2,快照 v1
        bundle = _export(tmp_path)
        execute_delete("test-agent", with_benchmarks=False)

        result = import_agent_bundle(bundle["zip"], new_name="renamed-agent")
        assert result["renamed"] is True
        assert result["name"] == "renamed-agent"
        # md frontmatter 已改名
        md = __import__("pathlib").Path(result["file_path"]).read_text(encoding="utf-8")
        assert "name: renamed-agent" in md
        # state.json 三处重写
        state = agent_state.get_agent_state("renamed-agent")
        assert state["version"] == 2
        assert state["md_path"] == result["file_path"]
        assert state["md_sha256"] == agent_state.md_sha256(result["file_path"])
        # 快照重打后可直接恢复,恢复出的 md 仍是新名(端到端)
        agent_state.restore_snapshot("renamed-agent", 1)
        md_v1 = __import__("pathlib").Path(result["file_path"]).read_text(
            encoding="utf-8"
        )
        assert "name: renamed-agent" in md_v1

    def test_import_same_name_conflict(self, tmp_path):
        _register(tmp_path)
        bundle = _export(tmp_path)
        before = paths.agent_dir("test-agent").joinpath("state.json").read_bytes()
        with pytest.raises(HarnessUsageError) as exc:
            import_agent_bundle(bundle["zip"])
        assert "--as" in str(exc.value)
        # 原 agent 未被覆盖
        assert paths.agent_dir("test-agent").joinpath("state.json").read_bytes() == before

    def test_import_builtin_name_refused(self, tmp_path):
        _register(tmp_path)
        bundle = _export(tmp_path)
        execute_delete("test-agent", with_benchmarks=False)
        with pytest.raises(HarnessUsageError):
            import_agent_bundle(bundle["zip"], new_name="general-purpose")

    def test_import_corrupt_manifest(self, tmp_path):
        _register(tmp_path)
        bundle = _export(tmp_path)
        _corrupt_zip(bundle["zip"], "test-agent/manifest.json", b"{not json")
        execute_delete("test-agent", with_benchmarks=False)
        with pytest.raises(HarnessUsageError, match="manifest"):
            import_agent_bundle(bundle["zip"])
        # 零残留
        agents_dir = utils.get_user_workspace_dir() / "agents"
        assert not (agents_dir / "test-agent.md").exists()
        assert not paths.agent_dir("test-agent").exists()

    def test_import_sha_mismatch(self, tmp_path):
        _register(tmp_path)
        bundle = _export(tmp_path)
        _corrupt_zip(bundle["zip"], "test-agent/agent.md", b"# tampered\n")
        execute_delete("test-agent", with_benchmarks=False)
        with pytest.raises(HarnessUsageError, match="sha256"):
            import_agent_bundle(bundle["zip"])

    def test_import_bad_snapshot_identity(self, tmp_path):
        _register(tmp_path)
        _make_snapshot("test-agent", 1)
        bundle = _export(tmp_path)
        # 把快照文件名改成 v9.tar.gz(超出 state.version → 语义校验拒绝)
        with zipfile.ZipFile(bundle["zip"], "r") as zf:
            data = {n: zf.read(n) for n in zf.namelist()}
        data["test-agent/sidecar/snapshots/v9.tar.gz"] = data.pop(
            "test-agent/sidecar/snapshots/v1.tar.gz"
        )
        # 同步改 manifest.files 键名(内容 sha 不变),让完整性层通过、语义层拦截
        manifest = json.loads(data["test-agent/manifest.json"])
        manifest["files"]["sidecar/snapshots/v9.tar.gz"] = manifest["files"].pop(
            "sidecar/snapshots/v1.tar.gz"
        )
        data["test-agent/manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        new_zip = tmp_path / "bad-snap.zip"
        with zipfile.ZipFile(new_zip, "w") as zf:
            for n, b in data.items():
                zf.writestr(n, b)
        execute_delete("test-agent", with_benchmarks=False)
        with pytest.raises(HarnessUsageError, match="v9"):
            import_agent_bundle(new_zip)

    def test_import_multi_top_dir_rejected(self, tmp_path):
        bad = tmp_path / "multi.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("a/manifest.json", "{}")
            zf.writestr("b/manifest.json", "{}")
        with pytest.raises(HarnessUsageError, match="顶层"):
            import_agent_bundle(bad)

    def test_import_zip_slip_rejected(self, tmp_path):
        bad = tmp_path / "slip.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", "x")
            zf.writestr("ok/manifest.json", "{}")
        with pytest.raises(HarnessUsageError, match="非法路径"):
            import_agent_bundle(bad)

    def test_import_skill_conflict_skipped(self, tmp_path):
        _make_skill("sum-skill", content="原始内容")
        _register(tmp_path, skills=["sum-skill"])
        bundle = _export(tmp_path)
        # 改本地 skill 内容,再导入 → 已存在跳过不覆盖
        (utils.get_agent_skills_dir() / "sum-skill" / "SKILL.md").write_text(
            "本地新内容", encoding="utf-8"
        )
        result = import_agent_bundle(bundle["zip"], new_name="skill-agent")
        assert "sum-skill" in result["skills_skipped_existing"]
        assert (
            utils.get_agent_skills_dir() / "sum-skill" / "SKILL.md"
        ).read_text(encoding="utf-8") == "本地新内容"


class TestDelete:
    def test_delete_plan_missing_agent(self, tmp_path):
        with pytest.raises(HarnessUsageError):
            collect_delete_plan("no-such-agent")

    def test_delete_plan_builtin_refused(self, tmp_path):
        with pytest.raises(HarnessUsageError):
            collect_delete_plan("general-purpose")

    def test_execute_delete_full(self, tmp_path):
        _seed_config()
        _register(tmp_path)
        _make_snapshot("test-agent", 1)
        # 顺手造一个 harness 包 + 引用 benchmark
        from jiuwenswarm.harness_evolve.package_exporter import export_agent_package

        export_agent_package("test-agent", activate=True)
        from jiuwenswarm.harness_evolve import benchmark_store

        benchmark_store.init_benchmark(
            "ref-bench", "t", "d", "test-agent"
        )
        plan = collect_delete_plan("test-agent")
        assert plan["package"]["exists"] is True
        assert plan["config_entry"] is True
        assert plan["benchmarks"] == ["ref-bench"]

        result = execute_delete("test-agent", with_benchmarks=False)
        assert result["deleted"]["md"] is True
        assert result["deleted"]["sidecar"] is True
        assert result["deleted"]["package"] is True
        assert result["deleted"]["config_entry"] is True
        assert result["benchmarks_kept"] == ["ref-bench"]
        # 全部清理干净
        assert not (utils.get_user_workspace_dir() / "agents" / "test-agent.md").exists()
        assert not paths.agent_dir("test-agent").exists()
        from jiuwenswarm.harness_evolve.package_exporter import _packages_file

        data = json.loads(_packages_file().read_text(encoding="utf-8"))
        assert all(p["extension_name"] != "test-agent" for p in data["packages"])
        cfg = yaml.safe_load(
            (utils.get_user_workspace_dir() / "config" / "config.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert "test-agent" not in cfg["react"]["subagents"]
        # benchmark 保留
        assert paths.benchmark_dir("ref-bench").is_dir()

    def test_execute_delete_with_benchmarks(self, tmp_path):
        _register(tmp_path)
        from jiuwenswarm.harness_evolve import benchmark_store

        benchmark_store.init_benchmark("ref-bench", "t", "d", "test-agent")
        result = execute_delete("test-agent", with_benchmarks=True)
        assert result["removed_benchmarks"] == ["ref-bench"]
        assert not paths.benchmark_dir("ref-bench").exists()

    def test_delete_keeps_other_agents(self, tmp_path):
        _register(tmp_path, name="keep-agent")
        _register(tmp_path, name="del-agent")
        execute_delete("del-agent", with_benchmarks=False)
        assert (utils.get_user_workspace_dir() / "agents" / "keep-agent.md").exists()
        assert paths.agent_state_file("keep-agent").is_file()


class TestCli:
    def test_cli_delete_without_yes_exit_2(self, tmp_path, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        _register(tmp_path)
        capsys.readouterr()
        rc = main(
            [
                "agent-delete",
                "--name",
                "test-agent",
                "--data-dir",
                str(tmp_path / "he"),
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "未确认" in err
        # 零变化
        assert (utils.get_user_workspace_dir() / "agents" / "test-agent.md").exists()

    def test_cli_export_import_roundtrip(self, tmp_path, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        _seed_config()
        _register(tmp_path)
        _make_snapshot("test-agent", 1)
        capsys.readouterr()
        rc = main(
            [
                "agent-export",
                "--name",
                "test-agent",
                "--out",
                str(tmp_path / "bundle"),
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        assert rc == 0
        exported = json.loads(capsys.readouterr().out)
        assert exported["snapshots"] == [1]
        execute_delete("test-agent", with_benchmarks=False)
        capsys.readouterr()
        rc = main(
            [
                "agent-import",
                "--file",
                exported["zip"],
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        assert rc == 0
        imported = json.loads(capsys.readouterr().out)
        assert imported["version"] == 1
        assert imported["subagent_enabled"] is True


def _corrupt_zip(zip_path, member: str, new_bytes: bytes) -> None:
    """重写 zip 中某个 member 的内容(其余原样保留)。"""
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        data = {n: zf.read(n) for n in zf.namelist()}
    data[member] = new_bytes
    tmp = zip_path.with_name(zip_path.name + ".new")
    with zipfile.ZipFile(tmp, "w") as zf:
        for n, b in data.items():
            zf.writestr(n, b)
    shutil.move(tmp, zip_path)
