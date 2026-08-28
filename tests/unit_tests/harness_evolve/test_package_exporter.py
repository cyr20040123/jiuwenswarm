# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""package_exporter 测试:agent 打包为 Harness 包。

conftest 的 autouse fixture 已把 user home 重定向到 tmp_path,因此
``utils.get_user_workspace_dir()`` 返回 ``tmp_path/.jiuwenswarm``,
包目录位于 ``tmp_path/.jiuwenswarm/auto-harness/runtime_extensions/...``。
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from jiuwenswarm.common import utils
from jiuwenswarm.harness_evolve.errors import HarnessUsageError
from jiuwenswarm.harness_evolve.package_exporter import (
    _packages_file,
    export_agent_package,
    package_dir,
)

PROMPT = """你是测试 agent。

# 职责
只做一件事:回答输入。
"""

PROMPT_V2 = """你是测试 agent v2。

# 职责
只做一件事:回答输入并给出证据。
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


def _make_skill(skill: str) -> None:
    d = utils.get_agent_skills_dir() / skill
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")


def _load_manifest(name="test-agent") -> dict:
    return yaml.safe_load(
        (package_dir(name) / "harness_config.yaml").read_text(encoding="utf-8")
    )


class TestExport:
    def test_export_creates_package_and_metadata(self, tmp_path):
        _register(tmp_path, tools=["Read", "WebSearch", "WebFetch"])
        result = export_agent_package("test-agent")

        target = package_dir("test-agent")
        assert target.is_dir()
        assert (target / "harness_config.yaml").is_file()
        assert result["package_dir"] == str(target)
        assert result["package_id"].startswith("pkg_")
        assert result["activated"] is False
        # 元数据文件同步生成,构建期入口(_get_active_package_config_paths)读同一文件
        assert _packages_file().is_file()
        data = json.loads(_packages_file().read_text(encoding="utf-8"))
        assert data["native_version"]["is_active"] is True
        assert data["active_package_ids"] == []

    def test_manifest_parses_and_matches_prompt(self, tmp_path):
        from openjiuwen.harness.resources.extension_loader import load_plugin_package

        _register(tmp_path, tools=["Read", "WebSearch", "WebFetch"])
        export_agent_package("test-agent")

        spec = load_plugin_package(package_dir("test-agent") / "harness_config.yaml")
        assert spec.id == "test-agent-harness"
        assert len(spec.prompt_sections) == 1
        # AgentConfigService 存储时剥离了 prompt 尾部换行
        assert spec.prompt_sections[0].content["cn"] == PROMPT.rstrip("\n")
        assert spec.prompt_sections[0].content["en"] == PROMPT.rstrip("\n")
        # 工具一律不打包(主 agent 自带,声明会导致热加载绑定冲突)
        assert spec.tools == []

    def test_skills_copied_into_package(self, tmp_path):
        from openjiuwen.harness.resources.extension_loader import load_plugin_package

        _make_skill("sum-skill")
        _make_skill("fmt-skill")
        _register(tmp_path, skills=["sum-skill", "fmt-skill"])
        export_agent_package("test-agent")

        target = package_dir("test-agent")
        for skill in ("sum-skill", "fmt-skill"):
            assert (target / "skills" / skill / "SKILL.md").is_file()
        spec = load_plugin_package(target / "harness_config.yaml")
        assert sorted(s.dir for s in spec.skills) == sorted(
            str((target / "skills" / skill).resolve())
            for skill in ("sum-skill", "fmt-skill")
        )

    def test_missing_skill_skipped(self, tmp_path):
        from openjiuwen.harness.resources.extension_loader import load_plugin_package

        _register(tmp_path, skills=["ghost-skill"])
        result = export_agent_package("test-agent")
        assert result["skills"] == []
        spec = load_plugin_package(package_dir("test-agent") / "harness_config.yaml")
        assert spec.skills == []

    def test_tools_skipped_with_trace(self, tmp_path, caplog):
        _register(tmp_path, tools=["Read", "WebSearch", "WebFetch", "MysteryTool"])
        with caplog.at_level("WARNING"):
            result = export_agent_package("test-agent")
        # 文件工具与 web 工具都不打包(skipped_tools 溯源),未知工具告警
        assert result["tools"] == []
        assert sorted(result["skipped_tools"]) == ["Read", "WebFetch", "WebSearch"]
        assert "MysteryTool" in caplog.text
        meta = _load_manifest()["metadata"]
        assert meta["declared_tools"] == ["Read", "WebSearch", "WebFetch", "MysteryTool"]

    def test_metadata_records_state_and_model(self, tmp_path):
        _register(tmp_path, tools=["WebSearch"], model="deepseek-v4")
        export_agent_package("test-agent")

        meta = _load_manifest()["metadata"]
        assert meta["source"] == "harness_evolve"
        assert meta["agent_name"] == "test-agent"
        assert meta["agent_version"] == 1  # sidecar:agent-register 后 version=1
        assert re.fullmatch(r"[0-9a-f]{64}", meta["agent_sha256"])
        assert meta["model"] == "deepseek-v4"

    def test_reexport_keeps_id_and_updates_content(self, tmp_path):
        _register(tmp_path, tools=["WebSearch"])
        first = export_agent_package("test-agent")
        assert first["package_id"].startswith("pkg_")

        # agent-edit 迭代到 v2 后重导出:内容更新、id 不变、目录不新增
        from jiuwenswarm.harness_evolve.cli import main

        rc = main(
            [
                "snapshot-create",
                "--name",
                "test-agent",
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        assert rc == 0
        prompt_v2 = tmp_path / "prompt-v2.md"
        prompt_v2.write_text(PROMPT_V2, encoding="utf-8")
        rc = main(
            [
                "agent-edit",
                "--name",
                "test-agent",
                "--expected-version",
                "1",
                "--prompt-file",
                str(prompt_v2),
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        assert rc == 0

        second = export_agent_package("test-agent")
        assert second["package_id"] == first["package_id"]
        # 包内容已更新为 v2 prompt,metadata 版本跟进
        meta = _load_manifest()["metadata"]
        assert meta["agent_version"] == 2
        spec_prompt = _load_manifest()["prompt_sections"][0]["content"]["cn"]
        assert spec_prompt == PROMPT_V2.rstrip("\n")
        # runtime_extensions 下仍只有一个包目录
        runtime_root = package_dir("test-agent").parent.parent
        assert [p.name for p in runtime_root.glob("*/*")] == ["test-agent"]

    def test_activate_marks_active(self, tmp_path):
        _register(tmp_path, tools=["WebSearch"])
        result = export_agent_package("test-agent", activate=True)
        assert result["activated"] is True
        data = json.loads(_packages_file().read_text(encoding="utf-8"))
        assert data["active_package_ids"] == [result["package_id"]]
        assert data["native_version"]["is_active"] is False

    def test_activate_idempotent(self, tmp_path):
        _register(tmp_path, tools=["WebSearch"])
        first = export_agent_package("test-agent", activate=True)
        second = export_agent_package("test-agent", activate=True)
        assert second["package_id"] == first["package_id"]
        data = json.loads(_packages_file().read_text(encoding="utf-8"))
        assert data["active_package_ids"] == [first["package_id"]]

    def test_unknown_agent_raises(self, tmp_path):
        with pytest.raises(HarnessUsageError):
            export_agent_package("no-such-agent")

    def test_self_check_failure_keeps_old_package(self, tmp_path, monkeypatch):
        _register(tmp_path, tools=["WebSearch"])
        export_agent_package("test-agent")
        before = (package_dir("test-agent") / "harness_config.yaml").read_text(
            encoding="utf-8"
        )
        from jiuwenswarm.harness_evolve import package_exporter

        def _broken(package):
            raise ValueError("simulated manifest parse failure")

        monkeypatch.setattr(package_exporter, "_self_check", _broken)
        with pytest.raises(HarnessUsageError):
            export_agent_package("test-agent")
        # 旧包内容原样保留,无 staging 残留
        after = (package_dir("test-agent") / "harness_config.yaml").read_text(
            encoding="utf-8"
        )
        assert after == before
        staging = package_dir("test-agent").parent / ".test-agent.staging"
        assert not staging.exists()


class TestCli:
    def test_cli_export_package(self, tmp_path, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        _register(tmp_path, tools=["WebSearch"])
        capsys.readouterr()  # 清掉 agent-register 的 JSON 输出
        rc = main(
            [
                "agent-export-package",
                "--name",
                "test-agent",
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["package_id"] == export_agent_package(
            "test-agent"
        )["package_id"]
        assert result["name"] == "test-agent"
        assert package_dir("test-agent").is_dir()

    def test_cli_unknown_agent_exit_2(self, tmp_path, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        rc = main(
            [
                "agent-export-package",
                "--name",
                "no-such-agent",
                "--data-dir",
                str(tmp_path / "he"),
            ]
        )
        assert rc == 2
        assert "不存在" in capsys.readouterr().err
