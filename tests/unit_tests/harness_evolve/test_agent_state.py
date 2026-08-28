# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent_state.py 测试(版本/快照/编辑;apply_agent_edit 走真实 AgentConfigService)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.harness_evolve import agent_state, paths
from jiuwenswarm.harness_evolve.errors import (
    AgentStateError,
    HarnessUsageError,
    SnapshotError,
)

PROMPT_V1 = "你是摘要 agent v1。"
PROMPT_V2 = "你是摘要 agent v2,新增简洁要求。"


@pytest.fixture
def agent_name():
    return "sum-agent"


@pytest.fixture
def agent_def(agent_name):
    """在隔离用户目录创建真实 agent 定义,返回 (name, md_path)。"""
    from jiuwenswarm.server.runtime.agent_config_service import (
        AgentConfigService,
        CreateAgentParams,
    )

    service = AgentConfigService()
    created = service.create_agent(
        CreateAgentParams(
            name=agent_name,
            description="summarizer",
            prompt=PROMPT_V1,
            location="user",
        )
    )
    return agent_name, Path(created.file_path)


class TestEnsureState:
    def test_creates_v1(self, agent_def):
        name, md_path = agent_def
        state = agent_state.ensure_agent_state(name, md_path)
        assert state["version"] == 1
        assert state["next_version"] == 2
        assert state["md_sha256"] == agent_state.md_sha256(md_path)
        assert state["baseline_version"] is None

    def test_idempotent(self, agent_def):
        name, md_path = agent_def
        first = agent_state.ensure_agent_state(name, md_path)
        second = agent_state.ensure_agent_state(name, md_path)
        assert first == second

    def test_missing_md(self, agent_name):
        with pytest.raises(AgentStateError, match="md"):
            agent_state.ensure_agent_state(agent_name, "/nonexistent/x.md")

    def test_get_missing(self, agent_name):
        with pytest.raises(AgentStateError, match="状态"):
            agent_state.get_agent_state(agent_name)


class TestSnapshot:
    def test_create_and_restore_bytes(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        snapshot = agent_state.create_snapshot(name, 1)
        assert snapshot.is_file()
        assert snapshot.name == "v1.tar.gz"

        # 外部改动 md 后恢复
        md_path.write_text("被外部改坏了\n", encoding="utf-8")
        state = agent_state.restore_snapshot(name, 1)
        restored = md_path.read_text(encoding="utf-8")
        assert PROMPT_V1 in restored
        assert restored.startswith("---")
        assert state["version"] == 1
        assert state["md_sha256"] == agent_state.md_sha256(md_path)

    def test_refuse_overwrite(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        agent_state.create_snapshot(name, 1)
        with pytest.raises(SnapshotError, match="禁止覆盖"):
            agent_state.create_snapshot(name, 1)

    def test_version_must_match_state(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        with pytest.raises(SnapshotError, match="必须等于当前"):
            agent_state.create_snapshot(name, 99)

    def test_restore_missing(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        with pytest.raises(SnapshotError, match="不存在"):
            agent_state.restore_snapshot(name, 3)


class TestEdit:
    def test_requires_snapshot_first(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        with pytest.raises(AgentStateError, match="快照"):
            agent_state.apply_agent_edit(name, expected_version=1, prompt=PROMPT_V2)

    def test_version_mismatch(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        with pytest.raises(AgentStateError, match="期望基于版本"):
            agent_state.apply_agent_edit(
                name, expected_version=5, prompt=PROMPT_V2
            )

    def test_no_fields(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        agent_state.create_snapshot(name, 1)
        with pytest.raises(HarnessUsageError, match="修改字段"):
            agent_state.apply_agent_edit(name, expected_version=1)

    def test_drift_detected(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        agent_state.create_snapshot(name, 1)
        # 模拟手改:保留 frontmatter,只改 body
        drifted = md_path.read_text(encoding="utf-8").replace(
            PROMPT_V1, "手改的 prompt\n"
        )
        md_path.write_text(drifted, encoding="utf-8")
        with pytest.raises(AgentStateError, match="漂移"):
            agent_state.apply_agent_edit(
                name, expected_version=1, prompt=PROMPT_V2
            )
        # --force 重新对齐后继续
        state = agent_state.apply_agent_edit(
            name, expected_version=1, prompt=PROMPT_V2, force=True
        )
        assert state["version"] == 2

    def test_happy_path_bumps_version(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        agent_state.create_snapshot(name, 1)
        state = agent_state.apply_agent_edit(
            name, expected_version=1, prompt=PROMPT_V2, max_iterations=20
        )
        assert state["version"] == 2
        assert state["next_version"] == 3
        text = md_path.read_text(encoding="utf-8")
        assert text.startswith("---") and PROMPT_V2 in text
        from jiuwenswarm.server.runtime.agent_config_service import (
            AgentConfigService,
        )

        updated = AgentConfigService().get_agent(name)
        assert updated.prompt == PROMPT_V2
        assert updated.max_iterations == 20

    def test_no_content_change_rejected(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        agent_state.create_snapshot(name, 1)
        with pytest.raises(AgentStateError, match="未变化"):
            agent_state.apply_agent_edit(
                name, expected_version=1, prompt=PROMPT_V1
            )

    def test_rejected_version_not_reused(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        agent_state.create_snapshot(name, 1)
        agent_state.apply_agent_edit(name, expected_version=1, prompt=PROMPT_V2)
        assert agent_state.agent_version(name) == 2
        # v2 被拒 → 回滚 v1
        agent_state.create_snapshot(name, 2)
        agent_state.restore_snapshot(name, 1)
        assert agent_state.agent_version(name) == 1
        # 下一个候选不复用 2,直接 3
        state = agent_state.apply_agent_edit(
            name, expected_version=1, prompt=PROMPT_V2
        )
        assert state["version"] == 3
        assert state["next_version"] == 4

    def test_baseline_record(self, agent_def):
        name, md_path = agent_def
        agent_state.ensure_agent_state(name, md_path)
        state = agent_state.record_baseline(name, 1)
        assert state["baseline_version"] == 1
        assert agent_state.baseline_version(name) == 1
