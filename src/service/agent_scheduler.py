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
import json
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
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
        opensandbox_sandbox_id: Optional[str] = None,
        status: str = "deploying",
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
        self.opensandbox_sandbox_id = opensandbox_sandbox_id
        self.status = status  # deploying | running | stopping | stopped | failed
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
            "opensandbox_sandbox_id": self.opensandbox_sandbox_id,
            "status": self.status,
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
    _AGENT_WAREHOUSE_FILE = Path(_PROJECT_ROOT) / "config" / "agent_warehouse.json"

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
            if self.deploy_backend == "kubernetes" or self._is_opensandbox_backend(self.deploy_backend)
            else self.nats_servers,
        )
        self.nats_jetstream_domain = os.getenv("NATS_JETSTREAM_DOMAIN", "hub")
        self.nats_cloud_jetstream_domain = os.getenv("NATS_CLOUD_JETSTREAM_DOMAIN", "hub")
        self.agent_nats_jetstream_domain = os.getenv(
            "AGENT_NATS_JETSTREAM_DOMAIN",
            self.nats_jetstream_domain,
        )
        self.nats_stream_subjects = os.getenv("NATS_STREAM_SUBJECTS", "workflow.>")
        self.opensandbox_base_url = os.getenv(
            "OPENSANDBOX_SERVER_URL",
            "http://opensandbox-server.opensandbox-system.svc.cluster.local",
        ).rstrip("/")
        self.opensandbox_api_key = os.getenv("OPENSANDBOX_API_KEY", "")
        self.opensandbox_request_timeout = float(os.getenv("OPENSANDBOX_REQUEST_TIMEOUT", "30"))
        self.opensandbox_sandbox_timeout = int(os.getenv("OPENSANDBOX_SANDBOX_TIMEOUT_SECONDS", "3600"))
        logger.info(
            "AgentScheduler (ASD) 初始化完成: backend=%s, nats_servers=%s, "
            "agent_nats_servers=%s, agent_js_domain=%s",
            self.deploy_backend,
            self.nats_servers,
            self.agent_nats_servers,
            self.agent_nats_jetstream_domain or "<local>",
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
    def _sandbox_resource_limits(cpu_cores: float, memory_mb: int, gpu_count: int = 0) -> Dict[str, str]:
        """生成 OpenSandbox resourceLimits，沿用 Kubernetes quantity 表达。"""
        limits = {
            "cpu": AgentScheduler._cpu_quantity(cpu_cores),
            "memory": f"{memory_mb}Mi",
        }
        if gpu_count > 0:
            limits["nvidia.com/gpu"] = str(gpu_count)
        return limits

    @staticmethod
    def _is_opensandbox_backend(backend: str) -> bool:
        """判断当前部署后端是否走 OpenSandbox。"""
        return backend in {"opensandbox", "sandbox"}

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

    def _resolve_jetstream_domain(self, value: Optional[str]) -> str:
        """解析 Agent 配置里的 JetStream domain 别名。"""
        if value is None:
            return self.agent_nats_jetstream_domain
        normalized = str(value).strip().lower()
        if normalized in {"", "local", "edge"}:
            return ""
        if normalized in {"cloud", "hub"}:
            return self.nats_cloud_jetstream_domain
        return str(value)

    def _upsert_agent_nats_env(
        self,
        env_list: List[Dict],
        sandbox_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注入 Agent NATS 环境变量；沙盒 Agent 可按镜像覆盖数据面。"""
        nats_config = {}
        if sandbox_config:
            raw_config = sandbox_config.get("nats") or {}
            if isinstance(raw_config, dict):
                nats_config = raw_config

        servers = str(nats_config.get("servers") or self.agent_nats_servers)
        stream_subjects = str(nats_config.get("streamSubjects") or self.nats_stream_subjects)
        jetstream_domain = self._resolve_jetstream_domain(nats_config.get("jetstreamDomain"))

        self._upsert_env_var(env_list, "NATS_SERVERS", servers)
        self._upsert_env_var(env_list, "NATS_SERVER_URL", servers)
        self._upsert_env_var(env_list, "NATS_JETSTREAM_DOMAIN", jetstream_domain)
        self._upsert_env_var(env_list, "NATS_STREAM_SUBJECTS", stream_subjects)

    @staticmethod
    def _env_list_to_map(env_list: List[Dict]) -> Dict[str, str]:
        """把 Kubernetes env 列表转换成 OpenSandbox env 字典，仅保留静态 value。"""
        env_map: Dict[str, str] = {}
        for item in env_list:
            name = item.get("name")
            if not name:
                continue
            if "value" in item:
                env_map[name] = str(item.get("value", ""))
            elif "valueFrom" in item:
                logger.warning("[ASD] OpenSandbox 暂不转换 valueFrom 环境变量: %s", name)
        return env_map

    @staticmethod
    def _first_container_from_deployment(deployment_body: Optional[Dict]) -> Dict:
        """从 Deployment manifest 中取第一个容器配置。"""
        try:
            containers = deployment_body["spec"]["template"]["spec"]["containers"]
            if containers:
                return containers[0]
        except Exception:
            pass
        return {}

    def _opensandbox_config_for_agent(
        self,
        image_uri: str,
        agent_id: str,
        capability: str,
    ) -> Dict[str, Any]:
        """从 Agent 仓库读取单个镜像的 OpenSandbox 配置。"""
        try:
            data = json.loads(self._AGENT_WAREHOUSE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[ASD] 读取 Agent 仓库失败，跳过 OpenSandbox 单镜像配置: %s", exc)
            return {}

        candidates = {
            image_uri,
            agent_id,
            capability,
            image_uri.rsplit("/", 1)[-1],
        }
        for image in data.get("images") or []:
            identifiers = {
                str(image.get("image_id") or ""),
                str(image.get("name") or ""),
                str(image.get("capability") or ""),
            }
            if not candidates.intersection(identifiers):
                continue
            metadata = image.get("metadata") or {}
            config = metadata.get("opensandbox") or metadata.get("sandbox") or {}
            return config if isinstance(config, dict) else {}
        return {}

    def _image_default_entrypoint(self, image_uri: str) -> List[str]:
        """读取本地 Docker 镜像默认 Entrypoint/Cmd，作为 OpenSandbox entrypoint 回退。"""
        try:
            output = subprocess.check_output(
                ["docker", "image", "inspect", image_uri, "--format", "{{json .Config}}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            config = json.loads(output.strip())
        except Exception as exc:
            logger.warning("[ASD] 读取镜像默认命令失败: image=%s, error=%s", image_uri, exc)
            return []

        entrypoint = config.get("Entrypoint") or []
        cmd = config.get("Cmd") or []
        if isinstance(entrypoint, str):
            entrypoint = shlex.split(entrypoint)
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        return [str(part) for part in entrypoint + cmd]

    def _opensandbox_entrypoint(
        self,
        container: Dict,
        image_uri: str,
        sandbox_config: Dict[str, Any],
    ) -> List[str]:
        """确定 OpenSandbox entrypoint：YAML > 镜像配置 > 环境变量 > 镜像默认命令。"""
        command = container.get("command") or []
        args = container.get("args") or []
        if command:
            return [str(part) for part in command + args]

        configured = sandbox_config.get("entrypoint")
        if isinstance(configured, list) and configured:
            return [str(part) for part in configured]
        if isinstance(configured, str) and configured.strip():
            return shlex.split(configured)

        raw = os.getenv("OPENSANDBOX_ENTRYPOINT") or os.getenv("AGENT_SANDBOX_ENTRYPOINT")
        if raw:
            return shlex.split(raw)

        image_entrypoint = self._image_default_entrypoint(image_uri)
        if image_entrypoint:
            return image_entrypoint

        raise RuntimeError(
            "OpenSandbox requires an entrypoint. Set OPENSANDBOX_ENTRYPOINT "
            "or make the image available locally so Docker can inspect its default Cmd."
        )

    @staticmethod
    def _comma_list_env(name: str) -> List[str]:
        """读取逗号分隔环境变量。"""
        return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]

    def _opensandbox_network_policy(self, sandbox_config: Dict[str, Any]) -> Optional[Dict]:
        """按镜像配置或环境变量生成 OpenSandbox networkPolicy。"""
        configured_policy = sandbox_config.get("networkPolicy")
        if isinstance(configured_policy, dict):
            return configured_policy

        configured_network = sandbox_config.get("network") or {}
        if not isinstance(configured_network, dict):
            configured_network = {}

        default_action = str(
            configured_network.get("defaultAction")
            or os.getenv("OPENSANDBOX_NETWORK_DEFAULT_ACTION", "")
        ).strip().lower()
        allowed_targets = configured_network.get("allowedEgress")
        if allowed_targets is None:
            allowed_targets = self._comma_list_env("OPENSANDBOX_ALLOWED_EGRESS")
        denied_targets = configured_network.get("deniedEgress")
        if denied_targets is None:
            denied_targets = self._comma_list_env("OPENSANDBOX_DENIED_EGRESS")

        allowed_targets = [str(item).strip() for item in allowed_targets if str(item).strip()]
        denied_targets = [str(item).strip() for item in denied_targets if str(item).strip()]
        if not default_action and not allowed_targets and not denied_targets:
            return None

        policy: Dict[str, object] = {"egress": []}
        if default_action:
            policy["defaultAction"] = default_action
        for target in allowed_targets:
            policy["egress"].append({"action": "allow", "target": target})
        for target in denied_targets:
            policy["egress"].append({"action": "deny", "target": target})
        return policy

    def _opensandbox_request(self, method: str, path: str, payload: Optional[Dict] = None) -> Dict:
        """调用 OpenSandbox Server REST API。"""
        import urllib.error
        import urllib.request

        url = f"{self.opensandbox_base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.opensandbox_api_key:
            headers["OPEN-SANDBOX-API-KEY"] = self.opensandbox_api_key

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.opensandbox_request_timeout) as response:
                body = response.read()
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenSandbox API {method} {path} failed: {exc.code} {detail}") from exc

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
        self._upsert_env_var(env, "AGENT_ID", record.agent_id)
        self._upsert_env_var(env, "AGENT_CAPABILITY", capability)
        self._upsert_agent_nats_env(env)

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

    def _deploy_kubernetes(self, record: DeploymentRecord, capability: str) -> DeploymentRecord:
        """用 Kubernetes Deployment/Service 启动 Agent，并注册到 ARDC。"""
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

        try:
            core.create_namespaced_service(namespace=namespace, body=service_body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            core.patch_namespaced_service(name=deployment_name, namespace=namespace, body=service_body)

        record.status = "running"
        record.updated_at = datetime.utcnow().isoformat()
        self._register_to_ardc(record.agent_id, record.node_id, port=service_port, capability=capability)
        return record

    def _deploy_opensandbox(self, record: DeploymentRecord, capability: str) -> DeploymentRecord:
        """通过 OpenSandbox Server 创建受控沙盒，而不是直接创建普通 Deployment。"""
        deployment_name = record.k8s_deployment_name or self._safe_k8s_name(record.agent_id)
        record.k8s_deployment_name = deployment_name
        record.namespace = os.getenv("OPENSANDBOX_WORKLOAD_NAMESPACE", self.k8s_namespace)

        manifest_bodies = self._load_kubernetes_manifest_from_yaml(deployment_name)
        container = self._first_container_from_deployment(
            manifest_bodies.get("deployment") if manifest_bodies else None
        )
        image_uri = container.get("image") or record.image_id
        sandbox_config = self._opensandbox_config_for_agent(
            image_uri,
            record.agent_id,
            capability,
        )
        entrypoint = self._opensandbox_entrypoint(container, image_uri, sandbox_config)

        env_list = list(container.get("env") or [])
        self._upsert_env_var(env_list, "AGENT_ID", record.agent_id)
        self._upsert_env_var(env_list, "AGENT_CAPABILITY", capability)
        self._upsert_agent_nats_env(env_list, sandbox_config)
        env = self._env_list_to_map(env_list)
        configured_env = sandbox_config.get("env") or {}
        if isinstance(configured_env, dict):
            env.update({str(key): str(value) for key, value in configured_env.items()})

        request_body = {
            "image": {"uri": image_uri},
            "entrypoint": entrypoint,
            "timeout": int(sandbox_config.get("timeout", self.opensandbox_sandbox_timeout)),
            "resourceLimits": self._sandbox_resource_limits(
                record.cpu_cores,
                record.memory_mb,
                record.gpu_count,
            ),
            "env": env,
            "metadata": {
                "agent": deployment_name,
                "deployment": record.deployment_id,
                "capability": capability,
                "managed": "ats",
            },
        }

        network_policy = self._opensandbox_network_policy(sandbox_config)
        if network_policy:
            request_body["networkPolicy"] = network_policy

        logger.info(
            "[ASD] OpenSandbox 创建请求: agent_id=%s, image=%s, entrypoint=%s, "
            "nats=%s, js_domain=%s",
            record.agent_id,
            image_uri,
            entrypoint,
            env.get("NATS_SERVERS"),
            env.get("NATS_JETSTREAM_DOMAIN") or "<local>",
        )
        response = self._opensandbox_request("POST", "/v1/sandboxes", request_body)
        sandbox_id = response.get("id")
        if not sandbox_id:
            raise RuntimeError(f"OpenSandbox create response missing id: {response}")

        record.opensandbox_sandbox_id = sandbox_id
        record.status = "running"
        record.updated_at = datetime.utcnow().isoformat()
        self._register_to_ardc(record.agent_id, record.node_id)
        logger.info(
            "[ASD] ✅ OpenSandbox 部署成功: deployment_id=%s, sandbox_id=%s",
            record.deployment_id,
            sandbox_id,
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
            namespace=self.k8s_namespace
            if self.deploy_backend == "kubernetes" or self._is_opensandbox_backend(self.deploy_backend)
            else None,
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
                return self._deploy_kubernetes(record, capability)
            except Exception as exc:
                record.status = "failed"
                record.updated_at = datetime.utcnow().isoformat()
                logger.error(f"[ASD] Kubernetes 部署失败: agent_id={agent_id}, error={exc}")
                return record

        if self._is_opensandbox_backend(self.deploy_backend):
            capability = self._capability_from_image(image_id)
            try:
                return self._deploy_opensandbox(record, capability)
            except Exception as exc:
                record.status = "failed"
                record.updated_at = datetime.utcnow().isoformat()
                logger.error(f"[ASD] OpenSandbox 部署失败: agent_id={agent_id}, error={exc}")
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
        elif self._is_opensandbox_backend(record.backend):
            logger.warning(
                "[ASD] OpenSandbox 后端暂不支持原地扩缩容/资源 patch；"
                "请通过 shutdown_agent + deploy_agent 重新创建沙盒"
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
                elif self._is_opensandbox_backend(rec.backend) and rec.opensandbox_sandbox_id:
                    self._delete_opensandbox_sandbox(rec)

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

    def _delete_kubernetes_workload(self, record: DeploymentRecord) -> None:
        """删除 Kubernetes 后端创建的 Agent Deployment/Service；清理失败只记录警告。"""
        try:
            from kubernetes import client
            from kubernetes.client.rest import ApiException

            self._load_kube_config()
            namespace = record.namespace or self.k8s_namespace
            name = record.k8s_deployment_name
            apps = client.AppsV1Api()
            core = client.CoreV1Api()
            try:
                apps.delete_namespaced_deployment(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise
            try:
                core.delete_namespaced_service(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise
        except Exception as exc:
            logger.warning(f"[ASD] Kubernetes 清理失败（非关键）: {exc}")

    def _delete_opensandbox_sandbox(self, record: DeploymentRecord) -> None:
        """删除 OpenSandbox 后端创建的沙盒；清理失败只记录警告。"""
        sandbox_id = record.opensandbox_sandbox_id
        if not sandbox_id:
            return
        try:
            self._opensandbox_request("DELETE", f"/v1/sandboxes/{sandbox_id}")
            logger.info("[ASD] OpenSandbox 沙盒已删除: sandbox_id=%s", sandbox_id)
        except Exception as exc:
            logger.warning(f"[ASD] OpenSandbox 清理失败（非关键）: sandbox_id={sandbox_id}, error={exc}")

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
    ):
        """将运行中的 Agent 实例注册到 ARDC，更新 ip/port/status。"""
        try:
            from src.service.agent_registry import get_registry_client

            registry = get_registry_client()
            if port is not None and capability is not None:
                ip = self.startup_config.subprocess_host if self.startup_config.is_local_node(node_id) else node_id
                registry.register_agent(
                    agent_id=agent_id,
                    ip=ip,
                    port=port,
                    capability=capability,
                )
            else:
                registry.update_agent_status(agent_id, "online")
            logger.debug(f"[ASD→ARDC] 注册 agent_id={agent_id}, port={port}")
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
