"""
COMM 消息路由中间件 (Message Router)

编排层服务组件，负责：
- 维护 capability → [agent_url] 的路由表（从 AgentRegistryClient 读取）
- 按能力路由：从多个候选 Agent 中选一个（简单轮询策略）
- 直接路由：按 agent_id 精确查找 URL
- 广播：向所有在线 Agent 发送消息

替代 UnifiedExecutor 中直接构造 agent_url 的逻辑，实现能力解耦。

对应接口文档：编排层 COMM 跨 Agent 通信模块
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class MessageRouter:
    """
    消息路由器（轻量内存实现）

    路由表从 AgentRegistryClient 动态读取，无需手动维护。

    核心方法：
    - register()               — 手动注册一条路由（供测试 / 单元测试用）
    - unregister()             — 注销路由
    - route_by_capability()    — 按能力路由，返回 agent_url
    - route_direct()           — 按 agent_id 精确路由，返回 agent_url
    - broadcast()              — 向所有 online Agent 广播，返回 URL 列表
    - get_all_urls()           — 获取指定能力的所有候选 URL
    """

    def __init__(self):
        # 手动注册的路由表（除 registry 外的补充）：agent_id → agent_url
        self._manual_routes: Dict[str, str] = {}
        # 轮询计数器：capability → 当前轮询索引
        self._rr_counters: Dict[str, int] = {}
        logger.info("MessageRouter 初始化完成")

    # ------------------------------------------------------------------
    # 手动路由注册（测试/覆盖 registry 用）
    # ------------------------------------------------------------------

    def register(self, agent_id: str, capabilities: List[str], url: str):
        """
        手动注册 Agent 路由。

        正常生产场景下，路由从 AgentRegistryClient 动态读取，
        此方法主要用于单元测试或临时覆盖。

        Args:
            agent_id:     Agent 唯一 ID
            capabilities: 此 Agent 支持的能力列表（如 ["search", "nlp"]）
            url:          Agent 服务地址，如 "http://192.168.1.10:8080"
        """
        self._manual_routes[agent_id] = url
        logger.debug(f"[Router] 手动注册: agent_id={agent_id}, url={url}, caps={capabilities}")

    def unregister(self, agent_id: str):
        """注销手动路由"""
        self._manual_routes.pop(agent_id, None)
        logger.debug(f"[Router] 注销: agent_id={agent_id}")

    # ------------------------------------------------------------------
    # 路由查询
    # ------------------------------------------------------------------

    def route_by_capability(self, capability: str) -> Optional[str]:
        """
        按能力路由，使用轮询（Round-Robin）策略均衡负载。

        查找顺序：
        1. AgentRegistryClient 中 online 且匹配 capability 的 Agent
        2. 手动注册路由（fallback）

        Args:
            capability: 所需能力，如 "search"、"nlp"、"compute"

        Returns:
            agent_url（如 "http://127.0.0.1:8001"），无可用 Agent 时返回 None
        """
        urls = self.get_all_urls(capability)
        if not urls:
            logger.warning(f"[Router] 无可用 Agent，capability={capability}")
            return None

        # 轮询
        idx = self._rr_counters.get(capability, 0) % len(urls)
        self._rr_counters[capability] = idx + 1
        url = urls[idx]
        logger.debug(f"[Router] route_by_capability: cap={capability} → {url}")
        return url

    def route_direct(self, agent_id: str) -> Optional[str]:
        """
        按 agent_id 精确路由。

        查找顺序：
        1. AgentRegistryClient 中匹配 agent_id 的 Agent
        2. 手动注册路由（fallback）

        Args:
            agent_id: Agent 标识

        Returns:
            agent_url 或 None
        """
        # 从 registry 查
        try:
            from src.service.agent_registry import get_registry_client
            info = get_registry_client().get_agent_by_id(agent_id)
            if info and info.get("status") == "online":
                url = f"http://{info['ip']}:{info['port']}"
                logger.debug(f"[Router] route_direct (registry): {agent_id} → {url}")
                return url
        except Exception:
            pass

        # fallback：手动路由
        url = self._manual_routes.get(agent_id)
        if url:
            logger.debug(f"[Router] route_direct (manual): {agent_id} → {url}")
        return url

    def broadcast(self, capability: Optional[str] = None) -> List[str]:
        """
        返回所有可广播目标的 URL 列表。

        Args:
            capability: 若指定，则只广播到该能力的 Agent；None 则广播全部 online Agent

        Returns:
            URL 列表（去重）
        """
        if capability:
            return self.get_all_urls(capability)

        # 全部 online Agent
        try:
            from src.service.agent_registry import get_registry_client
            agents = get_registry_client().query_agents(status="online")
            urls = list({f"http://{a['ip']}:{a['port']}" for a in agents})
            logger.debug(f"[Router] broadcast: {len(urls)} 个目标")
            return urls
        except Exception as e:
            logger.warning(f"[Router] broadcast 失败: {e}")
            return []

    def get_all_urls(self, capability: str) -> List[str]:
        """
        获取指定能力的所有候选 Agent URL。

        Args:
            capability: 能力标识

        Returns:
            URL 列表（已去重）
        """
        urls: List[str] = []

        # 1. 从 AgentRegistryClient 查
        try:
            from src.service.agent_registry import get_registry_client
            agents = get_registry_client().query_agents(
                capability=capability, status="online"
            )
            for a in agents:
                url = f"http://{a['ip']}:{a['port']}"
                if url not in urls:
                    urls.append(url)
        except Exception as e:
            logger.warning(f"[Router] registry 查询失败（capability={capability}）: {e}")

        # 2. 补充手动注册的路由（按 capability 匹配 agent_id 前缀）
        for agent_id, url in self._manual_routes.items():
            if capability.lower() in agent_id.lower() and url not in urls:
                urls.append(url)

        return urls


# ======================================================================
# 单例访问
# ======================================================================
_router_instance: Optional[MessageRouter] = None


def get_message_router() -> MessageRouter:
    """获取全局 MessageRouter 单例"""
    global _router_instance
    if _router_instance is None:
        _router_instance = MessageRouter()
    return _router_instance
