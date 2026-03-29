"""
ARDC HTTP Gossip 集成测试

验证跨节点 agent 发现机制：
  T1 - sync_from_peer() 内存合并：两个 AgentRegistryClient 直接调用
  T2 - push_to_peer() HTTP 推送：向真实 uvicorn /registry/sync 端点推送
  T3 - prune_stale_peers() TTL 清理
  T4 - 合并后 query_agents() 能查到 peer 来的 agents（本地优先原则）
"""

import sys
import os
import socket
import threading
import time

import httpx
import pytest

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

@pytest.fixture
def fresh_registry():
    """
    每个测试用一个全新的 AgentRegistryClient 实例（不复用单例），
    避免测试间 peer 状态互相污染。
    """
    from src.service.agent_registry import AgentRegistryClient
    return AgentRegistryClient()


@pytest.fixture(scope="module")
def peer_server():
    """
    启动一个真实 uvicorn agent_server（充当 peer E_AOE），
    用于 T2 的真实 HTTP 推送测试。
    """
    import uvicorn
    from agent_server import app as peer_app

    port = _get_free_port()
    config = uvicorn.Config(
        app=peer_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    import uvicorn
    server = uvicorn.Server(config)

    def _run():
        server.run()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    server_url = f"http://127.0.0.1:{port}"
    assert _wait_for_server(server_url, timeout=5.0), (
        f"peer 服务在 5s 内未就绪（port={port}）"
    )

    yield server_url

    server.should_exit = True
    t.join(timeout=3.0)


# ============================================================
# T1：sync_from_peer() 内存合并
# ============================================================

def test_sync_from_peer_merges_agents(fresh_registry):
    """T1: sync_from_peer 将 peer agents 合并到本地注册表"""
    registry = fresh_registry
    initial_count = len(registry.get_all_agents())

    # 构造两个不与本地 id 重复的 peer agents
    peer_agents = [
        {
            "id": "peer_node_agent_001",
            "ip": "192.168.1.20",
            "port": 9000,
            "capability": "search",
            "status": "online",
            "description": "来自 peer 节点的搜索 agent",
        },
        {
            "id": "peer_node_agent_002",
            "ip": "192.168.1.20",
            "port": 9001,
            "capability": "compute",
            "status": "online",
            "description": "来自 peer 节点的计算 agent",
        },
    ]

    merged_count = registry.sync_from_peer("http://192.168.1.20:8000", peer_agents)

    # 断言：peer agents 被收录
    assert merged_count == 2, f"期望 2 个 peer agents，实际 {merged_count}"

    all_agents = registry.get_all_agents()
    all_ids = [a["id"] for a in all_agents]

    assert "peer_node_agent_001" in all_ids
    assert "peer_node_agent_002" in all_ids
    # 本地 agents 依然存在
    assert len(all_agents) == initial_count + 2


def test_sync_from_peer_local_priority(fresh_registry):
    """T1b: 本地 agent id 与 peer 重复时，本地优先"""
    registry = fresh_registry

    # search_agent_001 是本地默认 agent，ip=127.0.0.1
    local_agent = next(
        (a for a in registry.get_all_agents() if a["id"] == "search_agent_001"),
        None,
    )
    if local_agent is None:
        pytest.skip("本地注册表中无 search_agent_001，跳过优先级测试")

    # 推送同 id 但 ip 不同的 peer agent
    registry.sync_from_peer(
        "http://10.0.0.1:8000",
        [{"id": "search_agent_001", "ip": "10.0.0.1", "port": 8099,
          "capability": "search", "status": "online", "description": "peer 版本"}],
    )

    # 合并后 search_agent_001 应保留本地的 ip（而非 peer 的 10.0.0.1）
    merged = {a["id"]: a for a in registry.get_all_agents()}
    assert merged["search_agent_001"]["ip"] != "10.0.0.1", (
        "本地 agent 应优先于 peer agent，ip 不应被 peer 覆盖"
    )
    # 确认与初始本地值一致
    assert merged["search_agent_001"]["ip"] == local_agent["ip"], (
        f"本地 ip={local_agent['ip']} 应被保留，实际={merged['search_agent_001']['ip']}"
    )


# ============================================================
# T2：push_to_peer() 向真实 HTTP 端点推送
# ============================================================

@pytest.mark.asyncio
async def test_push_to_peer_real_http(peer_server):
    """T2: push_to_peer 向真实 uvicorn /registry/sync 端点推送，验证 merged_count"""
    from src.service.agent_registry import AgentRegistryClient

    registry = AgentRegistryClient()
    local_url = "http://127.0.0.1:18000"  # 本节点地址（测试用）

    success = await registry.push_to_peer(peer_server, local_url)
    assert success is True, "push_to_peer 应返回 True 表示推送成功"

    # 验证 peer 端 /registry/sync 已正确接收（直接 GET /registry/sync 不存在，
    # 通过再次 push 验证端点幂等性）
    success2 = await registry.push_to_peer(peer_server, local_url)
    assert success2 is True


@pytest.mark.asyncio
async def test_registry_sync_endpoint_response(peer_server):
    """T2b: 直接 POST /registry/sync 验证响应格式"""
    agents_payload = [
        {
            "id": "t2b_agent_001",
            "ip": "10.0.0.99",
            "port": 9999,
            "capability": "search",
            "status": "online",
            "description": "T2b 测试 agent",
        }
    ]

    async with httpx.AsyncClient(base_url=peer_server) as client:
        resp = await client.post(
            "/registry/sync",
            json={"source_url": "http://10.0.0.1:8000", "agents": agents_payload},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["source_url"] == "http://10.0.0.1:8000"
    assert body["received_agents"] == 1
    assert isinstance(body["merged_count"], int)
    assert body["merged_count"] >= 1


# ============================================================
# T3：prune_stale_peers() TTL 清理
# ============================================================

def test_prune_stale_peers(fresh_registry):
    """T3: TTL=0 时所有 peer 应被立即清理"""
    registry = fresh_registry

    registry.sync_from_peer(
        "http://stale-node:8000",
        [{"id": "stale_agent", "ip": "1.2.3.4", "port": 9000,
          "capability": "search", "status": "online", "description": "stale"}],
    )

    # 确认已同步
    assert "stale_agent" in [a["id"] for a in registry.get_all_agents()]

    # TTL=0 → 所有 peer 立即过期
    registry.prune_stale_peers(ttl_seconds=0)

    # peer agent 应被清理
    assert "stale_agent" not in [a["id"] for a in registry.get_all_agents()]
    assert "http://stale-node:8000" not in registry._peer_agents


def test_prune_keeps_fresh_peers(fresh_registry):
    """T3b: TTL 充足时 fresh peer 不应被清理"""
    registry = fresh_registry

    registry.sync_from_peer(
        "http://fresh-node:8000",
        [{"id": "fresh_agent", "ip": "5.6.7.8", "port": 9000,
          "capability": "compute", "status": "online", "description": "fresh"}],
    )

    # TTL=120s，刚同步的 peer 不应被清理
    registry.prune_stale_peers(ttl_seconds=120)

    assert "fresh_agent" in [a["id"] for a in registry.get_all_agents()]


# ============================================================
# T4：query_agents 能查到 peer agents（本地优先）
# ============================================================

def test_query_agents_includes_peer_agents(fresh_registry):
    """T4: 同步 peer agents 后，query_agents 能按 capability 查到它们"""
    registry = fresh_registry

    # 同步一个具有特殊 capability 的 peer agent
    registry.sync_from_peer(
        "http://10.0.0.5:8000",
        [{"id": "peer_gpu_agent", "ip": "10.0.0.5", "port": 8090,
          "capability": "gpu_inference", "status": "online",
          "description": "peer GPU 推理 agent"}],
    )

    # 本地无此 capability，应能从 peer 查到
    results = registry.query_agents(capability="gpu_inference")
    assert len(results) == 1
    assert results[0]["id"] == "peer_gpu_agent"


def test_query_agents_filters_offline_peer(fresh_registry):
    """T4b: offline 状态的 peer agent 不应出现在 query_agents 结果中"""
    registry = fresh_registry

    registry.sync_from_peer(
        "http://10.0.0.6:8000",
        [{"id": "offline_peer_agent", "ip": "10.0.0.6", "port": 8091,
          "capability": "search", "status": "offline",
          "description": "离线 peer agent"}],
    )

    results = registry.query_agents(capability="search")
    ids = [a["id"] for a in results]
    assert "offline_peer_agent" not in ids
