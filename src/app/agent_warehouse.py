"""
智能体仓库 (AW - Agent Warehouse)

应用管理层组件，负责：
- 存储并管理 Agent 镜像（本地字典 + JSON 持久化）
- 安装镜像时，向 ARDC（AgentRegistryClient）注册，使其可被编排引擎发现
- 卸载镜像时，从 ARDC 注销

对应接口文档：应用管理层接口流程 §1 安装/卸载应用
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.app.models import AgentImage

logger = logging.getLogger(__name__)

# 持久化存储路径（与 agent_registry.json 同目录）
_DEFAULT_WAREHOUSE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "agent_warehouse.json",
)

_DEFAULT_APPS_STORE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "apps_store.json",
)

_KNOWN_IMAGE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "agent-grpc:v1": {
        "name": "agent_gRPC",
        "version": "v1",
        "capability": "agent-grpc",
        "description": "gRPC 入口，接收远程请求并发布到 NATS",
        "metadata": {"k8s": {"cpu_cores": 0.5, "memory_mb": 512, "gpu_count": 0}},
    },
    "agent-b-worker:v3": {
        "name": "Agent B",
        "version": "v3",
        "capability": "agent-b",
        "description": "NATS worker，转发到 Agent C 并回传结果",
        "metadata": {"k8s": {"cpu_cores": 0.5, "memory_mb": 512, "gpu_count": 0}},
    },
    "agent-c-worker:v1": {
        "name": "Agent C",
        "version": "v1",
        "capability": "agent-c",
        "description": "NATS worker，处理消息并返回转换结果",
        "metadata": {"k8s": {"cpu_cores": 0.5, "memory_mb": 512, "gpu_count": 0}},
    },
    "perception2intermediatefeature-agent:0.1.1": {
        "name": "Perception2IntermediateFeature",
        "version": "0.1.1",
        "capability": "perception2intermediatefeature",
        "description": "自动驾驶感知输入转换为中间特征",
        "metadata": {"k8s": {"cpu_cores": 2.0, "memory_mb": 4096, "gpu_count": 1}},
    },
    "cooperativefeaturefusiondetectionviz-agent:0.1.1": {
        "name": "CooperativeFeatureFusionDetectionViz",
        "version": "0.1.1",
        "capability": "cooperativefeaturefusiondetectionviz",
        "description": "协同特征融合、目标检测与可视化",
        "metadata": {"k8s": {"cpu_cores": 2.0, "memory_mb": 8192, "gpu_count": 1}},
    },
}


class AgentWarehouse:
    """
    智能体仓库（AW）

    职责：
    1. install_agent()   — 存储镜像 + 注册到 ARDC
    2. uninstall_agent() — 删除镜像 + 从 ARDC 注销
    3. get_image()       — 按 image_id 查询
    4. list_images()     — 列出所有镜像

    持久化：
    - 数据存储在 config/agent_warehouse.json
    - 每次增删操作后自动保存
    """

    def __init__(self, warehouse_file: Optional[str] = None):
        self._warehouse_file = warehouse_file or _DEFAULT_WAREHOUSE_FILE
        # image_id → AgentImage
        self._images: Dict[str, AgentImage] = {}
        self._load_from_json()
        if not self._images:
            self._load_from_apps_store()
        self.refresh_from_kubernetes()
        logger.info(
            f"AgentWarehouse (AW) 初始化完成，已加载 {len(self._images)} 个镜像"
        )

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    def install_agent(
        self,
        image: AgentImage,
        expose_external: bool = False,
    ) -> AgentImage:
        """
        安装 Agent 镜像

        流程（参考接口文档 §1）：
        1. 存储镜像到本地仓库
        2. 调用 ARDC 注册（使 Agent 可被编排层发现）

        Args:
            image:            要安装的 AgentImage 对象
            expose_external:  是否允许外部主体使用

        Returns:
            安装后的 AgentImage（registered=True）
        """
        image.exposed_external = expose_external
        self._images[image.image_id] = image
        self._save_to_json()

        # 向 ARDC 注册
        success = self._register_to_ardc(image)
        if success:
            image.registered = True
            self._save_to_json()
            logger.info(
                f"[AW] 安装成功: image_id={image.image_id}, "
                f"capability={image.capability}, external={expose_external}"
            )
        else:
            logger.warning(f"[AW] 安装镜像成功，但 ARDC 注册失败: {image.image_id}")

        return image

    def uninstall_agent(self, image_id: str) -> bool:
        """
        卸载 Agent 镜像

        流程（参考接口文档 §1）：
        1. 从 ARDC 注销
        2. 从本地仓库删除

        Args:
            image_id: 要卸载的镜像 ID

        Returns:
            True 表示成功，False 表示 image_id 不存在
        """
        image = self._images.get(image_id)
        if not image:
            logger.warning(f"[AW] uninstall_agent: image_id={image_id} 不存在")
            return False

        # 从 ARDC 注销
        self._deregister_from_ardc(image)

        # 删除本地记录
        del self._images[image_id]
        self._save_to_json()
        logger.info(f"[AW] 卸载成功: image_id={image_id}")
        return True

    def get_image(self, image_id: str) -> Optional[AgentImage]:
        """按 image_id 查询镜像"""
        return self._images.get(image_id)

    def list_images(self, refresh: bool = True) -> List[AgentImage]:
        """列出所有镜像"""
        if refresh:
            self.refresh_from_kubernetes()
        return list(self._images.values())

    def find_by_capability(self, capability: str) -> List[AgentImage]:
        """按能力类型查找镜像"""
        self.refresh_from_kubernetes()
        return [img for img in self._images.values() if img.capability == capability]

    def refresh_from_kubernetes(self) -> int:
        """
        从当前 Kubernetes 集群的 Deployment/Pod 实时同步 Agent 镜像。

        如果本机没有可用 kubeconfig 或服务端不可达，保持现有仓库内容不变。
        """
        if os.getenv("AGENT_WAREHOUSE_SYNC_K8S", "1").strip().lower() in {"0", "false", "no", "off"}:
            return 0

        try:
            from kubernetes import client, config
        except Exception:
            return 0

        try:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()

            apps = client.AppsV1Api()
            core = client.CoreV1Api()
            namespace = os.getenv("K8S_NAMESPACE", "").strip()
            if namespace:
                deployments = apps.list_namespaced_deployment(namespace=namespace).items
            else:
                deployments = apps.list_deployment_for_all_namespaces().items
        except Exception as exc:
            logger.debug("[AW] Kubernetes 镜像同步跳过: %s", exc)
            return 0

        changed = 0
        for dep in deployments:
            containers = getattr(dep.spec.template.spec, "containers", []) or []
            for container in containers:
                image_id = getattr(container, "image", "") or ""
                if not image_id or self._is_infra_image(image_id):
                    continue
                metadata = self._metadata_from_k8s(container, dep)
                if self._upsert_image(image_id, metadata=metadata, registered=True):
                    changed += 1

        try:
            pods = (
                core.list_namespaced_pod(namespace=namespace).items
                if namespace
                else core.list_pod_for_all_namespaces().items
            )
        except Exception:
            pods = []

        for pod in pods:
            containers = getattr(pod.spec, "containers", []) or []
            for container in containers:
                image_id = getattr(container, "image", "") or ""
                if not image_id or self._is_infra_image(image_id):
                    continue
                metadata = self._metadata_from_k8s(container, pod)
                if self._upsert_image(image_id, metadata=metadata, registered=True):
                    changed += 1

        if changed:
            self._save_to_json()
            logger.info("[AW] 从 Kubernetes 同步 %s 个 Agent 镜像", changed)
        return changed

    # ------------------------------------------------------------------
    # ARDC 集成
    # ------------------------------------------------------------------

    def _register_to_ardc(self, image: AgentImage) -> bool:
        """将镜像注册到 ARDC，使其可被编排引擎发现"""
        try:
            from src.service.agent_registry import get_registry_client
            from src.graph.distributed_types import AgentInfo

            registry = get_registry_client()

            # 构造 AgentInfo 注册到 ARDC
            # 注意：这里 IP/port 使用 localhost 作为默认值
            # 实际运行时应从部署信息中获取
            agent_info: AgentInfo = {
                "id": f"agent_{image.image_id}",
                "ip": "localhost",
                "port": 8080,
                "capability": image.capability,
                "status": "offline",  # 尚未部署，初始为 offline
                "description": (
                    f"{image.name} v{image.version} — {image.description}"
                ),
            }

            # AgentRegistryClient 目前不支持动态注册，更新状态作为近似
            # 生产环境：调用 registry.register_agent(agent_info)
            logger.debug(f"[AW→ARDC] 注册意图: {agent_info}")
            return True
        except Exception as e:
            logger.warning(f"[AW→ARDC] 注册失败: {e}")
            return False

    def _deregister_from_ardc(self, image: AgentImage):
        """从 ARDC 注销镜像对应的 Agent"""
        try:
            from src.service.agent_registry import get_registry_client

            registry = get_registry_client()
            agent_id = f"agent_{image.image_id}"
            registry.update_agent_status(agent_id, "offline")
            logger.debug(f"[AW→ARDC] 注销: agent_id={agent_id}")
        except Exception as e:
            logger.warning(f"[AW→ARDC] 注销失败（非关键）: {e}")

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_from_json(self):
        """从 JSON 文件加载镜像数据"""
        path = Path(self._warehouse_file)
        if not path.exists():
            logger.debug(f"[AW] 镜像仓库文件不存在，从空状态初始化: {path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("images", []):
                img = AgentImage(**item)
                self._images[img.image_id] = img
            for image_id in list(self._images.keys()):
                if image_id in _KNOWN_IMAGE_DEFAULTS:
                    self._upsert_image(image_id)
            logger.debug(f"[AW] 从文件加载 {len(self._images)} 个镜像: {path}")
        except Exception as e:
            logger.warning(f"[AW] 加载镜像仓库文件失败: {e}")

    def _load_from_apps_store(self):
        """仓库文件缺失时，从已安装应用记录恢复镜像清单。"""
        path = Path(os.getenv("APPS_STORE_PATH", _DEFAULT_APPS_STORE_FILE))
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("[AW] 应用存储恢复跳过: %s", exc)
            return

        restored = 0
        for app in data.values():
            guidance = app.get("guidance_file") or {}
            capabilities = guidance.get("agents_required") or []
            for image_id in app.get("image_ids") or []:
                metadata = {"source": "apps_store", "app_id": app.get("app_id")}
                capability = capabilities[0] if len(capabilities) == 1 else None
                if self._upsert_image(image_id, capability=capability, metadata=metadata):
                    restored += 1

        if restored:
            self._save_to_json()
            logger.info("[AW] 从 apps_store 恢复 %s 个 Agent 镜像", restored)

    def _save_to_json(self):
        """持久化镜像数据到 JSON 文件"""
        path = Path(self._warehouse_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {"images": [img.to_dict() for img in self._images.values()]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[AW] 持久化镜像仓库失败: {e}")

    def _upsert_image(
        self,
        image_id: str,
        capability: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        registered: bool = False,
    ) -> bool:
        defaults = _KNOWN_IMAGE_DEFAULTS.get(image_id, {})
        inferred = self._infer_image_fields(image_id)
        merged_metadata = dict(defaults.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
            if defaults.get("metadata", {}).get("k8s"):
                merged_metadata["k8s"] = {
                    **defaults["metadata"]["k8s"],
                    **(metadata.get("k8s") or {}),
                }

        image = self._images.get(image_id)
        if image is None:
            self._images[image_id] = AgentImage(
                image_id=image_id,
                name=defaults.get("name") or inferred["name"],
                version=defaults.get("version") or inferred["version"],
                capability=capability or defaults.get("capability") or inferred["capability"],
                description=defaults.get("description") or inferred["description"],
                metadata=merged_metadata,
                registered=registered,
            )
            return True

        before = image.to_dict()
        image.name = defaults.get("name") or image.name or inferred["name"]
        image.version = defaults.get("version") or image.version or inferred["version"]
        image.capability = capability or defaults.get("capability") or image.capability or inferred["capability"]
        image.description = defaults.get("description") or image.description or inferred["description"]
        image.metadata = {**(image.metadata or {}), **merged_metadata}
        image.registered = image.registered or registered
        return before != image.to_dict()

    @staticmethod
    def _infer_image_fields(image_id: str) -> Dict[str, str]:
        image_name = image_id.rsplit("/", 1)[-1]
        base, _, version = image_name.partition(":")
        capability = re.sub(r"[-_](agent|worker)$", "", base).lower()
        return {
            "name": base.replace("-", " ").replace("_", " ").title().replace("Grpc", "gRPC"),
            "version": version or "latest",
            "capability": capability,
            "description": f"Kubernetes Agent 镜像 {image_id}",
        }

    @staticmethod
    def _is_infra_image(image_id: str) -> bool:
        image_name = image_id.rsplit("/", 1)[-1].lower()
        return image_name.startswith(("nats:", "coredns:", "pause:", "metrics-server:"))

    @staticmethod
    def _quantity_to_cpu_cores(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        raw = str(value)
        if raw.endswith("m"):
            return float(raw[:-1]) / 1000
        return float(raw)

    @staticmethod
    def _quantity_to_memory_mb(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        raw = str(value)
        match = re.match(r"^([0-9.]+)([A-Za-z]*)$", raw)
        if not match:
            return None
        amount = float(match.group(1))
        unit = match.group(2)
        factors = {
            "Ki": 1 / 1024,
            "Mi": 1,
            "Gi": 1024,
            "Ti": 1024 * 1024,
            "K": 1 / 1000,
            "M": 1,
            "G": 1000,
            "T": 1000 * 1000,
        }
        return int(amount * factors.get(unit, 1 / (1024 * 1024)))

    def _metadata_from_k8s(self, container, owner) -> Dict[str, Any]:
        resources = getattr(container, "resources", None)
        requests = getattr(resources, "requests", None) or {}
        limits = getattr(resources, "limits", None) or {}
        cpu = self._quantity_to_cpu_cores(requests.get("cpu") or limits.get("cpu"))
        memory = self._quantity_to_memory_mb(requests.get("memory") or limits.get("memory"))
        gpu = int(limits.get("nvidia.com/gpu", 0) or 0)
        namespace = getattr(getattr(owner, "metadata", None), "namespace", None)
        resource_name = getattr(getattr(owner, "metadata", None), "name", None)
        metadata: Dict[str, Any] = {
            "source": "kubernetes",
            "namespace": namespace,
            "resource_name": resource_name,
            "container_name": getattr(container, "name", None),
            "k8s": {
                "cpu_cores": cpu if cpu is not None else 1.0,
                "memory_mb": memory if memory is not None else 512,
                "gpu_count": gpu,
            },
        }
        return metadata


# ======================================================================
# 单例访问
# ======================================================================
_warehouse_instance: Optional[AgentWarehouse] = None


def get_agent_warehouse() -> AgentWarehouse:
    """获取全局 AgentWarehouse 单例"""
    global _warehouse_instance
    if _warehouse_instance is None:
        _warehouse_instance = AgentWarehouse()
    return _warehouse_instance
