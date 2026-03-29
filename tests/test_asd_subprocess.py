"""
ASD subprocess 部署集成测试

验证 AgentScheduler 能通过 subprocess 真正启动/停止 agent_server.py 进程：
  T1 - deploy_agent() 启动进程，/health 可访问，ARDC 注册正确
  T2 - shutdown_agent() 终止进程，/health 不可达
  T3 - deploy 后 ARDC 中 agent 的 ip:port 为真实值
  T4 - node_id 非 localhost 时降级 mock（不启动进程）
"""

import sys
import os
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


# ============================================================
# T1：deploy_agent 真实启动进程
# ============================================================

def test_deploy_agent_starts_process():
    """T1: deploy_agent 启动真实 subprocess，/health 可访问"""
    from src.service.agent_scheduler import AgentScheduler

    scheduler = AgentScheduler()
    agent_id = "test_deploy_search_001"

    record = scheduler.deploy_agent(
        image_id="search_agent:v1",
        agent_id=agent_id,
        node_id="localhost",
    )

    try:
        assert record.status == "running", f"期望 running，实际: {record.status}"
        assert agent_id in scheduler._processes, "进程句柄应被记录"
        assert agent_id in scheduler._agent_ports, "端口应被记录"

        port = scheduler._agent_ports[agent_id]
        assert _is_port_open(port), f"agent_server 应在 {port} 上就绪"

    finally:
        # 无论测试是否通过都清理进程
        scheduler.shutdown_agent(agent_id)


# ============================================================
# T2：shutdown_agent 终止进程
# ============================================================

def test_shutdown_agent_terminates_process():
    """T2: shutdown_agent 终止进程，/health 随后不可达"""
    from src.service.agent_scheduler import AgentScheduler

    scheduler = AgentScheduler()
    agent_id = "test_shutdown_compute_001"

    record = scheduler.deploy_agent(
        image_id="compute_agent:v1",
        agent_id=agent_id,
        node_id="127.0.0.1",
    )

    assert record.status == "running"
    port = scheduler._agent_ports[agent_id]
    assert _is_port_open(port), "关闭前 /health 应可访问"

    success = scheduler.shutdown_agent(agent_id)

    assert success is True
    assert agent_id not in scheduler._processes, "进程句柄应被移除"
    assert agent_id not in scheduler._agent_ports, "端口记录应被移除"

    # 等待端口释放
    time.sleep(0.3)
    assert not _is_port_open(port, timeout=0.3), "关闭后 /health 不应可访问"


# ============================================================
# T3：ARDC 注册 ip:port 为真实值
# ============================================================

def test_ardc_registers_real_port():
    """T3: deploy 后 ARDC 中该 agent 的 port 为真实分配端口"""
    from src.service.agent_scheduler import AgentScheduler
    from src.service.agent_registry import AgentRegistryClient

    scheduler = AgentScheduler()
    # 使用独立 registry 实例避免单例污染
    registry = AgentRegistryClient()
    agent_id = "test_ardc_nlp_001"

    record = scheduler.deploy_agent(
        image_id="nlp_agent:v1",
        agent_id=agent_id,
        node_id="localhost",
    )

    try:
        assert record.status == "running"
        port = scheduler._agent_ports[agent_id]

        # 直接查这个 scheduler 用的全局 registry 单例
        from src.service.agent_registry import get_registry_client
        reg = get_registry_client()
        info = reg.get_agent_by_id(agent_id)

        assert info is not None, f"ARDC 中应能找到 {agent_id}"
        assert info["port"] == port, (
            f"ARDC 中 port={info['port']} 应与实际分配端口 {port} 一致"
        )
        assert info["ip"] == "127.0.0.1"
        assert info["capability"] == "nlp"
        assert info["status"] == "online"

    finally:
        scheduler.shutdown_agent(agent_id)


# ============================================================
# T4：非 localhost node_id 降级 mock
# ============================================================

def test_remote_node_id_falls_back_to_mock():
    """T4: node_id 指向非本机时，不启动进程，降级为 mock 记录"""
    from src.service.agent_scheduler import AgentScheduler

    scheduler = AgentScheduler()
    agent_id = "test_remote_node_001"

    record = scheduler.deploy_agent(
        image_id="vision_agent:v1",
        agent_id=agent_id,
        node_id="192.168.1.99",  # 非本机
    )

    assert record.status == "running", "降级 mock 也应标记为 running"
    assert agent_id not in scheduler._processes, "远端节点不应启动本地进程"
    assert agent_id not in scheduler._agent_ports, "远端节点无本地端口"
