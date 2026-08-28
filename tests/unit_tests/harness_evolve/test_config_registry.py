# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""config_registry 测试:agent-register 自动注册进 react.subagents 启用。

conftest 的 autouse fixture 已把 user home 重定向到 tmp_path,因此
``utils.get_user_workspace_dir()`` 返回 ``tmp_path/.jiuwenswarm``,
用户级 config 位于 ``tmp_path/.jiuwenswarm/config/config.yaml``。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.config import load_yaml_round_trip
from jiuwenswarm.harness_evolve.config_registry import (
    enable_subagent_in_config,
    remove_subagent_from_config,
    user_config_path,
)

BASE_CONFIG = """# 用户配置
react:
  agent_name: main_agent
  subagents:
    statusline-setup:
      enabled: true
    browser_agent:
      enabled: true  # 浏览器子代理
"""


@pytest.fixture
def user_config(tmp_path):
    """预置用户级 config.yaml(含注释与已有 subagents 段)。"""
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    return cfg


class TestEnableSubagent:
    def test_adds_entry_and_preserves_existing(self, user_config):
        ok, msg = enable_subagent_in_config("citation_checker")
        assert ok is True
        assert "citation_checker" in msg
        data = load_yaml_round_trip(user_config)
        entry = data["react"]["subagents"]["citation_checker"]
        assert entry["enabled"] is True
        # 已有条目与注释原样保留(round-trip)
        assert data["react"]["subagents"]["statusline-setup"]["enabled"] is True
        assert data["react"]["subagents"]["browser_agent"]["enabled"] is True
        assert "浏览器子代理" in user_config.read_text(encoding="utf-8")
        assert "用户配置" in user_config.read_text(encoding="utf-8")

    def test_idempotent_flips_false_to_true(self, tmp_path, user_config):
        data = load_yaml_round_trip(user_config)
        data["react"]["subagents"]["citation_checker"] = {
            "enabled": False,
            "max_iterations": 5,
        }
        from jiuwenswarm.common.config import dump_yaml_round_trip

        dump_yaml_round_trip(user_config, data)

        ok, _ = enable_subagent_in_config("citation_checker")
        assert ok is True
        data = load_yaml_round_trip(user_config)
        entry = data["react"]["subagents"]["citation_checker"]
        assert entry["enabled"] is True
        assert entry["max_iterations"] == 5  # 其它字段保留

    def test_creates_react_section_when_missing(self, tmp_path):
        cfg = user_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("channels: {}\n", encoding="utf-8")

        ok, _ = enable_subagent_in_config("sum-agent")
        assert ok is True
        data = load_yaml_round_trip(cfg)
        assert data["react"]["subagents"]["sum-agent"]["enabled"] is True
        assert data["channels"] == {}  # 其它段不动

    def test_creates_subagents_section_when_missing(self, tmp_path):
        cfg = user_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("react:\n  agent_name: main_agent\n", encoding="utf-8")

        ok, _ = enable_subagent_in_config("sum-agent")
        assert ok is True
        data = load_yaml_round_trip(cfg)
        assert data["react"]["agent_name"] == "main_agent"
        assert data["react"]["subagents"]["sum-agent"]["enabled"] is True

    def test_missing_config_file_skips_without_creating(self, tmp_path):
        ok, msg = enable_subagent_in_config("sum-agent")
        assert ok is False
        assert "config.yaml 不存在" in msg
        # 绝不新建用户级 config 文件
        assert not user_config_path().exists()

    def test_corrupt_react_section_reports_failure(self, tmp_path):
        cfg = user_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("react: not-a-mapping\n", encoding="utf-8")

        ok, msg = enable_subagent_in_config("sum-agent")
        assert ok is False
        assert "不是映射" in msg
        # 损坏配置不做任何写盘
        assert "subagents" not in cfg.read_text(encoding="utf-8")

    def test_corrupt_entry_reports_failure(self, tmp_path, user_config):
        data = load_yaml_round_trip(user_config)
        data["react"]["subagents"]["sum-agent"] = "not-a-mapping"
        from jiuwenswarm.common.config import dump_yaml_round_trip

        dump_yaml_round_trip(user_config, data)

        ok, msg = enable_subagent_in_config("sum-agent")
        assert ok is False
        assert "不是映射" in msg


class TestRemoveSubagent:
    def test_removes_entry_preserves_others(self, user_config):
        enable_subagent_in_config("citation_checker")
        ok, msg = remove_subagent_from_config("citation_checker")
        assert ok is True
        assert "citation_checker" in msg
        data = load_yaml_round_trip(user_config)
        assert "citation_checker" not in data["react"]["subagents"]
        # 其它条目与注释原样保留
        assert data["react"]["subagents"]["statusline-setup"]["enabled"] is True
        assert data["react"]["subagents"]["browser_agent"]["enabled"] is True
        assert "浏览器子代理" in user_config.read_text(encoding="utf-8")

    def test_remove_missing_no_write(self, tmp_path, user_config):
        before = user_config.read_text(encoding="utf-8")
        ok, msg = remove_subagent_from_config("ghost-agent")
        assert ok is False
        assert "无需清理" in msg
        # 不写盘:内容逐字节不变
        assert user_config.read_text(encoding="utf-8") == before


class TestAgentRegisterIntegration:
    """agent-register 成功后自动启用子代理(端到端)。"""

    def test_register_writes_config_entry(self, tmp_path, user_config):
        from jiuwenswarm.harness_evolve.cli import main

        prompt = tmp_path / "prompt.md"
        prompt.write_text("你是引用检查 agent。\n", encoding="utf-8")
        rc = main(
            [
                "agent-register",
                "--name",
                "citation_checker",
                "--description",
                "citation checker",
                "--prompt-file",
                str(prompt),
                "--data-dir",
                str(tmp_path / "he"),
                "--json",
            ]
        )
        assert rc == 0
        data = load_yaml_round_trip(user_config)
        entry = data["react"]["subagents"]["citation_checker"]
        assert entry["enabled"] is True
        # 已有条目不受影响
        assert data["react"]["subagents"]["statusline-setup"]["enabled"] is True

    def test_register_without_config_still_succeeds(self, tmp_path):
        from jiuwenswarm.harness_evolve.cli import main

        prompt = tmp_path / "prompt.md"
        prompt.write_text("你是摘要 agent。\n", encoding="utf-8")
        rc = main(
            [
                "agent-register",
                "--name",
                "sum-agent",
                "--description",
                "summarizer",
                "--prompt-file",
                str(prompt),
                "--data-dir",
                str(tmp_path / "he"),
            ]
        )
        # 无 config.yaml 时注册主流程仍成功,只是跳过子代理启用
        assert rc == 0
        assert not user_config_path().exists()
