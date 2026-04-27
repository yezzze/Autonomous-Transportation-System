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
from typing import Dict, List, Optional
from datetime import datetime

from src.service.agent_startup import AgentStartupConfig
from src.service.nats_startup import NatsStartupConfig

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
        self.nats_config = NatsStartupConfig.from_env()
        self.nats_servers = self.nats_config.servers
        self.ensure_nats = self.startup_config.ensure_nats
        logger.info(
            "AgentScheduler (ASD) 初始化完成: backend=%s, nats_servers=%s, ensure_nats=%s",
            self.deploy_backend,
            self.nats_servers,
            self.ensure_nats,
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
        if self.ensure_nats:
            self._ensure_kubernetes_nats(namespace, apps, core)

        deployment_name = record.k8s_deployment_name or self._safe_k8s_name(record.agent_id)
        record.namespace = namespace
        record.k8s_deployment_name = deployment_name
        labels = {"app": deployment_name, "agent-id": deployment_name}
        is_grpc_entry = self.startup_config.is_grpc_entry(
            record.agent_id,
            record.image_id,
            capability,
        )
        container_port, service_port, service_type, service_port_name = self.startup_config.k8s_ports(is_grpc_entry)
        container = {
            "name": self.startup_config.agent_container_name,
            "image": record.image_id,
            "imagePullPolicy": self.startup_config.image_pull_policy,
            "ports": [{"containerPort": container_port}],
            "env": [
                {"name": "AGENT_ID", "value": record.agent_id},
                {"name": "AGENT_CAPABILITY", "value": capability},
                {"name": "NATS_SERVERS", "value": self.nats_servers},
            ],
            "resources": self._resource_requirements(record.cpu_cores, record.memory_mb, record.gpu_count),
        }
        container.update(self.startup_config.health_probes(container_port))

        pod_spec = {
            "containers": [container],
        }
        node_selector = self._node_selector(record.node_id)
        if node_selector:
            pod_spec["nodeSelector"] = node_selector

        deployment_body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": deployment_name, "labels": labels},
            "spec": {
                "replicas": record.replicas,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": pod_spec,
                },
            },
        }
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

    def _ensure_kubernetes_nats(self, namespace: str, apps, core) -> None:
        """确保集群内 NATS Deployment/Service 存在；已存在时按启动配置 patch 更新。"""
        from kubernetes.client.rest import ApiException

        deployment_body = self.nats_config.deployment_body()
        service_body = self.nats_config.service_body()

        try:
            apps.create_namespaced_deployment(namespace=namespace, body=deployment_body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            apps.patch_namespaced_deployment(
                name=self.nats_config.deployment_name,
                namespace=namespace,
                body=deployment_body,
            )

        try:
            core.create_namespaced_service(namespace=namespace, body=service_body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            core.patch_namespaced_service(
                name=self.nats_config.service_name,
                namespace=namespace,
                body=service_body,
            )

        logger.info(
            "[ASD] NATS ensured: namespace=%s, deployment=%s, service=%s, image=%s, args=%s",
            namespace,
            self.nats_config.deployment_name,
            self.nats_config.service_name,
            self.nats_config.image,
            self.nats_config.server_args(),
        )

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
                return self._deploy_kubernetes(record, capability)
            except Exception as exc:
                record.status = "failed"
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
