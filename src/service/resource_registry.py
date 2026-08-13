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
import threading
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
    # 展示名称；调度仍使用不可变的 Kubernetes node_id
    display_name: str = ""
    source_url: str = ""
    is_local: bool = True
    # GPU（可选）
    gpu_count: int = 0
    gpu_available: int = 0
    # 元数据
    tags: List[str] = field(default_factory=list)
    status: str = "online"  # "online" | "offline" | "degraded"
    last_heartbeat: str = ""

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.node_id

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
    - allocate()                   — 创建部署提交前的短期预留
    - release()                    — 撤销短期预留
    - update_heartbeat()           — 节点心跳更新
    - get_summary()                — 资源总览统计
    """

    def __init__(self):
        # node_id → ResourceInfo
        self._nodes: Dict[str, ResourceInfo] = {}
        # peer URL → 该 peer 最近一次 Gossip 推送的资源快照
        self._peer_nodes: Dict[str, List[ResourceInfo]] = {}
        # reservation_id → 部署提交前的短期资源预留
        self._reservations: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        # self._init_mock_nodes()
        self._init_kubernetes_nodes()
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
                gpu_count=2,
                gpu_available=2,
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

    def _init_kubernetes_nodes(self):
        """从 Kubernetes 获取节点资源信息并注册"""
        resources = self.get_kubernetes_available_resources()
        for resource in resources:
            self.register_node(resource)
        logger.info(
            f"[RRDC] Kubernetes 节点初始化完成，已注册 {len(resources)} 个节点"
        )

    def refresh_from_kubernetes(self) -> bool:
        """从 Kubernetes 刷新资源快照，并扣除尚未提交的短期预留。"""
        resources = self.get_kubernetes_available_resources()
        if not resources:
            logger.warning("[RRDC] Kubernetes 资源刷新未返回节点，保留上次快照")
            return False

        with self._lock:
            refreshed = {resource.node_id: resource for resource in resources}
            for reservation in self._reservations.values():
                node = refreshed.get(reservation["node_id"])
                if node is None:
                    continue
                node.cpu_available = max(
                    0.0, node.cpu_available - reservation["cpu_cores"]
                )
                node.mem_available_mb = max(
                    0, node.mem_available_mb - reservation["memory_mb"]
                )
                node.gpu_available = max(
                    0, node.gpu_available - reservation["gpu_count"]
                )
            self._nodes = refreshed
        return True

    def get_kubernetes_available_resources(self) -> List[ResourceInfo]:
        """
        从 Kubernetes 获取各节点当前可用的 CPU、内存和 GPU。

        可用量按节点 allocatable 减去所有未终止 Pod 的 requests 计算。
        本方法只返回实时查询结果，不修改资源注册表中的现有节点。
        """
        try:
            from kubernetes import client, config
            from kubernetes.utils.quantity import parse_quantity
        except (ImportError, ModuleNotFoundError) as exc:
            logger.warning("[RRDC] Kubernetes 客户端不可用: %s", exc)
            return []

        try:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()

            core_api = client.CoreV1Api()
            nodes = core_api.list_node().items
            pods = core_api.list_pod_for_all_namespaces().items
        except Exception as exc:
            logger.warning("[RRDC] 查询 Kubernetes 资源失败: %s", exc)
            return []

        def quantity(resources: Optional[Dict], name: str) -> float:
            """把 Kubernetes quantity 转为基础单位数值。"""
            value = (resources or {}).get(name)
            if value is None:
                return 0.0
            try:
                return float(parse_quantity(value))
            except (TypeError, ValueError):
                logger.warning(
                    "[RRDC] 无法解析 Kubernetes resource quantity: %s=%r",
                    name,
                    value,
                )
                return 0.0

        def container_requests(container) -> Dict[str, float]:
            requests = getattr(getattr(container, "resources", None), "requests", None)
            return {
                "cpu": quantity(requests, "cpu"),
                "memory": quantity(requests, "memory"),
                "gpu": quantity(requests, "nvidia.com/gpu"),
            }

        requested_by_node: Dict[str, Dict[str, float]] = {}
        for pod in pods:
            if getattr(getattr(pod, "status", None), "phase", None) in {
                "Succeeded",
                "Failed",
            }:
                continue

            spec = getattr(pod, "spec", None)
            node_name = getattr(spec, "node_name", None)
            if not node_name:
                continue

            regular = {"cpu": 0.0, "memory": 0.0, "gpu": 0.0}
            for container in getattr(spec, "containers", None) or []:
                requests = container_requests(container)
                for resource_name in regular:
                    regular[resource_name] += requests[resource_name]

            init_max = {"cpu": 0.0, "memory": 0.0, "gpu": 0.0}
            for container in getattr(spec, "init_containers", None) or []:
                requests = container_requests(container)
                for resource_name in init_max:
                    init_max[resource_name] = max(
                        init_max[resource_name], requests[resource_name]
                    )

            overhead = getattr(spec, "overhead", None) or {}
            effective = {
                "cpu": max(regular["cpu"], init_max["cpu"])
                + quantity(overhead, "cpu"),
                "memory": max(regular["memory"], init_max["memory"])
                + quantity(overhead, "memory"),
                "gpu": max(regular["gpu"], init_max["gpu"])
                + quantity(overhead, "nvidia.com/gpu"),
            }
            node_requests = requested_by_node.setdefault(
                node_name, {"cpu": 0.0, "memory": 0.0, "gpu": 0.0}
            )
            for resource_name in node_requests:
                node_requests[resource_name] += effective[resource_name]

        resources: List[ResourceInfo] = []
        for node in nodes:
            metadata = getattr(node, "metadata", None)
            status = getattr(node, "status", None)
            node_name = getattr(metadata, "name", "")
            allocatable = getattr(status, "allocatable", None) or {}
            requested = requested_by_node.get(
                node_name, {"cpu": 0.0, "memory": 0.0, "gpu": 0.0}
            )

            cpu_total = quantity(allocatable, "cpu")
            mem_total_bytes = quantity(allocatable, "memory")
            gpu_total = int(quantity(allocatable, "nvidia.com/gpu"))

            ip = node_name
            addresses = getattr(status, "addresses", None) or []
            for address in addresses:
                if getattr(address, "type", None) == "InternalIP":
                    ip = getattr(address, "address", node_name)
                    break

            ready = any(
                getattr(condition, "type", None) == "Ready"
                and getattr(condition, "status", None) == "True"
                for condition in (getattr(status, "conditions", None) or [])
            )
            labels = getattr(metadata, "labels", None) or {}
            node_type = labels.get("node-type", labels.get("node_type", "cloud"))
            display_name = labels.get("ats.local/display-name", node_name)

            resources.append(
                ResourceInfo(
                    node_id=node_name,
                    display_name=display_name,
                    node_type=node_type,
                    ip=ip,
                    cpu_total=cpu_total,
                    cpu_available=max(0.0, cpu_total - requested["cpu"]),
                    mem_total_mb=int(mem_total_bytes / (1024 * 1024)),
                    mem_available_mb=max(
                        0,
                        int(
                            (mem_total_bytes - requested["memory"])
                            / (1024 * 1024)
                        ),
                    ),
                    gpu_count=gpu_total,
                    gpu_available=max(0, int(gpu_total - requested["gpu"])),
                    status="online" if ready else "offline",
                    last_heartbeat=datetime.utcnow().isoformat(),
                )
            )

        return resources

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
        with self._lock:
            nodes = list(self._nodes.values())
        for node in nodes:
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
        with self._lock:
            return self._nodes.get(node_id)

    def get_all_nodes(self) -> List[ResourceInfo]:
        """获取所有节点"""
        with self._lock:
            return list(self._nodes.values())

    def sync_peer_resources(self, peer_url: str, resources: List[Dict]) -> int:
        """保存 peer 推送的资源快照；这些节点仅供展示，不参与本地调度。"""
        fields = set(ResourceInfo.__dataclass_fields__)
        peer_nodes: List[ResourceInfo] = []
        for item in resources:
            data = {key: value for key, value in item.items() if key in fields}
            if not data.get("node_id"):
                continue
            data["source_url"] = peer_url
            data["is_local"] = False
            try:
                peer_nodes.append(ResourceInfo(**data))
            except (TypeError, ValueError) as exc:
                logger.warning("[RRDC] 忽略无效 peer 资源: peer=%s error=%s", peer_url, exc)
        with self._lock:
            self._peer_nodes[peer_url] = peer_nodes
        return len(peer_nodes)

    def remove_peer_resources(self, peer_url: str) -> None:
        with self._lock:
            self._peer_nodes.pop(peer_url, None)

    def get_all_nodes_with_peers(self) -> List[ResourceInfo]:
        """返回本地和 peer 节点；同一来源内按 node_id 去重，本地节点优先。"""
        with self._lock:
            merged = {
                f"{peer_url}:{node.node_id}": node
                for peer_url, nodes in self._peer_nodes.items()
                for node in nodes
            }
            for node in self._nodes.values():
                merged[f"local:{node.node_id}"] = node
            return list(merged.values())

    # ------------------------------------------------------------------
    # 资源分配/释放
    # ------------------------------------------------------------------

    def allocate(
        self,
        node_id: str,
        cpu_cores: float,
        memory_mb: int,
        gpu_count: int = 0,
        reservation_id: Optional[str] = None,
    ) -> bool:
        """
        为 Agent 部署创建提交前的短期资源预留。

        部署请求提交给 Kubernetes 后必须调用 release() 撤销，
        实际资源占用由 Kubernetes Pod requests 唯一确定。

        Args:
            node_id:    目标节点 ID
            cpu_cores:  申请的 CPU 核心数
            memory_mb:  申请的内存 (MB)
            gpu_count:  申请的 GPU 数量
            reservation_id: 预留唯一标识

        Returns:
            True 分配成功，False 资源不足或节点不存在
        """
        # 预留前强制获取最新 Kubernetes 快照，避免使用选点阶段的旧数据。
        if not self.refresh_from_kubernetes():
            logger.warning("[RRDC] allocate 失败: Kubernetes 资源状态刷新失败")
            return False

        reservation_id = reservation_id or (
            f"{node_id}:{cpu_cores}:{memory_mb}:{gpu_count}"
        )
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                logger.warning(f"[RRDC] allocate 失败: node_id={node_id} 不存在")
                return False

            if reservation_id in self._reservations:
                logger.warning(
                    "[RRDC] allocate 失败: reservation_id=%s 已存在",
                    reservation_id,
                )
                return False

            if (
                node.cpu_available < cpu_cores
                or node.mem_available_mb < memory_mb
                or node.gpu_available < gpu_count
            ):
                logger.warning(
                    f"[RRDC] allocate 失败: 资源不足 "
                    f"(需cpu={cpu_cores},mem={memory_mb}MB,gpu={gpu_count}; "
                    f"可用cpu={node.cpu_available},mem={node.mem_available_mb}MB,"
                    f"gpu={node.gpu_available})"
                )
                return False

            self._reservations[reservation_id] = {
                "node_id": node_id,
                "cpu_cores": cpu_cores,
                "memory_mb": memory_mb,
                "gpu_count": gpu_count,
            }
            node.cpu_available -= cpu_cores
            node.mem_available_mb -= memory_mb
            node.gpu_available -= gpu_count
        logger.info(
            f"[RRDC] 预留资源: reservation={reservation_id}, node={node_id}, "
            f"cpu={cpu_cores}, mem={memory_mb}MB, gpu={gpu_count}"
        )
        return True

    def release(
        self,
        node_id: str,
        cpu_cores: float,
        memory_mb: int,
        gpu_count: int = 0,
        reservation_id: Optional[str] = None,
    ) -> bool:
        """
        撤销 Agent 部署提交前的短期资源预留。

        Args:
            node_id:    目标节点 ID
            cpu_cores:  归还的 CPU 核心数
            memory_mb:  归还的内存 (MB)
            gpu_count:  归还的 GPU 数量
            reservation_id: 预留唯一标识

        Returns:
            True 释放成功，False 节点不存在
        """
        reservation_id = reservation_id or (
            f"{node_id}:{cpu_cores}:{memory_mb}:{gpu_count}"
        )
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return False

            node = self._nodes.get(reservation["node_id"])
            if node is not None:
                node.cpu_available = min(
                    node.cpu_available + reservation["cpu_cores"], node.cpu_total
                )
                node.mem_available_mb = min(
                    node.mem_available_mb + reservation["memory_mb"],
                    node.mem_total_mb,
                )
                node.gpu_available = min(
                    node.gpu_available + reservation["gpu_count"], node.gpu_count
                )
        logger.info(
            f"[RRDC] 撤销资源预留: reservation={reservation_id}, node={node_id}"
        )
        return True

    # ------------------------------------------------------------------
    # 统计接口
    # ------------------------------------------------------------------

    def get_summary(self, include_peers: bool = False) -> Dict:
        """返回整体资源使用情况"""
        nodes = self.get_all_nodes_with_peers() if include_peers else self.get_all_nodes()
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
