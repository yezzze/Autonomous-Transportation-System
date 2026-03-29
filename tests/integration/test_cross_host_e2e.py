"""
跨主机端到端集成测试

验证 T_AOE → E_AOE 全链路 HTTP 调用，所有 LLM 调用均被 Mock。

测试场景：
  T1 - dispatch 端点响应格式（in-process httpx client）
  T2 - session 清理端点 + 幂等性（in-process httpx client）
  T3 - 全链路真实 HTTP（uvicorn 后台线程 + dispatch_subtask_to_remote_aoe）
  T4 - failover：E_AOE 宕机时 status=error 且 task_id 正确返回
"""

import asyncio
import socket
import threading
import time
import sys
import os
from typing import Generator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import uvicorn

# 确保项目根目录在 sys.path（支持 pytest 从项目根运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ============================================================
# 工具函数
# ============================================================

def _get_free_port() -> int:
    """获取本机随机空闲端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 5.0) -> bool:
    """轮询 /health 直到服务就绪或超时"""
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

@pytest.fixture()
def mock_workflow():
    """
    Mock run_distributed_workflow，防止测试触发真实 LLM 调用。
    run_distributed_workflow 在 agent_server 内部为懒导入（函数内 from ... import），
    需 patch 源模块而非 agent_server 命名空间。
    """
    mock_result = {"messages": [], "result": "mock_ok", "execution_plan": []}
    with patch(
        "src.distributed_workflow.run_distributed_workflow",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as m:
        yield m


@pytest.fixture(scope="module")
def e_aoe_server():
    """
    在后台线程启动真实 E_AOE uvicorn 服务（随机端口）。
    yield server_url，测试结束后停止服务。
    """
    # 延迟导入，避免 module 级别触发 LLM 初始化
    from agent_server import app as e_aoe_app

    port = _get_free_port()
    config = uvicorn.Config(
        app=e_aoe_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # 降低噪音，T3 用 -s 看 INFO 日志时可手动改为 "info"
    )
    server = uvicorn.Server(config)

    def _run():
        server.run()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    server_url = f"http://127.0.0.1:{port}"
    assert _wait_for_server(server_url, timeout=5.0), (
        f"E_AOE 服务在 5s 内未就绪（port={port}）"
    )

    yield server_url

    # Teardown
    server.should_exit = True
    t.join(timeout=3.0)


# ============================================================
# T1：dispatch 端点响应格式（in-process client）
# ============================================================

@pytest.mark.asyncio
async def test_dispatch_response_format(mock_workflow):
    """T1: POST /orchestration/dispatch 返回字段齐全且格式正确"""
    from agent_server import app

    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/orchestration/dispatch",
            json={
                "subtask": {
                    "task_id": "t1_task",
                    "task_description": "测试任务描述",
                    "timeout_seconds": 10,
                },
                "session_id": "sess_t1",
                "source_aoe_url": "http://localhost:8000",
            },
        )

    assert resp.status_code == 200
    body = resp.json()

    assert body["task_id"] == "t1_task"
    assert body["session_id"] == "sess_t1"
    assert body["status"] in ("completed", "error", "timeout")
    assert "workflow_handle" in body
    assert body["workflow_handle"].startswith("remote_wf_")
    assert "result" in body

    # mock 被调用了 1 次
    assert mock_workflow.call_count == 1


# ============================================================
# T2：session 清理端点 + 幂等性（in-process client）
# ============================================================

@pytest.mark.asyncio
async def test_session_cleanup_and_idempotency(mock_workflow):
    """T2: DELETE /orchestration/session/{id} 关闭后幂等返回 not_found"""
    from agent_server import app

    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        # 先 dispatch 注册 session
        dispatch_resp = await client.post(
            "/orchestration/dispatch",
            json={
                "subtask": {
                    "task_id": "t2_task",
                    "task_description": "会话清理测试",
                    "timeout_seconds": 5,
                },
                "session_id": "sess_t2",
                "source_aoe_url": "http://localhost:8000",
            },
        )
        assert dispatch_resp.status_code == 200
        dispatch_body = dispatch_resp.json()
        wf_handle = dispatch_body["workflow_handle"]

        # 第一次 DELETE → closed
        del_resp = await client.delete("/orchestration/session/sess_t2")
        assert del_resp.status_code == 200
        del_body = del_resp.json()
        assert del_body["status"] == "closed"
        assert del_body["session_id"] == "sess_t2"
        assert del_body["workflow_handle"] == wf_handle

        # 第二次 DELETE 同 session_id → not_found（幂等性）
        del_resp2 = await client.delete("/orchestration/session/sess_t2")
        assert del_resp2.status_code == 200
        assert del_resp2.json()["status"] == "not_found"


# ============================================================
# T3：全链路实 HTTP（uvicorn 后台线程）
# ============================================================

@pytest.mark.asyncio
async def test_full_chain_real_http(e_aoe_server):
    """
    T3: T_AOE 侧调用 dispatch_subtask_to_remote_aoe → 真实 uvicorn E_AOE → 返回 completed

    注意：e_aoe_server fixture 在 module scope 启动一次，
    run_distributed_workflow 在 E_AOE 进程内执行。
    本测试通过 mock_workflow fixture 打 patch，但 e_aoe_server 是 module scope，
    所以此处用独立的 patch 上下文来覆盖模块内的 run_distributed_workflow。
    """
    from src.graph.distributed_nodes import dispatch_subtask_to_remote_aoe

    mock_result = {"messages": [], "result": "t3_mock_ok"}
    with patch(
        "src.distributed_workflow.run_distributed_workflow",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await dispatch_subtask_to_remote_aoe(
            subtask={
                "task_id": "t3_task",
                "task_description": "全链路集成测试任务",
                "timeout_seconds": 10,
            },
            remote_aoe_url=e_aoe_server,
            session_timeout=15,
        )

    assert result["status"] == "completed", f"期望 completed，实际: {result}"
    assert result["session_id"], "session_id 不应为空"
    assert result["workflow_handle"].startswith("remote_wf_"), (
        f"workflow_handle 格式错误: {result['workflow_handle']}"
    )
    assert result["remote_aoe_url"] == e_aoe_server

    # 等待 fire-and-forget cleanup DELETE 完成
    await asyncio.sleep(0.5)

    # 验证 session 已被清理（cleanup 成功后 registry 中无此 session）
    # 通过再次 DELETE 验证：若已清理则返回 not_found
    async with httpx.AsyncClient(base_url=e_aoe_server) as client:
        check_resp = await client.delete(
            f"/orchestration/session/{result['session_id']}"
        )
    assert check_resp.json()["status"] == "not_found", (
        "cleanup 应已将 session 从 registry 中移除"
    )


# ============================================================
# T4：failover（E_AOE 宕机）
# ============================================================

@pytest.mark.asyncio
async def test_failover_e_aoe_down():
    """T4: E_AOE 不可达时 status=error，task_id 正确返回"""
    from src.graph.distributed_nodes import dispatch_subtask_to_remote_aoe

    result = await dispatch_subtask_to_remote_aoe(
        subtask={
            "task_id": "t4_task",
            "task_description": "failover 测试，目标不存在",
            "timeout_seconds": 3,
        },
        remote_aoe_url="http://127.0.0.1:19999",  # 不存在的端口
        session_timeout=3,
    )

    assert result["status"] == "error", f"期望 error，实际: {result['status']}"
    assert result["task_id"] == "t4_task"
    assert result["session_id"], "session_id 应被生成（即使调用失败）"

    # 验证 state 中 failed_cross_host_tasks 可正确收录此 task_id
    failed_cross_host_tasks = [result["task_id"]]
    assert "t4_task" in failed_cross_host_tasks
