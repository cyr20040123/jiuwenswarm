# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""hot_reload 测试:CLI 触发热重载通知(假 agent server,不真连 18092)。

conftest 的 autouse fixture 默认置 ``JIUWENSWARM_HARNESS_EVOLVE_NO_HOT_RELOAD=1``
(存量 CLI 测试零网络);本文件里需要真连假 server 的用例通过
``hot_reload_enabled`` fixture 解除。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

from jiuwenswarm.harness_evolve import hot_reload
from jiuwenswarm.harness_evolve.hot_reload import (
    build_reload_envelope,
    notify_agent_reload,
    resolve_agent_server_addr,
    _parse_reload_response,
)

_SUCCESS_MSG = "agent 服务已热重载,新定义立即生效"
_ENV_SKIP = "JIUWENSWARM_HARNESS_EVOLVE_NO_HOT_RELOAD"


# ---------------------------------------------------------------------------
# 假 agent server(真 WS,随机端口,后台线程)
# ---------------------------------------------------------------------------


class FakeAgentServer:
    """在随机端口起真 WebSocket 服务:连接即发 connection.ack,记录请求。"""

    def __init__(self, behavior):
        self._behavior = behavior  # async callable(ws, request_dict, server)
        self.port: int | None = None
        self.connect_count = 0
        self.requests: list[dict] = []
        self._loop = None
        self._server = None
        self._thread = None

    def _serve_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        try:
            from websockets.legacy.server import serve as legacy_serve

            server = await legacy_serve(self._handler, "127.0.0.1", 0)
        except ImportError:  # websockets 新 API 回退(与仓库既有模式一致)
            from websockets import serve as new_serve

            server = await new_serve(self._handler, "127.0.0.1", 0)
        self._server = server
        socks = getattr(server, "sockets", None) or getattr(
            getattr(server, "server", None), "sockets", None
        )
        self.port = socks[0].getsockname()[1]
        await server.wait_closed()

    async def _handler(self, ws, path=None) -> None:
        self.connect_count += 1
        try:
            await ws.send(json.dumps({
                "type": "event", "event": "connection.ack",
                "payload": {"status": "ready"},
            }))
            raw = await asyncio.wait_for(ws.recv(), 10.0)
            req = json.loads(raw)
            self.requests.append(req)
            await self._behavior(ws, req, self)
        except Exception:
            pass  # 客户端断开/测试行为关闭连接均属预期

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()
        deadline = time.time() + 5.0
        while self.port is None and time.time() < deadline:
            time.sleep(0.02)
        assert self.port is not None, "假 agent server 未就绪"

    def stop(self) -> None:
        if self._server is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._server.close)
            self._thread.join(timeout=3.0)
        self._thread = None


async def _respond_ok(ws, req, server) -> None:
    await ws.send(json.dumps({
        "status": "succeeded", "body": {"result": {"reloaded": True}},
    }))


async def _respond_fail(ws, req, server) -> None:
    await ws.send(json.dumps({"status": "failed", "body": {"message": "boom"}}))


def _close_first_n(n: int, inner) -> None:
    """前 n 个连接不发响应直接断开(模拟传输层失败)。"""

    async def behavior(ws, req, server) -> None:
        if server.connect_count <= n:
            await ws.close()
        else:
            await inner(ws, req, server)

    return behavior


def _unused_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_server(request):
    def make(behavior):
        server = FakeAgentServer(behavior)
        server.start()
        request.addfinalizer(server.stop)
        return server

    return make


@pytest.fixture
def hot_reload_enabled(monkeypatch):
    """解除 conftest 的跳过 env,并隔离本用例的地址 env。"""
    monkeypatch.delenv(_ENV_SKIP, raising=False)
    monkeypatch.delenv("AGENT_PORT", raising=False)


def _point_to(monkeypatch, server: FakeAgentServer) -> None:
    monkeypatch.setenv("AGENT_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_SERVER_PORT", str(server.port))


# ---------------------------------------------------------------------------
# 单元:纯函数
# ---------------------------------------------------------------------------


