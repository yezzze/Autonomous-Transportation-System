"""
Agent 启动参数配置。

这个模块集中管理 AgentScheduler 启动普通 Agent、gRPC 入口 Agent、
本机 subprocess Agent 时用到的默认值，避免这些参数散落在调度逻辑里。
"""

import os
import shlex
from typing import Dict, List, Optional, Tuple


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，支持 true/false、1/0、yes/no 等常见写法。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；未配置或空字符串时使用默认值。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_list(name: str, default: List[str]) -> List[str]:
    """读取逗号分隔的环境变量列表；未配置时返回默认列表副本。"""
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class AgentStartupConfig:
    """Agent 启动配置，供 AgentScheduler 在 Kubernetes/subprocess 后端复用。"""

    def __init__(
        self,
        deploy_backend: str = "subprocess",
        k8s_namespace: str = "default",
        agent_container_name: str = "agent",
        agent_container_port: int = 8000,
        agent_service_port: Optional[int] = None,
        agent_service_type: str = "ClusterIP",
        grpc_container_port: int = 50051,
        grpc_service_port: Optional[int] = None,
        grpc_service_type: str = "NodePort",
        grpc_node_port: Optional[int] = None,
        image_pull_policy: str = "IfNotPresent",
        enable_health_probe: bool = False,
        health_path: str = "/health",
        readiness_initial_delay_seconds: int = 5,
        readiness_period_seconds: int = 5,
        liveness_initial_delay_seconds: int = 20,
        liveness_period_seconds: int = 10,
        local_node_ids: Optional[List[str]] = None,
        subprocess_host: str = "127.0.0.1",
        subprocess_script: str = "agent_server.py",
        subprocess_ready_timeout: float = 15.0,
        subprocess_extra_args: Optional[List[str]] = None,
        grpc_agent_id_prefixes: Optional[List[str]] = None,
        grpc_image_markers: Optional[List[str]] = None,
        grpc_capabilities: Optional[List[str]] = None,
    ):
        self.deploy_backend = deploy_backend
        self.k8s_namespace = k8s_namespace
        self.agent_container_name = agent_container_name
        self.agent_container_port = agent_container_port
        self.agent_service_port = agent_service_port or agent_container_port
        self.agent_service_type = agent_service_type
        self.grpc_container_port = grpc_container_port
        self.grpc_service_port = grpc_service_port or grpc_container_port
        self.grpc_service_type = grpc_service_type
        self.grpc_node_port = grpc_node_port
        self.image_pull_policy = image_pull_policy
        self.enable_health_probe = enable_health_probe
        self.health_path = health_path
        self.readiness_initial_delay_seconds = readiness_initial_delay_seconds
        self.readiness_period_seconds = readiness_period_seconds
        self.liveness_initial_delay_seconds = liveness_initial_delay_seconds
        self.liveness_period_seconds = liveness_period_seconds
        self.local_node_ids = local_node_ids or [
            "localhost",
            "127.0.0.1",
            "host.docker.internal",
            "node_localhost",
        ]
        self.subprocess_host = subprocess_host
        self.subprocess_script = subprocess_script
        self.subprocess_ready_timeout = subprocess_ready_timeout
        self.subprocess_extra_args = subprocess_extra_args or []
        self.grpc_agent_id_prefixes = grpc_agent_id_prefixes or ["agent-grpc", "agent-a"]
        self.grpc_image_markers = grpc_image_markers or ["agent-grpc", "agent-a-grpc"]
        self.grpc_capabilities = grpc_capabilities or ["agent-grpc", "agent-a"]

    @classmethod
    def from_env(cls) -> "AgentStartupConfig":
        """从 server 启动环境变量生成 Agent 启动配置。"""
        grpc_node_port = os.getenv("AGENT_GRPC_NODE_PORT") or os.getenv("AGENT_A_NODE_PORT")
        return cls(
            deploy_backend=os.getenv("AGENT_DEPLOY_BACKEND", "subprocess").strip().lower(),
            k8s_namespace=os.getenv("K8S_NAMESPACE", "default"),
            agent_container_name=os.getenv("AGENT_CONTAINER_NAME", "agent"),
            agent_container_port=_env_int("AGENT_CONTAINER_PORT", 8000),
            agent_service_port=_env_int(
                "AGENT_SERVICE_PORT",
                _env_int("AGENT_CONTAINER_PORT", 8000),
            ),
            agent_service_type=os.getenv("AGENT_SERVICE_TYPE", "ClusterIP"),
            grpc_container_port=_env_int("AGENT_GRPC_CONTAINER_PORT", 50051),
            grpc_service_port=_env_int("AGENT_GRPC_SERVICE_PORT", 50051),
            grpc_service_type=os.getenv("AGENT_GRPC_SERVICE_TYPE", "NodePort"),
            grpc_node_port=int(grpc_node_port) if grpc_node_port else None,
            image_pull_policy=os.getenv("AGENT_IMAGE_PULL_POLICY", "IfNotPresent"),
            enable_health_probe=_env_bool("AGENT_ENABLE_HEALTH_PROBE", False),
            health_path=os.getenv("AGENT_HEALTH_PATH", "/health"),
            readiness_initial_delay_seconds=_env_int("AGENT_READINESS_INITIAL_DELAY_SECONDS", 5),
            readiness_period_seconds=_env_int("AGENT_READINESS_PERIOD_SECONDS", 5),
            liveness_initial_delay_seconds=_env_int("AGENT_LIVENESS_INITIAL_DELAY_SECONDS", 20),
            liveness_period_seconds=_env_int("AGENT_LIVENESS_PERIOD_SECONDS", 10),
            local_node_ids=_env_list(
                "AGENT_LOCAL_NODE_IDS",
                ["localhost", "127.0.0.1", "host.docker.internal", "node_localhost"],
            ),
            subprocess_host=os.getenv("AGENT_SUBPROCESS_HOST", "127.0.0.1"),
            subprocess_script=os.getenv("AGENT_SUBPROCESS_SCRIPT", "agent_server.py"),
            subprocess_ready_timeout=float(os.getenv("AGENT_SUBPROCESS_READY_TIMEOUT", "15.0")),
            subprocess_extra_args=shlex.split(os.getenv("AGENT_SUBPROCESS_EXTRA_ARGS", "")),
            grpc_agent_id_prefixes=_env_list("AGENT_GRPC_ID_PREFIXES", ["agent-grpc", "agent-a"]),
            grpc_image_markers=_env_list("AGENT_GRPC_IMAGE_MARKERS", ["agent-grpc", "agent-a-grpc"]),
            grpc_capabilities=_env_list("AGENT_GRPC_CAPABILITIES", ["agent-grpc", "agent-a"]),
        )

    def is_local_node(self, node_id: str) -> bool:
        """判断 node_id 是否代表本机部署目标。"""
        return node_id in set(self.local_node_ids)

    def is_grpc_entry(self, agent_id: str, image_id: str, capability: str) -> bool:
        """判断当前 Agent 是否应按 gRPC 入口服务暴露。"""
        return (
            any(agent_id.startswith(prefix) for prefix in self.grpc_agent_id_prefixes)
            or any(marker in image_id for marker in self.grpc_image_markers)
            or capability in set(self.grpc_capabilities)
        )

    def k8s_ports(self, is_grpc_entry: bool) -> Tuple[int, int, str, str]:
        """返回 Kubernetes containerPort、servicePort、Service 类型和端口名称。"""
        if is_grpc_entry:
            return (
                self.grpc_container_port,
                self.grpc_service_port,
                self.grpc_service_type,
                "grpc",
            )
        return (
            self.agent_container_port,
            self.agent_service_port,
            self.agent_service_type,
            "http",
        )

    def health_probes(self, container_port: int) -> Dict:
        """生成 Kubernetes readiness/liveness probe 配置；未启用时返回空字典。"""
        if not self.enable_health_probe:
            return {}

        probe = {"httpGet": {"path": self.health_path, "port": container_port}}
        return {
            "readinessProbe": {
                **probe,
                "initialDelaySeconds": self.readiness_initial_delay_seconds,
                "periodSeconds": self.readiness_period_seconds,
            },
            "livenessProbe": {
                **probe,
                "initialDelaySeconds": self.liveness_initial_delay_seconds,
                "periodSeconds": self.liveness_period_seconds,
            },
        }

    def subprocess_command(self, python_executable: str, port: int) -> List[str]:
        """生成本机 subprocess 后端启动 agent_server 的命令。"""
        return [
            python_executable,
            self.subprocess_script,
            str(port),
        ] + self.subprocess_extra_args
