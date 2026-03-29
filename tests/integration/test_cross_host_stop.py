"""
§2.4 跨主体工作流停止编排 集成测试

验证：当 T_AOE 调用 stop_app() 时，正在远端 E_AOE 执行的子任务 Task 能被取消，
而不是等待 timeout。

测试场景：
  T1 - DELETE /orchestration/session/{id} 取消正在运行的 Task（< 1s 完成，不等 timeout）
  T2 - stop_app() 通过 fire-and-forget 通知远端会话取消
  T3 - Task 已完成时 DELETE 幂等返回 closed（不报错）
  T4 - _active_remote_sessions 追踪：dispatch 中注册，结束后移除
"""

import asyncio
import socket
import threading
import time
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ============================================================
# 工具函数
# ============================================================

def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{url}/health", timeout=0.5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def e_aoe_server():
    """
    在后台线程启动真实 E_AOE uvicorn 服务。
    workflow mock 在各测试中 patch，这里只负责启动服务。
    """
    from agent_server import app

    port = _get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    assert _wait_for_server(url, timeout=8.0), f"E_AOE 启动超时: {url}"
    yield url

    server.should_exit = True
    thread.join(timeout=3.0)


# ============================================================
# T1 - DELETE session 取消正在执行的 Task（不等 timeout）
# ============================================================

@pytest.mark.asyncio
async def test_delete_session_cancels_running_task(e_aoe_server):
    """
    T1: E_AOE 收到 subtask 后 Task 开始运行（sleep 30s 模拟长任务），
    立即发送 DELETE /session/{id}，Task 应在 < 2s 内被取消并返回 cancelled。
    不等待 30s timeout。
    """
    server_url = e_aoe_server
    session_id = "test-session-cancel-001"

    # Mock: workflow 执行 30s 长任务（模拟）
    dispatch_done = asyncio.Event()

    async def slow_workflow(**kwargs):
        try:
            await asyncio.sleep(30)
            return {"result": "should not reach"}
        except asyncio.CancelledError:
            raise

    with patch(
        "src.distributed_workflow.run_distributed_workflow",
        side_effect=slow_workflow,
    ):
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 发送 dispatch（不等待，后台运行）
            dispatch_task = asyncio.create_task(
                client.post(
                    f"{server_url}/orchestration/dispatch",
                    json={
                        "subtask": {
                            "task_id": "t1",
                            "task_description": "long task",
                            "timeout_seconds": 30,
                        },
                        "session_id": session_id,
                        "source_aoe_url": "http://localhost:9999",
                    },
                )
            )

            # 等待 0.3s 让 dispatch handler 启动 Task
            await asyncio.sleep(0.3)

            # 发送 DELETE session → 触发 Task.cancel()
            t_start = time.monotonic()
            del_resp = await client.delete(
                f"{server_url}/orchestration/session/{session_id}"
            )
            elapsed_delete = time.monotonic() - t_start

            assert del_resp.status_code == 200
            del_data = del_resp.json()
            assert del_data["status"] in ("closed", "not_found")

            # dispatch 应该因 CancelledError 很快返回（< 2s）
            t_start = time.monotonic()
            try:
                dispatch_resp = await asyncio.wait_for(dispatch_task, timeout=5.0)
                elapsed_dispatch = time.monotonic() - t_start
                data = dispatch_resp.json()
                # status 应为 cancelled（Task 被取消）
                assert data["status"] in ("cancelled", "error"), (
                    f"期望 cancelled/error，实际: {data}"
                )
                assert elapsed_dispatch < 3.0, (
                    f"Task 取消后 dispatch 返回时间过长: {elapsed_dispatch:.2f}s"
                )
            except asyncio.TimeoutError:
                pytest.fail("dispatch 未在 5s 内返回，Task 取消未生效")


# ============================================================
# T2 - stop_app fire-and-forget 通知远端
# ============================================================

@pytest.mark.asyncio
async def test_stop_app_notifies_remote_session(e_aoe_server):
    """
    T2: 模拟 stop_app 调用路径，验证 _cancel_remote_session 被触发。
    用 Mock patch _cancel_remote_session，无需真实工作流。
    """
    from src.graph.distributed_nodes import _active_remote_sessions

    server_url = e_aoe_server
    session_id = "test-session-stop-002"

    # 手动写入一个活跃会话
    _active_remote_sessions[session_id] = server_url

    cancelled_calls = []

    async def mock_cancel(url, sid):
        cancelled_calls.append((url, sid))

    with patch(
        "src.app.app_logic_engine._cancel_remote_session",
        side_effect=mock_cancel,
    ):
        from src.app.app_logic_engine import AppLogicEngine
        engine = AppLogicEngine()

        # 注入一个虚假 running task
        fake_task = asyncio.create_task(asyncio.sleep(100))
        engine._running_tasks["app-001"] = fake_task
        engine._workflow_handles["app-001"] = "wf-001"

        result = await engine.stop_app("app-001")
        assert result is True

        # 等 fire-and-forget 任务执行
        await asyncio.sleep(0.1)

    assert any(sid == session_id for _, sid in cancelled_calls), (
        f"_cancel_remote_session 未被调用，calls={cancelled_calls}"
    )

    # 清理
    _active_remote_sessions.pop(session_id, None)


# ============================================================
# T3 - DELETE 幂等：session 不存在时返回 not_found（不报错）
# ============================================================

@pytest.mark.asyncio
async def test_delete_nonexistent_session_is_idempotent(e_aoe_server):
    """
    T3: 对不存在的 session_id 发送 DELETE，应返回 200 + {"status": "not_found"}。
    """
    server_url = e_aoe_server
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.delete(
            f"{server_url}/orchestration/session/nonexistent-session-xyz"
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


# ============================================================
# T4 - _active_remote_sessions 追踪完整生命周期
# ============================================================

@pytest.mark.asyncio
async def test_active_remote_sessions_lifecycle():
    """
    T4: dispatch_subtask_to_remote_aoe 执行期间：
      - 注册 session_id 到 _active_remote_sessions
      - 执行结束（无论成功/失败）后从 _active_remote_sessions 移除
    """
    from src.graph.distributed_nodes import (
        dispatch_subtask_to_remote_aoe,
        _active_remote_sessions,
    )

    session_id = "lifecycle-test-session-003"
    subtask = {
        "task_id": "lc-t1",
        "task_description": "lifecycle test",
        "session_id": session_id,
        "timeout_seconds": 5,
    }

    registered_during = []

    # patch httpx 使 dispatch 快速返回
    async def mock_post(*args, **kwargs):
        # 在 httpx.post 执行期间检查 _active_remote_sessions
        registered_during.append(session_id in _active_remote_sessions)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "status": "completed",
            "workflow_handle": "wf-lc-001",
            "result": "ok",
        }
        return mock_resp

    class MockAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return await mock_post(*args, **kwargs)

    with patch("httpx.AsyncClient", return_value=MockAsyncClient()):
        result = await dispatch_subtask_to_remote_aoe(
            subtask=subtask,
            remote_aoe_url="http://fake-remote:9000",
            session_timeout=5,
        )

    # dispatch 期间 session 已注册
    assert any(registered_during), "_active_remote_sessions 未在 dispatch 期间注册 session"

    # dispatch 结束后 session 已清除
    assert session_id not in _active_remote_sessions, (
        f"dispatch 结束后 session 未从 _active_remote_sessions 移除"
    )

    assert result["status"] == "completed"