class TestPureFunctions:
    def test_resolve_defaults(self, monkeypatch):
        monkeypatch.delenv("AGENT_SERVER_HOST", raising=False)
        monkeypatch.delenv("AGENT_SERVER_PORT", raising=False)
        monkeypatch.delenv("AGENT_PORT", raising=False)
        assert resolve_agent_server_addr() == ("127.0.0.1", 18092)

    def test_resolve_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_SERVER_HOST", "10.0.0.5")
        monkeypatch.setenv("AGENT_SERVER_PORT", "19999")
        assert resolve_agent_server_addr() == ("10.0.0.5", 19999)

    def test_resolve_agent_port_fallback(self, monkeypatch):
        monkeypatch.delenv("AGENT_SERVER_PORT", raising=False)
        monkeypatch.setenv("AGENT_PORT", "18888")
        assert resolve_agent_server_addr() == ("127.0.0.1", 18888)

    def test_resolve_invalid_port_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_SERVER_PORT", "not-a-port")
        assert resolve_agent_server_addr() == ("127.0.0.1", 18092)

    @pytest.mark.parametrize(
        "raw,expected_ok,expected_frag",
        [
            ('{"status":"succeeded","body":{"result":{"reloaded":true}}}', True, ""),
            ('{"status":"failed","body":{"message":"boom"}}', False, "boom"),
            ('{"ok":true,"payload":{"reloaded":true}}', True, ""),
            ('{"ok":false,"payload":{"error":"nope"}}', False, "nope"),
            ("not json at all", False, "invalid response"),
            ('{"status":"succeeded","body":{"result":{"reloaded":false}}}', False, "missing"),
        ],
    )
    def test_parse_response(self, raw, expected_ok, expected_frag):
        ok, msg = _parse_reload_response(raw)
        assert ok is expected_ok
        if expected_frag:
            assert expected_frag in msg

    def test_envelope_fields(self):
        env = build_reload_envelope()
        assert env["method"] == "agent.reload_config"
        assert env["params"] == {}
        assert env["session_id"] == "sess_reload"
        assert env["user_id"] == "agentos_test"
        assert env["request_id"].startswith("agent-reload-")
        assert env["is_stream"] is False
        assert env["timestamp"]  # 非空(已规范化为时间字符串)

    def test_request_ids_differ(self):
        assert build_reload_envelope()["request_id"] != build_reload_envelope()["request_id"]


# ---------------------------------------------------------------------------
# 集成:真 WS 假 server
# ---------------------------------------------------------------------------


class TestNotify:
    def test_success_reload(self, fake_server, hot_reload_enabled, monkeypatch):
        server = fake_server(_respond_ok)
        _point_to(monkeypatch, server)
        ok, msg = notify_agent_reload(retries=0, interval=0, timeout=5.0)
        assert ok is True
        assert msg == _SUCCESS_MSG
        assert server.connect_count == 1
        req = server.requests[0]
        assert req["method"] == "agent.reload_config"
        assert req["params"] == {}
        assert req["session_id"] == "sess_reload"
        assert req["user_id"] == "agentos_test"
        assert req["request_id"].startswith("agent-reload-")

    def test_retry_succeeds_after_failures(
        self, fake_server, hot_reload_enabled, monkeypatch
    ):
        server = fake_server(_close_first_n(2, _respond_ok))
        _point_to(monkeypatch, server)
        ok, msg = notify_agent_reload(retries=3, interval=0, timeout=5.0)
        assert ok is True
        assert msg == _SUCCESS_MSG
        assert server.connect_count == 3  # 前 2 次断开,第 3 次成功

    def test_all_fail_degrades(self, hot_reload_enabled, monkeypatch):
        monkeypatch.setenv("AGENT_SERVER_HOST", "127.0.0.1")
        monkeypatch.setenv("AGENT_SERVER_PORT", str(_unused_port()))
        ok, msg = notify_agent_reload(retries=2, interval=0, timeout=1.0)
        assert ok is False
        assert "重启 agent 服务后生效" in msg
        assert "重试 2 次" in msg

    def test_server_reject_no_retry(
        self, fake_server, hot_reload_enabled, monkeypatch
    ):
        server = fake_server(_respond_fail)
        _point_to(monkeypatch, server)
        ok, msg = notify_agent_reload(retries=3, interval=0, timeout=5.0)
        assert ok is False
        assert "拒绝热重载" in msg
        assert "boom" in msg
        assert server.connect_count == 1  # 明确拒绝不再重试

    def test_env_skip_no_network(self, fake_server, monkeypatch):
        server = fake_server(_respond_ok)
        _point_to(monkeypatch, server)
        monkeypatch.setenv(_ENV_SKIP, "1")
        ok, msg = notify_agent_reload(retries=0, interval=0, timeout=5.0)
        assert ok is False
        assert msg == ""
        assert server.connect_count == 0

    def test_response_timeout(self, fake_server, hot_reload_enabled, monkeypatch):
        async def never_respond(ws, req, server):
            await asyncio.sleep(5.0)  # ack 后不回包

        server = fake_server(never_respond)
        _point_to(monkeypatch, server)
        started = time.monotonic()
        ok, msg = notify_agent_reload(retries=1, interval=0, timeout=0.5)
        elapsed = time.monotonic() - started
        assert ok is False
        assert "重启 agent 服务后生效" in msg
        assert elapsed < 5.0

    def test_garbage_response_rejected(self, fake_server, hot_reload_enabled, monkeypatch):
        async def garbage(ws, req, server):
            await ws.send("not json at all")

        server = fake_server(garbage)
        _point_to(monkeypatch, server)
        ok, msg = notify_agent_reload(retries=3, interval=0, timeout=5.0)
        assert ok is False
        assert "拒绝热重载" in msg
        assert server.connect_count == 1


