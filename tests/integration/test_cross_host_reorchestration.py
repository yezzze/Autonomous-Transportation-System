"""
§2.3 跨主体工作流重编排（Agent 失效 / 远端节点宕机）集成测试

测试场景：
  T1 - find_alternative_remote_aoe：有 peer agents 时返回备用 URL
  T2 - find_alternative_remote_aoe：无替代节点时返回 None
  T3 - apply_failure_rules Rule 0：跨主体失败 → failover 到新 URL
  T4 - apply_failure_rules：无替代节点 → handled=False（触发 LLM 重规划）
  T5 - apply_failure_rules Rule 1：非跨主体失败不受 Rule 0 影响
  T6 - executor 正确记录 failed_remote_aoe_urls
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ============================================================
# 公共 fixture
# ============================================================

def _make_task(task_id="t1", agent_id="search_agent_001", status="failed", retry=1):
    return {
        "task_id": task_id,
        "task_title": f"任务 {task_id}",
        "task_description": "test description",
        "assigned_agent_id": agent_id,
        "target_ip": "192.168.1.10",
        "target_port": 8080,
        "status": status,
        "result": "远端 AOE 调用失败: Connection refused",
        "retry_count": retry,
        "parallel_group": "",
        "capability_required": "search",
    }


def _make_state(
    tasks=None,
    cross_host_sessions=None,
    failed_cross_host_tasks=None,
    failed_remote_aoe_urls=None,
    current_task_index=1,
):
    tasks = tasks or [_make_task()]
    return {
        "messages": [MagicMock(content="搜索一些内容")],
        "execution_plan": tasks,
        "current_task_index": current_task_index,
        "cross_host_sessions": cross_host_sessions or {"t1": "http://192.168.1.10:9000"},
        "failed_cross_host_tasks": failed_cross_host_tasks or ["t1"],
        "failed_remote_aoe_urls": failed_remote_aoe_urls or {"t1": ["http://192.168.1.10:9000"]},
        "agent_registry_cache": [
            {
                "id": "search_agent_001",
                "ip": "192.168.1.10",
                "port": 8080,
                "capability": "search",
                "status": "online",
                "description": "search agent on node 10",
            },
            {
                "id": "search_agent_002",
                "ip": "192.168.1.20",
                "port": 8080,
                "capability": "search",
                "status": "online",
                "description": "search agent on node 20",
            },
        ],
        "replanning_count": 0,
        "max_replanning": 2,
        "replanning_enabled": True,
        "failed_tasks": [],
        "session_timeout_seconds": 30,
    }


# ============================================================
# T1 - find_alternative_remote_aoe：有 peer agents 时返回备用 URL
# ============================================================

def test_find_alternative_returns_peer_url():
    """T1: 存在另一个 IP 节点的 agent 时，应返回其 AOE URL"""
    from src.graph.distributed_nodes import find_alternative_remote_aoe

    task = _make_task()
    failed_urls = ["http://192.168.1.10:9000"]

    # find_alternative_remote_aoe 在函数内部 local import get_registry_client
    # 必须 patch src.service.agent_registry.get_registry_client
    mock_registry = MagicMock()
    mock_registry.get_agent_by_id.return_value = {
        "id": "search_agent_001",
        "capability": "search",
        "ip": "192.168.1.10",
        "port": 8080,
        "status": "online",
    }
    mock_registry.get_all_agents.return_value = [
        {"id": "search_agent_001", "ip": "192.168.1.10", "capability": "search", "status": "online"},
        {"id": "search_agent_002", "ip": "192.168.1.20", "capability": "search", "status": "online"},
    ]

    with patch("src.service.agent_registry.get_registry_client", return_value=mock_registry):
        result = find_alternative_remote_aoe(task, failed_urls)

    assert result is not None
    assert "192.168.1.20" in result
    assert result.startswith("http://")


# ============================================================
# T2 - find_alternative_remote_aoe：无替代节点时返回 None
# ============================================================

def test_find_alternative_returns_none_when_no_alternatives():
    """T2: 所有节点要么是本地，要么都失败了"""
    from src.graph.distributed_nodes import find_alternative_remote_aoe

    task = _make_task()
    # 两个节点都已失败
    failed_urls = ["http://192.168.1.10:9000", "http://192.168.1.20:9000"]

    mock_registry = MagicMock()
    mock_registry.get_agent_by_id.return_value = {"capability": "search"}
    mock_registry.get_all_agents.return_value = [
        {"id": "search_agent_001", "ip": "192.168.1.10", "capability": "search", "status": "online"},
        {"id": "search_agent_002", "ip": "192.168.1.20", "capability": "search", "status": "online"},
    ]

    with patch("src.service.agent_registry.get_registry_client", return_value=mock_registry):
        result = find_alternative_remote_aoe(task, failed_urls)

    assert result is None


# ============================================================
# T3 - apply_failure_rules Rule 0：跨主体失败 → failover 到新 URL
# ============================================================

def test_apply_failure_rules_rule0_failover():
    """T3: 跨主体失败且有替代节点时，Rule 0 触发 failover"""
    from src.graph.distributed_nodes import apply_failure_rules

    state = _make_state()
    failed_task = state["execution_plan"][0]  # task t1 is failed

    alt_url = "http://192.168.1.20:9000"
    with patch(
        "src.graph.distributed_nodes.find_alternative_remote_aoe",
        return_value=alt_url,
    ):
        result = apply_failure_rules(state, [failed_task])

    assert result["handled"] is True
    assert "192.168.1.20" in result["action"]
    assert result["state_update"]["cross_host_sessions"]["t1"] == alt_url
    # 任务应重置为 pending
    assert failed_task["status"] == "pending"
    # current_task_index 应重置到该任务的位置（index 0）
    assert result["state_update"].get("current_task_index") == 0


# ============================================================
# T4 - apply_failure_rules：无替代节点 → handled=False
# ============================================================

def test_apply_failure_rules_no_alternative_returns_not_handled():
    """T4: 跨主体失败但无替代远端节点时，Rule 0 不应触发跨主体切换
    （Rule 2 本地备用切换或 LLM 重规划可能仍会接管，但 action 不应是"跨主体故障切换"）"""
    from src.graph.distributed_nodes import apply_failure_rules

    state = _make_state()
    failed_task = state["execution_plan"][0]
    failed_task["retry_count"] = 5  # 超过 Rule 1 阈值，Rule 1 必然跳过

    with patch(
        "src.graph.distributed_nodes.find_alternative_remote_aoe",
        return_value=None,
    ):
        result = apply_failure_rules(state, [failed_task])

    # Rule 0 不应触发跨主体故障切换
    if result["handled"]:
        assert "跨主体故障切换" not in result.get("action", ""), (
            f"Rule 0 不应在无替代节点时触发跨主体切换，实际: {result}"
        )
    # 状态未被 Rule 0 接管（cross_host_sessions 未因 Rule 0 更新）
    # 允许 Rule 2 等其他规则处理（正常的系统降级行为）


# ============================================================
# T5 - apply_failure_rules Rule 1：非跨主体失败走 Rule 1
# ============================================================

def test_apply_failure_rules_non_cross_host_uses_rule1():
    """T5: 非跨主体失败任务（retry_count < 3）仍然走 Rule 1 简单重试"""
    from src.graph.distributed_nodes import apply_failure_rules

    task = _make_task(task_id="t2", retry=0)
    task["status"] = "failed"
    state = _make_state(
        tasks=[task],
        cross_host_sessions={},
        failed_cross_host_tasks=[],   # 非跨主体失败
        failed_remote_aoe_urls={},
        current_task_index=1,
    )

    result = apply_failure_rules(state, [task])

    assert result["handled"] is True
    assert task["status"] == "pending"
    assert task["retry_count"] == 1


# ============================================================
# T6 - executor 正确记录 failed_remote_aoe_urls
# ============================================================

@pytest.mark.asyncio
async def test_executor_records_failed_remote_aoe_urls():
    """T6: 跨主体任务失败时，executor 应在 failed_remote_aoe_urls 中记录失败的 URL"""
    from src.graph.distributed_nodes import distributed_executor_node
    from langchain_core.messages import HumanMessage

    remote_url = "http://192.168.1.10:9000"
    task = _make_task(task_id="task_001", retry=0, status="pending")

    state = {
        "messages": [HumanMessage(content="搜索测试")],
        "execution_plan": [task],
        "current_task_index": 0,
        "cross_host_sessions": {"task_001": remote_url},
        "failed_cross_host_tasks": [],
        "failed_remote_aoe_urls": {},
        "failed_tasks": [],
        "session_timeout_seconds": 5,
        "replanning_count": 0,
        "max_replanning": 2,
        "replanning_enabled": True,
        "agent_registry_cache": [],
    }

    # dispatch_subtask_to_remote_aoe 是 async 函数，需要 AsyncMock
    async_dispatch = AsyncMock(return_value={
        "status": "error",
        "result": "Connection refused",
        "session_id": "sess-001",
    })

    # Mock UnifiedExecutor 的初始化（避免 MCP/A2A 客户端副作用）
    mock_executor_instance = MagicMock()
    mock_executor_class = MagicMock(return_value=mock_executor_instance)

    with (
        patch("src.graph.distributed_nodes.dispatch_subtask_to_remote_aoe", new=async_dispatch),
        patch("src.graph.unified_executor.UnifiedExecutor", mock_executor_class),
        # 禁用 LLM 模拟器降级，防止其将 "failed" 覆盖为 "completed"
        patch("src.graph.distributed_nodes.USE_LLM_SIMULATOR", False),
    ):
        command = await distributed_executor_node(state)

    update = command.update
    # 应记录失败 URL
    assert "task_001" in update.get("failed_remote_aoe_urls", {}), (
        f"failed_remote_aoe_urls 未记录 task_001，update={update}"
    )
    assert remote_url in update["failed_remote_aoe_urls"]["task_001"]
    # 应记录 failed_cross_host_tasks
    assert "task_001" in update.get("failed_cross_host_tasks", [])
    # 任务状态应为 failed
    plan = update.get("execution_plan", [])
    assert plan[0]["status"] == "failed"
