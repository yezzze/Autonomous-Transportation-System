"""
资源注册与发现中心 (RRDC - Resource Registration & Discovery Center)

编排层组件，负责：
- 管理各节点（终端/边缘/云）的计算、通信资源信息
- 为调度器（ASD）提供资源查询接口
- 记录资源分配/释放，维护可用资源视图

当前实现：Mock 级别，内存存储，提供 3 个预定义节点。
生产环境：替换为 etcd / Consul / 自定义 REST 服务。
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class ResourceInfo:
    """节点资源信息"""
    node_id: str
    node_type: str          # "device" | "edge" | "cloud"
    ip: str
    # CPU
    cpu_total: float        # 总核心数
    cpu_available: float    # 可用核心数
    # 内存
    mem_total_mb: int       # 总内存 (MB)
    mem_available_mb: int   # 可用内存 (MB)
    # GPU（可选）
    gpu_count: int = 0
    gpu_available: int = 0
    # 元数据
    tags: List[str] = field(default_factory=list)
    status: str = "online"  # "online" | "offline" | "degraded"
    last_heartbeat: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    @property
    def cpu_usage_percent(self) -> float:
        if self.cpu_total == 0:
            return 0.0
        return (1 - self.cpu_available / self.cpu_total) * 100

    @property
    def mem_usage_percent(self) -> float:
        if self.mem_total_mb == 0:
            return 0.0
        return (1 - self.mem_available_mb / self.mem_total_mb) * 100


class ResourceRegistry:
    """
    资源注册与发现中心（RRDC）

    核心接口：
    - register_node()              — 注册节点资源
    - query_available_resources()  — 查询满足条件的节点列表
    - allocate()                   — 分配资源（减少可用量）
    - release()                    — 释放资源（增加可用量）
    - update_heartbeat()           — 节点心跳更新
    - get_summary()                — 资源总览统计
    """

    def __init__(self):
        # node_id → ResourceInfo
        self._nodes: Dict[str, ResourceInfo] = {}
        self._init_mock_nodes()
        logger.info(
            f"ResourceRegistry (RRDC) 初始化完成，已加载 {len(self._nodes)} 个节点"
        )

    # ------------------------------------------------------------------
    # Mock 初始化：与 agent_registry.json 中 IP 对应的 3 个节点
    # ------------------------------------------------------------------

    def _init_mock_nodes(self):
        """初始化预定义节点，与 agent_registry.json 中的 Agent IP 对应"""
        nodes = [
            ResourceInfo(
                node_id="node_localhost",
                node_type="device",
                ip="host.docker.internal",
                cpu_total=8.0,
                cpu_available=6.0,
                mem_total_mb=16384,
                mem_available_mb=12288,
                tags=["search", "web"],
                last_heartbeat=datetime.utcnow().isoformat(),
            ),
            ResourceInfo(
                node_id="node_edge_01",
                node_type="edge",
                ip="192.168.1.102",
                cpu_total=16.0,
                cpu_available=14.0,
                mem_total_mb=32768,
                mem_available_mb=28672,
                gpu_count=1,
                gpu_available=1,
                tags=["compute", "nlp", "vision"],
                last_heartbeat=datetime.utcnow().isoformat(),
            ),
            ResourceInfo(
                node_id="node_edge_02",
                node_type="edge",
                ip="192.168.1.105",
                cpu_total=8.0,
                cpu_available=7.0,
                mem_total_mb=16384,
                mem_available_mb=15360,
                tags=["code_execution", "web_interaction"],
                last_heartbeat=datetime.utcnow().isoformat(),
            ),
        ]
        for node in nodes:
            self._nodes[node.node_id] = node

    # ------------------------------------------------------------------
    # 注册接口
    # ------------------------------------------------------------------

    def register_node(self, resource: ResourceInfo) -> bool:
        """
        注册或更新节点资源信息

        Args:
            resource: 节点资源信息

        Returns:
            True 表示首次注册，False 表示更新已有节点
        """
        is_new = resource.node_id not in self._nodes
        resource.last_heartbeat = datetime.utcnow().isoformat()
        self._nodes[resource.node_id] = resource
        action = "注册" if is_new else "更新"
        logger.info(
            f"[RRDC] {action}节点: node_id={resource.node_id}, "
            f"type={resource.node_type}, ip={resource.ip}"
        )
        return is_new

    def deregister_node(self, node_id: str) -> bool:
        """注销节点"""
        if node_id in self._nodes:
            del self._nodes[node_id]
            logger.info(f"[RRDC] 注销节点: node_id={node_id}")
            return True
        return False

    def update_heartbeat(self, node_id: str) -> bool:
        """更新节点心跳时间"""
        if node_id in self._nodes:
            self._nodes[node_id].last_heartbeat = datetime.utcnow().isoformat()
            return True
        return False

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def query_available_resources(
        self,
        min_cpu: float = 0.5,
        min_mem_mb: int = 256,
        node_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[ResourceInfo]:
        """
        查询满足条件的节点列表（按可用 CPU 降序排列）

        Args:
            min_cpu:    最低可用 CPU 核心数
            min_mem_mb: 最低可用内存 (MB)
            node_type:  节点类型过滤 ("device"/"edge"/"cloud")
            tags:       标签过滤（所需标签至少有一个匹配）

        Returns:
            符合条件的 ResourceInfo 列表，按 cpu_available 降序
        """
        results = []
        for node in self._nodes.values():
            if node.status != "online":
                continue
            if node.cpu_available < min_cpu:
                continue
            if node.mem_available_mb < min_mem_mb:
                continue
            if node_type and node.node_type != node_type:
                continue
            if tags:
                if not any(t in node.tags for t in tags):
                    continue
            results.append(node)

        results.sort(key=lambda n: n.cpu_available, reverse=True)
        return results

    def get_node(self, node_id: str) -> Optional[ResourceInfo]:
        """按 node_id 获取节点信息"""
        return self._nodes.get(node_id)

    def get_all_nodes(self) -> List[ResourceInfo]:
        """获取所有节点"""
        return list(self._nodes.values())

    # ------------------------------------------------------------------
    # 资源分配/释放
    # ------------------------------------------------------------------

    def allocate(
        self,
        node_id: str,
        cpu_cores: float,
        memory_mb: int,
    ) -> bool:
        """
        为 Agent 部署分配资源

        Args:
            node_id:    目标节点 ID
            cpu_cores:  申请的 CPU 核心数
            memory_mb:  申请的内存 (MB)

        Returns:
            True 分配成功，False 资源不足或节点不存在
        """
        node = self._nodes.get(node_id)
        if not node:
            logger.warning(f"[RRDC] allocate 失败: node_id={node_id} 不存在")
            return False

        if node.cpu_available < cpu_cores or node.mem_available_mb < memory_mb:
            logger.warning(
                f"[RRDC] allocate 失败: 资源不足 "
                f"(需cpu={cpu_cores},mem={memory_mb}MB; "
                f"可用cpu={node.cpu_available},mem={node.mem_available_mb}MB)"
            )
            return False

        node.cpu_available -= cpu_cores
        node.mem_available_mb -= memory_mb
        logger.info(
            f"[RRDC] 分配资源: node={node_id}, cpu={cpu_cores}, mem={memory_mb}MB"
        )
        return True

    def release(
        self,
        node_id: str,
        cpu_cores: float,
        memory_mb: int,
    ) -> bool:
        """
        释放 Agent 占用的资源

        Args:
            node_id:    目标节点 ID
            cpu_cores:  归还的 CPU 核心数
            memory_mb:  归还的内存 (MB)

        Returns:
            True 释放成功，False 节点不存在
        """
        node = self._nodes.get(node_id)
        if not node:
            logger.warning(f"[RRDC] release 失败: node_id={node_id} 不存在")
            return False

        # 不超过总量上限
        node.cpu_available = min(node.cpu_available + cpu_cores, node.cpu_total)
        node.mem_available_mb = min(
            node.mem_available_mb + memory_mb, node.mem_total_mb
        )
        logger.info(
            f"[RRDC] 释放资源: node={node_id}, cpu={cpu_cores}, mem={memory_mb}MB"
        )
        return True

    # ------------------------------------------------------------------
    # 统计接口
    # ------------------------------------------------------------------

    def get_summary(self) -> Dict:
        """返回整体资源使用情况"""
        nodes = list(self._nodes.values())
        total_cpu = sum(n.cpu_total for n in nodes)
        avail_cpu = sum(n.cpu_available for n in nodes)
        total_mem = sum(n.mem_total_mb for n in nodes)
        avail_mem = sum(n.mem_available_mb for n in nodes)
        return {
            "total_nodes": len(nodes),
            "online_nodes": sum(1 for n in nodes if n.status == "online"),
            "total_cpu": total_cpu,
            "available_cpu": avail_cpu,
            "cpu_usage_percent": round((1 - avail_cpu / total_cpu) * 100, 1) if total_cpu else 0,
            "total_mem_mb": total_mem,
            "available_mem_mb": avail_mem,
            "mem_usage_percent": round((1 - avail_mem / total_mem) * 100, 1) if total_mem else 0,
        }


# ======================================================================
# 单例访问
# ======================================================================
_registry_instance: Optional[ResourceRegistry] = None


def get_resource_registry() -> ResourceRegistry:
    """获取全局 ResourceRegistry 单例"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ResourceRegistry()
    return _registry_instance