# ---------------------------------------------------------------------------
# CLI 集成(monkeypatch notify,不真连网络)
# ---------------------------------------------------------------------------


def _register(tmp_path, name="test-agent", extra=()):
    from jiuwenswarm.harness_evolve.cli import main

    prompt = tmp_path / "prompt.md"
    prompt.write_text("你是测试 agent。\n", encoding="utf-8")
    argv = [
        "agent-register", "--name", name, "--description", "t",
        "--prompt-file", str(prompt),
        "--data-dir", str(tmp_path / "he"), "--json",
    ] + list(extra)
    return main(argv)


def _payload_from_stdout(out: str) -> dict:
    """取 stdout 中第一个 '{' 起的 JSON(进程首次导入的启动日志可能混在 stdout)。"""
    idx = out.index("{")
    return json.loads(out[idx:])


class TestCliIntegration:
    def test_register_hot_reload_true(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            hot_reload, "notify_agent_reload", lambda **kw: (True, _SUCCESS_MSG)
        )
        capsys.readouterr()
        rc = _register(tmp_path)
        assert rc == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is True
        assert payload["hot_reload_msg"] == _SUCCESS_MSG

    def test_register_degrade_message(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            hot_reload, "notify_agent_reload",
            lambda **kw: (False, "警告:无法连接 agent 服务(重试 5 次后放弃);变更已保存,重启 agent 服务后生效"),
        )
        capsys.readouterr()
        rc = _register(tmp_path)
        assert rc == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is False
        assert "重启 agent 服务后生效" in payload["hot_reload_msg"]

    def test_register_env_skip_silent(self, tmp_path, capsys):
        """conftest 默认跳过:静默,payload 字段存在但为 None。"""
        capsys.readouterr()
        rc = _register(tmp_path)
        assert rc == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is False
        assert payload["hot_reload_msg"] is None

    def test_all_five_commands_payload(self, tmp_path, monkeypatch, capsys):
        from jiuwenswarm.harness_evolve.cli import main

        monkeypatch.setattr(
            hot_reload, "notify_agent_reload", lambda **kw: (True, _SUCCESS_MSG)
        )
        data_dir = str(tmp_path / "he")

        capsys.readouterr()
        assert _register(tmp_path) == 0
        capsys.readouterr()
        # edit(v2)
        assert main(["snapshot-create", "--name", "test-agent", "--data-dir", data_dir, "--json"]) == 0
        capsys.readouterr()
        v2 = tmp_path / "v2.md"
        v2.write_text("你是测试 agent v2。\n", encoding="utf-8")
        assert main(["agent-edit", "--name", "test-agent", "--expected-version", "1",
                     "--prompt-file", str(v2), "--data-dir", data_dir, "--json"]) == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is True
        # restore
        assert main(["agent-restore", "--name", "test-agent", "--version", "1",
                     "--data-dir", data_dir, "--json"]) == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is True
        # export → delete → import
        assert main(["agent-export", "--name", "test-agent", "--out", str(tmp_path / "b"),
                     "--data-dir", data_dir, "--json"]) == 0
        bundle = json.loads(capsys.readouterr().out)["zip"]
        assert main(["agent-delete", "--name", "test-agent", "--yes",
                     "--data-dir", data_dir, "--json"]) == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is True
        assert main(["agent-import", "--file", bundle,
                     "--data-dir", data_dir, "--json"]) == 0
        payload = _payload_from_stdout(capsys.readouterr().out)
        assert payload["hot_reload"] is True
