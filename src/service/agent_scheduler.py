"""
智能体调度与部署 (ASD - Agent Scheduling & Deployment)

编排层组件，负责：
- 接收编排引擎的部署/关闭指令
- 通过 subprocess 在本机启动/关闭 agent_server.py 进程
- 与资源注册与发现中心（RRDC）协作完成资源分配
- 将运行实例注册/注销到 ARDC（AgentRegistryClient）

本机部署：使用 subprocess 启动 agent_server.py，支持动态端口分配。
跨节点部署：subprocess 后端下 node_id 非 localhost 时降级 mock；kubernetes 后端下
由 Deployment nodeSelector 交给 K8s 调度。
"""
import logging
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional
from datetime import datetime

from src.service.agent_startup import AgentStartupConfig

logger = logging.getLogger(__name__)


class DeploymentRecord:
    """部署记录：一次部署操作的完整信息"""

    def __init__(
        self,
        deployment_id: str,
        agent_id: str,
        image_id: str,
        node_id: str,
        cpu_cores: float,
        memory_mb: int,
        gpu_count: int = 0,
        replicas: int = 1,
        backend: str = "subprocess",
        namespace: Optional[str] = None,
        k8s_deployment_name: Optional[str] = None,
        k8s_pod_name: Optional[str] = None,
        k8s_pod_uid: Optional[str] = None,
        status: str = "deploying",
        error_message: Optional[str] = None,
    ):
        """初始化一条部署记录，记录调度输入、运行状态和 Kubernetes 资源名。"""
        self.deployment_id = deployment_id
        self.agent_id = agent_id
        self.image_id = image_id
        self.node_id = node_id
        self.cpu_cores = cpu_cores
        self.memory_mb = memory_mb
        self.gpu_count = gpu_count
        self.replicas = replicas
        self.backend = backend
        self.namespace = namespace
        self.k8s_deployment_name = k8s_deployment_name
        self.k8s_pod_name = k8s_pod_name
        self.k8s_pod_uid = k8s_pod_uid
        self.status = status  # deploying | running | stopping | stopped | failed
        self.error_message = error_message
        self.created_at = datetime.utcnow().isoformat()
        self.updated_at = self.created_at

    def to_dict(self) -> Dict:
        """把部署记录转换成 API 可直接返回的字典结构。"""
        return {
            "deployment_id": self.deployment_id,
            "agent_id": self.agent_id,
            "image_id": self.image_id,
            "node_id": self.node_id,
            "cpu_cores": self.cpu_cores,
            "memory_mb": self.memory_mb,
            "gpu_count": self.gpu_count,
            "replicas": self.replicas,
            "backend": self.backend,
            "namespace": self.namespace,
            "k8s_deployment_name": self.k8s_deployment_name,
            "k8s_pod_name": self.k8s_pod_name,
            "k8s_pod_uid": self.k8s_pod_uid,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AgentScheduler:
    """
    智能体调度与部署（ASD）

    职责：
    1. deploy_agent()   — 部署 Agent 实例（分配资源 → 拉镜像 → 启动 → 注册 ARDC）
    2. shutdown_agent() — 关闭 Agent 实例（退订通知 → 停止容器 → 注销 ARDC → 释放资源）
    3. health_check()   — 单个 Agent 健康检查
    4. list_running()   — 列出当前所有运行中的 Agent 部署记录
    """

    # 项目根目录（agent_server.py 所在位置）
    _PROJECT_ROOT = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    _K8S_MANIFEST_DIR = Path(_PROJECT_ROOT) / "k8s"

    def __init__(self):
        """初始化调度器状态，并从 server 启动环境读取 Agent/NATS 启动配置。"""
        # 部署记录字典: deployment_id → DeploymentRecord
        self._deployments: Dict[str, DeploymentRecord] = {}
        # agent_id → deployment_id 的反向索引（一个 agent_id 可能对应多个部署）
        self._agent_index: Dict[str, List[str]] = {}
        # subprocess 进程句柄: agent_id → Popen
        self._processes: Dict[str, subprocess.Popen] = {}
        # agent_id → 分配的端口
        self._agent_ports: Dict[str, int] = {}
        self.startup_config = AgentStartupConfig.from_env()
        self.deploy_backend = self.startup_config.deploy_backend
        self.k8s_namespace = self.startup_config.k8s_namespace
        nats_service = os.getenv("NATS_SERVICE_NAME", "nats")
        nats_port = int(os.getenv("NATS_CLIENT_PORT", "4222"))
        self.nats_servers = os.getenv("NATS_SERVERS", f"nats://127.0.0.1:{nats_port}")
        self.agent_nats_servers = os.getenv(
            "AGENT_NATS_SERVERS",
            f"nats://{nats_service}:{nats_port}"
            if self.deploy_backend == "kubernetes"
            else self.nats_servers,
        )
        self.nats_jetstream_domain = os.getenv("NATS_JETSTREAM_DOMAIN", "hub")
        self.nats_stream_subjects = os.getenv("NATS_STREAM_SUBJECTS", "workflow.>")
        logger.info(
            "AgentScheduler (ASD) 初始化完成: backend=%s, nats_servers=%s, agent_nats_servers=%s",
            self.deploy_backend,
            self.nats_servers,
            self.agent_nats_servers,
        )

    # ------------------------------------------------------------------
    # 私有工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _allocate_port(host: str = "127.0.0.1") -> int:
        """从操作系统申请一个随机空闲端口"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]

    @staticmethod
    def _wait_for_ready(port: int, host: str = "127.0.0.1", timeout: float = 8.0) -> bool:
        """轮询 /health 直到 agent_server 就绪或超时"""
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/health", timeout=0.5
                ) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    @staticmethod
    def _capability_from_image(image_id: str) -> str:
        """
        从 image_id 推断 capability。
        规则：取冒号前的部分，去掉 '_agent' 后缀。
        例：'search_agent:v1' → 'search'，'custom_cap' → 'custom_cap'
        """
        base = image_id.split(":")[0]          # e.g. "search_agent"
        if base.endswith("_agent"):
            base = base[: -len("_agent")]       # e.g. "search"
        return base

    @staticmethod
    def _safe_k8s_name(value: str) -> str:
        """把任意 agent_id 转成合法 Kubernetes 资源名。"""
        name = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
        if not name:
            name = f"agent-{uuid.uuid4().hex[:8]}"
        return name[:63].rstrip("-")

    @staticmethod
    def _cpu_quantity(cpu_cores: float) -> str:
        """把浮点 CPU 核数转换成 Kubernetes CPU quantity。"""
        if cpu_cores < 1:
            return f"{int(cpu_cores * 1000)}m"
        if float(cpu_cores).is_integer():
            return str(int(cpu_cores))
        return str(cpu_cores)

    @staticmethod
    def _resource_requirements(cpu_cores: float, memory_mb: int, gpu_count: int = 0) -> Dict:
        """根据资源配置生成 Kubernetes container.resources。"""
        resources = {
            "requests": {
                "cpu": AgentScheduler._cpu_quantity(cpu_cores),
                "memory": f"{memory_mb}Mi",
            },
            "limits": {
                "cpu": AgentScheduler._cpu_quantity(cpu_cores),
                "memory": f"{memory_mb}Mi",
            },
        }
        if gpu_count > 0:
            resources["limits"]["nvidia.com/gpu"] = str(gpu_count)
        return resources

    @staticmethod
    def _load_kube_config() -> None:
        """加载 Kubernetes 配置，优先使用集群内配置，失败后回退本机 kubeconfig。"""
        try:
            from kubernetes import config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
        except ImportError as exc:
            raise RuntimeError(
                "Kubernetes backend requires the 'kubernetes' Python package. "
                "Install it with: pip install kubernetes"
            ) from exc

    @staticmethod
    def _upsert_env_var(env_list: List[Dict], name: str, value: str) -> None:
        """在容器 env 列表中按 name 覆盖或追加变量。"""
        for item in env_list:
            if item.get("name") == name:
                item["value"] = value
                return
        env_list.append({"name": name, "value": value})

    def _upsert_agent_nats_env(self, env_list: List[Dict]) -> None:
        """注入 Agent 连接本集群 NATS 所需的环境变量。"""
        self._upsert_env_var(env_list, "NATS_SERVERS", self.agent_nats_servers)
        self._upsert_env_var(env_list, "NATS_SERVER_URL", self.agent_nats_servers)
        self._upsert_env_var(env_list, "NATS_JETSTREAM_DOMAIN", self.nats_jetstream_domain)
        self._upsert_env_var(env_list, "NATS_STREAM_SUBJECTS", self.nats_stream_subjects)

    def _kubernetes_manifest_path(self, deployment_name: str) -> Path:
        """返回 k8s/ 根目录下对应 Deployment 名称的 YAML 路径。"""
        return self._K8S_MANIFEST_DIR / f"{deployment_name}.yaml"

    @staticmethod
    def _load_yaml_documents(manifest_path: Path) -> List[Dict]:
        """读取多文档 YAML；失败时返回空列表并记录原因。"""
        try:
            import yaml
        except ImportError:
            logger.warning("[ASD] PyYAML 未安装，跳过 YAML 读取: %s", manifest_path)
            return []

        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                return [doc for doc in yaml.safe_load_all(handle) if doc]
        except Exception as exc:
            logger.warning("[ASD] 读取 Kubernetes YAML 失败: %s, error=%s", manifest_path, exc)
            return []

    def _load_kubernetes_manifest_from_yaml(self, deployment_name: str) -> Optional[Dict[str, Dict]]:
        """优先从 k8s/ 根目录读取 Deployment/Service YAML；未找到或读取失败时返回 None。"""
        manifest_path = self._kubernetes_manifest_path(deployment_name)
        if not manifest_path.exists():
            return None

        docs = self._load_yaml_documents(manifest_path)
        if not docs:
            return None

        manifests: Dict[str, Dict] = {}
        for doc in docs:
            kind = str(doc.get("kind", "")).lower()
            if kind == "deployment":
                manifests["deployment"] = doc
            elif kind == "service":
                manifests["service"] = doc

        if "deployment" not in manifests:
            logger.warning("[ASD] YAML 中未找到 Deployment: %s", manifest_path)
            return None

        logger.info("[ASD] 使用 YAML 作为 Kubernetes Manifest: %s", manifest_path)
        return manifests

    @staticmethod
    def _extract_port_config(
        deployment_body: Optional[Dict],
        service_body: Optional[Dict],
        default_container_port: int,
        default_service_port: int,
        default_service_type: str,
        default_service_port_name: str,
    ) -> Dict[str, object]:
        """从 manifest 中提取端口配置；缺失时回退到默认值。"""
        container_port = default_container_port
        service_port = default_service_port
        service_type = default_service_type
        service_port_name = default_service_port_name

        try:
            if deployment_body:
                containers = deployment_body["spec"]["template"]["spec"]["containers"]
                if containers and containers[0].get("ports"):
                    first_port = containers[0]["ports"][0]
                    container_port = int(first_port.get("containerPort", container_port))
                    service_port_name = first_port.get("name", service_port_name)
        except Exception:
            pass

        try:
            if service_body:
                service_spec = service_body["spec"]
                ports = service_spec.get("ports", [])
                if ports:
                    first_port = ports[0]
                    service_port = int(first_port.get("port", service_port))
                    service_port_name = first_port.get("name", service_port_name)
                service_type = service_spec.get("type", service_type)
        except Exception:
            pass

        return {
            "container_port": container_port,
            "service_port": service_port,
            "service_type": service_type,
            "service_port_name": service_port_name,
        }

    def _apply_runtime_overrides_to_deployment(
        self,
        deployment_body: Dict,
        record: DeploymentRecord,
        capability: str,
        deployment_name: str,
        labels: Dict[str, str],
        container_port: int,
        service_port_name: str,
        node_selector: Dict[str, str],
    ) -> Dict:
        """把运行时字段叠加到 YAML 或字典生成的 Deployment 上。"""
        metadata = deployment_body.setdefault("metadata", {})
        metadata["name"] = deployment_name
        metadata["labels"] = labels

        spec = deployment_body.setdefault("spec", {})
        spec["replicas"] = record.replicas
        spec.setdefault("selector", {})["matchLabels"] = labels

        template = spec.setdefault("template", {})
        template.setdefault("metadata", {})["labels"] = labels
        pod_spec = template.setdefault("spec", {})
        if node_selector:
            pod_spec["nodeSelector"] = node_selector

        containers = pod_spec.setdefault("containers", [])
        if not containers:
            containers.append({})
        container = containers[0]
        container.setdefault("name", self.startup_config.agent_container_name)
        container.setdefault("image", record.image_id)
        container["imagePullPolicy"] = self.startup_config.image_pull_policy

        ports = container.setdefault("ports", [])
        if not ports:
            ports.append({"containerPort": container_port, "name": service_port_name})
        else:
            ports[0].setdefault("containerPort", container_port)
            ports[0].setdefault("name", service_port_name)

        env = container.setdefault("env", [])
        # self._upsert_env_var(env, "AGENT_ID", record.agent_id)
        # self._upsert_env_var(env, "AGENT_CAPABILITY", capability)
        # self._upsert_agent_nats_env(env)

        container["resources"] = self._resource_requirements(
            record.cpu_cores,
            record.memory_mb,
            record.gpu_count,
        )
        container.update(self.startup_config.health_probes(container_port))
        return deployment_body

    def _apply_runtime_overrides_to_service(
        self,
        service_body: Dict,
        deployment_name: str,
        labels: Dict[str, str],
        service_port: int,
        container_port: int,
        service_type: str,
        service_port_name: str,
        is_grpc_entry: bool,
    ) -> Dict:
        """把运行时字段叠加到 YAML 或字典生成的 Service 上。"""
        metadata = service_body.setdefault("metadata", {})
        metadata["name"] = deployment_name
        metadata["labels"] = labels

        spec = service_body.setdefault("spec", {})
        spec["selector"] = labels
        spec["type"] = service_type

        ports = spec.setdefault("ports", [])
        if not ports:
            ports.append({"name": service_port_name, "port": service_port, "targetPort": container_port})
        else:
            ports[0]["name"] = service_port_name
            ports[0]["port"] = service_port
            ports[0]["targetPort"] = container_port
        if is_grpc_entry and self.startup_config.grpc_node_port:
            ports[0]["nodePort"] = self.startup_config.grpc_node_port
        return service_body

    def _build_kubernetes_manifests(
        self,
        record: DeploymentRecord,
        capability: str,
        deployment_name: str,
        labels: Dict[str, str],
        container_port: int,
        service_port: int,
        service_type: str,
        service_port_name: str,
        is_grpc_entry: bool,
        node_selector: Dict[str, str],
    ) -> Dict[str, Dict]:
        """在没有 YAML 时，回退到原有的字典构造方式。"""
        deployment_body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": deployment_name, "labels": labels},
            "spec": {
                "replicas": record.replicas,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": self.startup_config.agent_container_name,
                                "image": record.image_id,
                                "imagePullPolicy": self.startup_config.image_pull_policy,
                                "ports": [{"containerPort": container_port}],
                                "env": [
                                    {"name": "AGENT_ID", "value": record.agent_id},
                                    {"name": "AGENT_CAPABILITY", "value": capability},
                                ],
                                "resources": self._resource_requirements(
                                    record.cpu_cores,
                                    record.memory_mb,
                                    record.gpu_count,
                                ),
                            }
                        ],
                    },
                },
            },
        }
        deployment_body["spec"]["template"]["spec"].update(
            self.startup_config.health_probes(container_port)
        )
        if node_selector:
            deployment_body["spec"]["template"]["spec"]["nodeSelector"] = node_selector
        self._upsert_agent_nats_env(
            deployment_body["spec"]["template"]["spec"]["containers"][0]["env"]
        )

        service_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": deployment_name, "labels": labels},
            "spec": {
                "selector": labels,
                "ports": [{"name": service_port_name, "port": service_port, "targetPort": container_port}],
                "type": service_type,
            },
        }
        if is_grpc_entry and self.startup_config.grpc_node_port:
            service_body["spec"]["ports"][0]["nodePort"] = self.startup_config.grpc_node_port

        return {"deployment": deployment_body, "service": service_body}

    def _node_selector(self, node_id: str) -> Dict[str, str]:
        """根据 node_id 生成 Kubernetes nodeSelector；本机目标不限制调度节点。"""
        if self.startup_config.is_local_node(node_id):
            return {}
        if "=" in node_id:
            key, value = node_id.split("=", 1)
            return {key: value}

        try:
            from kubernetes import client

            core = client.CoreV1Api()
            node_names = {node.metadata.name for node in core.list_node().items}
            if node_id in node_names:
                return {"kubernetes.io/hostname": node_id}
            logger.warning(
                "[ASD] node_id=%s 不是当前 Kubernetes 节点名，忽略 nodeSelector；"
                "如需强制调度请使用 key=value 形式",
                node_id,
            )
            return {}
        except Exception as exc:
            logger.warning("[ASD] 查询 Kubernetes 节点失败，跳过 nodeSelector: node_id=%s, error=%s", node_id, exc)
            return {}

    def _kubernetes_service_registration_endpoint(
        self,
        core,
        namespace: str,
        service_name: str,
        node_id: str,
    ) -> tuple[str, int]:
        """读取实际 Service，解析写入 ARDC 的访问地址。"""
        service = core.read_namespaced_service(
            name=service_name,
            namespace=namespace,
        )
        service_ports = getattr(getattr(service, "spec", None), "ports", None) or []
        if not service_ports:
            raise RuntimeError(f"Kubernetes Service {service_name} 未配置端口")

        service_port = service_ports[0]
        service_type = getattr(service.spec, "type", None) or "ClusterIP"
        if service_type == "NodePort":
            register_port = getattr(service_port, "node_port", None)
            if not register_port:
                raise RuntimeError(
                    f"Kubernetes NodePort Service {service_name} 未分配 nodePort"
                )

            from src.service.resource_registry import get_resource_registry

            node = get_resource_registry().get_node(node_id)
            if not node or not node.ip:
                raise RuntimeError(f"无法解析 Kubernetes 节点 {node_id} 的访问 IP")
            register_ip = node.ip
        else:
            register_ip = getattr(service.spec, "cluster_ip", None)
            register_port = getattr(service_port, "port", None)
            if not register_ip or not register_port:
                raise RuntimeError(
                    f"无法解析 Kubernetes Service {service_name} 的 ClusterIP 地址"
                )

        return str(register_ip), int(register_port)

    @staticmethod
    def _pod_failure_reason(pod) -> Optional[str]:
        """返回 Pod 已确定无法就绪的原因；尚在正常启动时返回 None。"""
        status = getattr(pod, "status", None)
        pod_name = getattr(getattr(pod, "metadata", None), "name", "unknown")
        if getattr(status, "phase", None) == "Failed":
            reason = getattr(status, "reason", None) or "PodFailed"
            message = getattr(status, "message", None) or ""
            detail = f"pod={pod_name}, reason={reason}"
            return f"{detail}, message={message}" if message else detail

        for condition in getattr(status, "conditions", None) or []:
            if (
                getattr(condition, "type", None) == "PodScheduled"
                and getattr(condition, "status", None) == "False"
                and getattr(condition, "reason", None) == "Unschedulable"
            ):
                return (
                    f"pod={pod_name}, reason=Unschedulable, "
                    f"message={getattr(condition, 'message', '')}"
                )

        image_failure_reasons = {
            "ErrImagePull",
            "ImagePullBackOff",
            "InvalidImageName",
            "RegistryUnavailable",
        }
        statuses = (getattr(status, "init_container_statuses", None) or []) + (
            getattr(status, "container_statuses", None) or []
        )
        for container_status in statuses:
            waiting = getattr(getattr(container_status, "state", None), "waiting", None)
            reason = getattr(waiting, "reason", None)
            if reason in image_failure_reasons:
                return (
                    f"pod={pod_name}, container={getattr(container_status, 'name', 'unknown')}, "
                    f"reason={reason}, message={getattr(waiting, 'message', '')}"
                )
        return None

    @staticmethod
    def _pod_is_running_ready(pod) -> bool:
        """判断 Pod 是否同时处于 Running 且 Ready。"""
        status = getattr(pod, "status", None)
        if getattr(status, "phase", None) != "Running":
            return False
        return any(
            getattr(condition, "type", None) == "Ready"
            and getattr(condition, "status", None) == "True"
            for condition in (getattr(status, "conditions", None) or [])
        )

    @staticmethod
    def _format_pod_diagnostics(
        core,
        namespace: str,
        pods: List,
        deployment_name: Optional[str] = None,
    ) -> str:
        """汇总 Pod conditions 和 Kubernetes Events，用于超时错误返回。"""
        diagnostics: List[str] = []
        for pod in pods:
            metadata = getattr(pod, "metadata", None)
            status = getattr(pod, "status", None)
            pod_name = getattr(metadata, "name", "unknown")
            conditions = []
            for condition in getattr(status, "conditions", None) or []:
                conditions.append(
                    f"{getattr(condition, 'type', 'Unknown')}="
                    f"{getattr(condition, 'status', 'Unknown')}"
                    f"({getattr(condition, 'reason', '')}:"
                    f"{getattr(condition, 'message', '')})"
                )
            diagnostics.append(
                f"pod={pod_name}, phase={getattr(status, 'phase', 'Unknown')}, "
                f"conditions=[{' | '.join(conditions)}]"
            )
            try:
                events = core.list_namespaced_event(
                    namespace=namespace,
                    field_selector=f"involvedObject.kind=Pod,involvedObject.name={pod_name}",
                ).items
                for event in events[-10:]:
                    diagnostics.append(
                        f"event[{getattr(event, 'type', 'Unknown')}/"
                        f"{getattr(event, 'reason', 'Unknown')}]: "
                        f"{getattr(event, 'message', '')}"
                    )
            except Exception as exc:
                diagnostics.append(f"pod={pod_name}, events_query_error={exc}")
        if not pods and deployment_name:
            try:
                events = core.list_namespaced_event(
                    namespace=namespace,
                    field_selector=(
                        "involvedObject.kind=Deployment,"
                        f"involvedObject.name={deployment_name}"
                    ),
                ).items
                for event in events[-10:]:
                    diagnostics.append(
                        f"deployment_event[{getattr(event, 'type', 'Unknown')}/"
                        f"{getattr(event, 'reason', 'Unknown')}]: "
                        f"{getattr(event, 'message', '')}"
                    )
            except Exception as exc:
                diagnostics.append(f"deployment_events_query_error={exc}")
        return "; ".join(diagnostics) if diagnostics else "no Pod created and no event found"

    def _wait_for_kubernetes_ready(
        self,
        core,
        namespace: str,
        deployment_name: str,
        replicas: int,
    ) -> tuple[bool, str]:
        """等待 Deployment 的期望 Pod 全部 Running/Ready。"""
        timeout = max(1.0, float(os.getenv("K8S_DEPLOY_READY_TIMEOUT", "120")))
        poll_interval = max(0.2, float(os.getenv("K8S_DEPLOY_POLL_INTERVAL", "2")))
        deadline = time.monotonic() + timeout
        last_pods: List = []
        label_selector = f"app={deployment_name}"

        while time.monotonic() < deadline:
            try:
                last_pods = core.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=label_selector,
                ).items
            except Exception as exc:
                return False, f"Kubernetes Pod 状态查询失败: {exc}"

            for pod in last_pods:
                failure = self._pod_failure_reason(pod)
                if failure:
                    diagnostics = self._format_pod_diagnostics(core, namespace, [pod])
                    return False, f"Kubernetes Pod 启动失败: {failure}; {diagnostics}"

            active_pods = [
                pod
                for pod in last_pods
                if getattr(getattr(pod, "metadata", None), "deletion_timestamp", None)
                is None
            ]
            ready_count = sum(self._pod_is_running_ready(pod) for pod in active_pods)
            if ready_count >= replicas:
                return True, ""
            time.sleep(poll_interval)

        diagnostics = self._format_pod_diagnostics(
            core,
            namespace,
            last_pods,
            deployment_name=deployment_name,
        )
        return (
            False,
            f"Kubernetes Pod 就绪超时({timeout:g}s): "
            f"deployment={deployment_name}; {diagnostics}",
        )

    def _deploy_kubernetes(
        self,
        record: DeploymentRecord,
        capability: str,
        on_submitted: Optional[Callable[[], None]] = None,
    ) -> DeploymentRecord:
        """提交 Kubernetes Deployment/Service，然后等待 Pod 就绪并注册 ARDC。"""
        self._load_kube_config()
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        apps = client.AppsV1Api()
        core = client.CoreV1Api()

        namespace = record.namespace or self.k8s_namespace

        deployment_name = record.k8s_deployment_name or self._safe_k8s_name(record.agent_id)
        record.namespace = namespace
        record.k8s_deployment_name = deployment_name
        labels = {"app": deployment_name, "agent-id": deployment_name}
        is_grpc_entry = self.startup_config.is_grpc_entry(
            record.agent_id,
            record.image_id,
            capability,
        )
        node_selector = self._node_selector(record.node_id)
        manifest_bodies = self._load_kubernetes_manifest_from_yaml(deployment_name)
        if manifest_bodies:
            port_config = self._extract_port_config(
                manifest_bodies.get("deployment"),
                manifest_bodies.get("service"),
                *self.startup_config.k8s_ports(is_grpc_entry),
            )
            container_port = port_config["container_port"]
            service_port = port_config["service_port"]
            service_type = port_config["service_type"]
            service_port_name = port_config["service_port_name"]
            deployment_body = self._apply_runtime_overrides_to_deployment(
                manifest_bodies["deployment"],
                record,
                capability,
                deployment_name,
                labels,
                container_port,
                service_port_name,
                node_selector,
            )
            service_body = manifest_bodies.get("service") or {}
            service_body = self._apply_runtime_overrides_to_service(
                service_body,
                deployment_name,
                labels,
                service_port,
                container_port,
                service_type,
                service_port_name,
                is_grpc_entry,
            )
        else:
            container_port, service_port, service_type, service_port_name = self.startup_config.k8s_ports(is_grpc_entry)
            built = self._build_kubernetes_manifests(
                record=record,
                capability=capability,
                deployment_name=deployment_name,
                labels=labels,
                container_port=container_port,
                service_port=service_port,
                service_type=service_type,
                service_port_name=service_port_name,
                is_grpc_entry=is_grpc_entry,
                node_selector=node_selector,
            )
            deployment_body = built["deployment"]
            service_body = built["service"]

        try:
            apps.create_namespaced_deployment(namespace=namespace, body=deployment_body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            apps.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment_body)

        # Deployment 已被 Kubernetes API 接受，Pod requests 开始由 K8s 资源视图接管。
        # 在等待 Pod Ready 之前撤销 RRDC 的提交前短期预留，避免重复扣减。
        if on_submitted is not None:
            try:
                on_submitted()
            except Exception as exc:
                logger.warning("[ASD] Deployment 提交回调执行失败: %s", exc)

        try:
            core.create_namespaced_service(namespace=namespace, body=service_body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            core.patch_namespaced_service(name=deployment_name, namespace=namespace, body=service_body)

        ready, error_message = self._wait_for_kubernetes_ready(
            core,
            namespace,
            deployment_name,
            record.replicas,
        )
        if not ready:
            record.status = "failed"
            record.error_message = error_message
            record.updated_at = datetime.utcnow().isoformat()
            logger.error("[ASD] Kubernetes 部署未就绪: %s", error_message)
            return record

        # ALCM 使用 Kubernetes 为实际运行 Pod 分配的 UID 作为实例 ID。
        # UID 在 Pod 生命周期内稳定，并且即使 Pod 名称被复用也不会冲突。
        pods = core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={deployment_name}",
        ).items
        ready_pods = [
            pod for pod in pods
            if getattr(getattr(pod, "metadata", None), "deletion_timestamp", None) is None
            and self._pod_is_running_ready(pod)
        ]
        if not ready_pods:
            record.status = "failed"
            record.error_message = "Kubernetes Pod 已就绪但无法读取实例元数据"
            record.updated_at = datetime.utcnow().isoformat()
            return record
        pod = sorted(
            ready_pods,
            key=lambda item: getattr(getattr(item, "metadata", None), "name", ""),
        )[0]
        record.k8s_pod_name = getattr(pod.metadata, "name", None)
        record.k8s_pod_uid = getattr(pod.metadata, "uid", None)
        if not record.k8s_pod_uid:
            record.status = "failed"
            record.error_message = "Kubernetes Pod 元数据中缺少 UID"
            record.updated_at = datetime.utcnow().isoformat()
            return record

        record.status = "running"
        record.error_message = None
        record.updated_at = datetime.utcnow().isoformat()
        register_ip, register_port = self._kubernetes_service_registration_endpoint(
            core=core,
            namespace=namespace,
            service_name=deployment_name,
            node_id=record.node_id,
        )
        self._register_to_ardc(
            agent_id=record.agent_id,
            node_id=record.node_id,
            ip=register_ip,
            port=register_port,
            capability=capability,
        )
        return record

    # ------------------------------------------------------------------
    # 核心部署接口
    # ------------------------------------------------------------------

    def deploy_agent(
        self,
        image_id: str,
        agent_id: Optional[str] = None,
        node_id: str = "localhost",
        cpu_cores: float = 1.0,
        memory_mb: int = 512,
        gpu_count: int = 0,
        replicas: int = 1,
        on_kubernetes_submitted: Optional[Callable[[], None]] = None,
    ) -> DeploymentRecord:
        """
        部署一个 Agent 实例

        流程：
        1. 向 RRDC 申请资源（当前 mock：直接记录）
        2. 拉取镜像（当前 mock：记录 image_id）
        3. 启动容器（当前 mock：打印部署意图）
        4. 注册到 ARDC（通过 AgentRegistryClient）

        Args:
            image_id:   镜像 ID（如 "search_agent:v1.0"）
            agent_id:   Agent 逻辑 ID；为 None 时自动生成
            node_id:    目标节点 ID
            cpu_cores:  CPU 核心数分配
            memory_mb:  内存分配（MB）
            gpu_count:  GPU 数量
            replicas:   Kubernetes 后端副本数
            on_kubernetes_submitted: Deployment 被 K8s API 接受后、等待 Ready 前的回调

        Returns:
            DeploymentRecord
        """
        if agent_id is None:
            agent_id = f"agent_{image_id.replace(':', '_')}_{uuid.uuid4().hex[:6]}"

        deployment_id = f"dep_{uuid.uuid4().hex[:8]}"
        record = DeploymentRecord(
            deployment_id=deployment_id,
            agent_id=agent_id,
            image_id=image_id,
            node_id=node_id,
            cpu_cores=cpu_cores,
            memory_mb=memory_mb,
            gpu_count=gpu_count,
            replicas=replicas,
            backend=self.deploy_backend,
            namespace=self.k8s_namespace if self.deploy_backend == "kubernetes" else None,
            status="deploying",
        )

        # 记录部署
        self._deployments[deployment_id] = record
        self._agent_index.setdefault(agent_id, []).append(deployment_id)

        logger.info(
            f"[ASD] 部署 Agent — image_id={image_id}, agent_id={agent_id}, "
            f"node={node_id}, cpu={cpu_cores}, mem={memory_mb}MB, gpu={gpu_count}, "
            f"replicas={replicas}, backend={self.deploy_backend}"
        )

        if self.deploy_backend == "kubernetes":
            capability = self._capability_from_image(image_id)
            try:
                return self._deploy_kubernetes(
                    record,
                    capability,
                    on_submitted=on_kubernetes_submitted,
                )
            except Exception as exc:
                record.status = "failed"
                record.error_message = str(exc)
                record.updated_at = datetime.utcnow().isoformat()
                logger.error(f"[ASD] Kubernetes 部署失败: agent_id={agent_id}, error={exc}")
                return record

        # 跨节点时降级 mock（本机以外的节点通过跨主机 HTTP dispatch 处理）
        if not self.startup_config.is_local_node(node_id):
            logger.warning(
                f"[ASD] node_id={node_id} 非本机，降级为 mock 部署（跨节点由 dispatch_subtask_to_remote_aoe 处理）"
            )
            record.status = "running"
            record.updated_at = datetime.utcnow().isoformat()
            self._register_to_ardc(agent_id, node_id)
            logger.info(f"[ASD] ✅ Mock 部署成功: deployment_id={deployment_id}")
            return record

        # ── 本机真实 subprocess 部署 ──────────────────────────────────
        port = self._allocate_port(self.startup_config.subprocess_host)
        capability = self._capability_from_image(image_id)

        env = {
            **os.environ,
            "AGENT_ID": agent_id,
            "AGENT_CAPABILITY": capability,
            "PYTHONPATH": self._PROJECT_ROOT,
        }

        proc = subprocess.Popen(
            self.startup_config.subprocess_command(sys.executable, port),
            cwd=self._PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ready = self._wait_for_ready(
            port,
            host=self.startup_config.subprocess_host,
            timeout=self.startup_config.subprocess_ready_timeout,
        )
        if not ready:
            proc.kill()
            proc.wait()
            record.status = "failed"
            record.updated_at = datetime.utcnow().isoformat()
            logger.error(f"[ASD] ❌ 部署失败（8s 内未就绪）: agent_id={agent_id}, port={port}")
            return record

        self._processes[agent_id] = proc
        self._agent_ports[agent_id] = port
        record.status = "running"
        record.updated_at = datetime.utcnow().isoformat()

        # 把真实 ip:port 写回 ARDC 注册表
        self._register_to_ardc(agent_id, node_id, port=port, capability=capability)

        logger.info(
            f"[ASD] ✅ 部署成功: deployment_id={deployment_id}, "
            f"agent_id={agent_id}, port={port}, pid={proc.pid}"
        )
        return record

    def scale_deployment(
        self,
        deployment_id: str,
        replicas: Optional[int] = None,
        cpu_cores: Optional[float] = None,
        memory_mb: Optional[int] = None,
        gpu_count: Optional[int] = None,
    ) -> Optional[DeploymentRecord]:
        """更新部署副本数和资源配置；Kubernetes 后端会 patch Deployment spec。"""
        record = self._deployments.get(deployment_id)
        if not record:
            logger.warning(f"[ASD] scale_deployment: deployment_id={deployment_id} 不存在")
            return None

        if replicas is not None:
            record.replicas = replicas
        if cpu_cores is not None:
            record.cpu_cores = cpu_cores
        if memory_mb is not None:
            record.memory_mb = memory_mb
        if gpu_count is not None:
            record.gpu_count = gpu_count

        if record.backend == "kubernetes":
            self._load_kube_config()
            from kubernetes import client

            apps = client.AppsV1Api()
            container_patch = {
                "name": "agent",
                "resources": self._resource_requirements(
                    record.cpu_cores,
                    record.memory_mb,
                    record.gpu_count,
                ),
            }
            patch = {
                "spec": {
                    "replicas": record.replicas,
                    "template": {
                        "spec": {
                            "containers": [container_patch],
                        }
                    },
                }
            }
            apps.patch_namespaced_deployment(
                name=record.k8s_deployment_name,
                namespace=record.namespace or self.k8s_namespace,
                body=patch,
            )

        record.updated_at = datetime.utcnow().isoformat()
        logger.info(
            f"[ASD] 扩缩容完成: deployment_id={deployment_id}, "
            f"replicas={record.replicas}, cpu={record.cpu_cores}, "
            f"memory={record.memory_mb}MB, gpu={record.gpu_count}"
        )
        return record

    def shutdown_agent(self, agent_id: str, force: bool = False) -> bool:
        """
        关闭指定 Agent 的所有运行实例

        流程：
        1. 停止容器（当前 mock：修改状态）
        2. 从 ARDC 注销
        3. 通知 RRDC 释放资源

        Args:
            agent_id: 要关闭的 Agent 逻辑 ID
            force:    是否强制关闭（忽略引用计数）

        Returns:
            True 表示成功，False 表示 agent_id 不存在
        """
        dep_ids = self._agent_index.get(agent_id, [])
        if not dep_ids:
            logger.warning(f"[ASD] shutdown_agent: agent_id={agent_id} 未找到部署记录")
            return False

        for dep_id in dep_ids:
            rec = self._deployments.get(dep_id)
            if rec and rec.status == "running":
                logger.info(
                    f"[ASD] 关闭 Agent — agent_id={agent_id}, deployment_id={dep_id}"
                )
                rec.status = "stopped"
                rec.updated_at = datetime.utcnow().isoformat()
                if rec.backend == "kubernetes" and rec.k8s_deployment_name:
                    self._delete_kubernetes_workload(rec)

        # 终止 subprocess 进程（若存在）
        proc = self._processes.pop(agent_id, None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self._agent_ports.pop(agent_id, None)
            logger.info(f"[ASD] 进程已终止: agent_id={agent_id}, pid={proc.pid}")

        # 从 ARDC 注销
        self._deregister_from_ardc(agent_id)
        logger.info(f"[ASD] ✅ 关闭成功: agent_id={agent_id}")
        return True

    def shutdown_instance(self, instance_id: str) -> bool:
        """按运行实例 ID 精确停止一个部署，并清理其 Kubernetes 工作负载。"""
        record = next(
            (
                rec
                for rec in self._deployments.values()
                if rec.deployment_id == instance_id or rec.k8s_pod_uid == instance_id
            ),
            None,
        )
        if record is None:
            logger.warning("[ASD] shutdown_instance: instance_id=%s 未找到", instance_id)
            return False
        if record.status == "stopped":
            return True

        if record.backend == "kubernetes" and record.k8s_deployment_name:
            self._delete_kubernetes_workload(record)

        record.status = "stopped"
        record.updated_at = datetime.utcnow().isoformat()

        # subprocess 后端仍以 agent_id 管理单个进程。
        if record.backend != "kubernetes":
            proc = self._processes.pop(record.agent_id, None)
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                self._agent_ports.pop(record.agent_id, None)

        # 即使同一逻辑 Agent 已没有其他运行实例，也保留其注册表 online 状态。
        # remaining = any(
        #     rec.status == "running" and rec.agent_id == record.agent_id
        #     for rec in self._deployments.values()
        # )
        # if not remaining:
        #     self._deregister_from_ardc(record.agent_id)
        logger.info(
            "[ASD] ✅ 实例关闭成功: instance_id=%s, deployment_id=%s",
            instance_id,
            record.deployment_id,
        )
        return True

    def _delete_kubernetes_workload(self, record: DeploymentRecord) -> None:
        """删除 Kubernetes 后端创建的 Agent Deployment 与 Service。"""
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        self._load_kube_config()
        namespace = record.namespace or self.k8s_namespace
        name = record.k8s_deployment_name
        apps = client.AppsV1Api()
        core = client.CoreV1Api()
        errors = []
        try:
            apps.delete_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                errors.append(f"Deployment 删除失败: {exc}")
        try:
            core.delete_namespaced_service(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                errors.append(f"Service 删除失败: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def health_check(self, agent_id: str) -> Dict:
        """
        检查 Agent 健康状态

        Returns:
            {"agent_id": str, "status": str, "deployments": int, "healthy": bool}
        """
        dep_ids = self._agent_index.get(agent_id, [])
        running = [
            d
            for d in dep_ids
            if self._deployments.get(d) and self._deployments[d].status == "running"
        ]
        healthy = len(running) > 0
        return {
            "agent_id": agent_id,
            "status": "running" if healthy else "stopped",
            "deployments": len(running),
            "healthy": healthy,
        }

    def get_running_agents(self) -> List[DeploymentRecord]:
        """返回所有处于 running 状态的部署记录"""
        return [r for r in self._deployments.values() if r.status == "running"]

    def redeploy_agent(self, agent_id: str) -> Optional[DeploymentRecord]:
        """
        重新部署指定 Agent（对应 QoS 告警触发的自动恢复）。

        流程：
        1. 查找此 agent_id 最近一次部署记录，获取 image_id / node_id / 资源配置
        2. 关闭现有实例（shutdown_agent）
        3. 重新部署（deploy_agent）
        4. 返回新部署记录

        Args:
            agent_id: 需要重新部署的 Agent 逻辑 ID

        Returns:
            新的 DeploymentRecord，若无历史记录则返回 None
        """
        dep_ids = self._agent_index.get(agent_id, [])
        if not dep_ids:
            logger.warning(f"[ASD] redeploy_agent: agent_id={agent_id} 无历史部署记录，跳过")
            return None

        # 取最近一条部署记录获取配置
        last_dep = self._deployments.get(dep_ids[-1])
        if not last_dep:
            return None

        image_id = last_dep.image_id
        node_id = last_dep.node_id
        cpu_cores = last_dep.cpu_cores
        memory_mb = last_dep.memory_mb
        gpu_count = last_dep.gpu_count
        replicas = last_dep.replicas

        logger.info(
            f"[ASD] 重部署 Agent — agent_id={agent_id}, "
            f"image={image_id}, node={node_id}"
        )

        # 先关闭旧实例
        self.shutdown_agent(agent_id, force=True)

        # 重新部署
        new_record = self.deploy_agent(
            image_id=image_id,
            agent_id=agent_id,
            node_id=node_id,
            cpu_cores=cpu_cores,
            memory_mb=memory_mb,
            gpu_count=gpu_count,
            replicas=replicas,
        )
        logger.info(f"[ASD] ✅ 重部署成功: agent_id={agent_id}, new_dep={new_record.deployment_id}")

        # 重置 QoS 指标，避免新实例启动后立即再次触发告警
        try:
            from src.runtime.qos_monitor import get_qos_monitor
            get_qos_monitor().reset_metrics(agent_id)
            logger.info(f"[ASD] QoS 指标已重置: agent_id={agent_id}")
        except Exception as qos_err:
            logger.warning(f"[ASD] QoS reset 失败（非关键）: {qos_err}")

        return new_record

    def get_deployment(self, deployment_id: str) -> Optional[DeploymentRecord]:
        """按 deployment_id 查询部署记录"""
        return self._deployments.get(deployment_id)

    def list_deployments_by_agent(self, agent_id: str) -> List[DeploymentRecord]:
        """返回某 agent_id 的所有部署记录"""
        return [
            self._deployments[d]
            for d in self._agent_index.get(agent_id, [])
            if d in self._deployments
        ]

    # ------------------------------------------------------------------
    # ARDC 集成（调用现有 AgentRegistryClient）
    # ------------------------------------------------------------------

    def _register_to_ardc(
        self,
        agent_id: str,
        node_id: str,
        port: Optional[int] = None,
        capability: Optional[str] = None,
        ip: Optional[str] = None,
    ):
        """将运行中的 Agent 实例注册到 ARDC，更新 ip/port/status。"""
        try:
            from src.service.agent_registry import get_registry_client

            registry = get_registry_client()
            registered_ip = ip
            if port is not None and capability is not None:
                register_ip = ip or (
                    self.startup_config.subprocess_host
                    if self.startup_config.is_local_node(node_id)
                    else node_id
                )
                registered_ip = register_ip
                registry.register_agent(
                    agent_id=agent_id,
                    ip=register_ip,
                    port=port,
                    capability=capability,
                )
            else:
                registry.update_agent_status(agent_id, "online")
            logger.debug(
                "[ASD→ARDC] 注册 agent_id=%s, ip=%s, port=%s",
                agent_id,
                registered_ip,
                port,
            )
        except Exception as e:
            logger.warning(f"[ASD→ARDC] 注册失败（非关键）: {e}")

    def _deregister_from_ardc(self, agent_id: str):
        """从 ARDC 注销 Agent，将状态设为 offline。"""
        try:
            from src.service.agent_registry import get_registry_client

            registry = get_registry_client()
            registry.update_agent_status(agent_id, "offline")
            logger.debug(f"[ASD→ARDC] 注销 agent_id={agent_id} 为 offline")
        except Exception as e:
            logger.warning(f"[ASD→ARDC] 注销失败（非关键）: {e}")


# ======================================================================
# 单例访问
# ======================================================================
_scheduler_instance: Optional[AgentScheduler] = None


def get_agent_scheduler() -> AgentScheduler:
    """获取全局 AgentScheduler 单例"""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = AgentScheduler()
    return _scheduler_instance
