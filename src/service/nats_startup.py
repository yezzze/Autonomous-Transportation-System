"""
NATS 启动参数配置。

这个模块集中管理 Kubernetes 后端自动创建 NATS 时用到的 Deployment/Service
参数，让 server 启动时可以通过环境变量修改，而不是改 AgentScheduler 代码。
"""

import os
import shlex
from typing import Dict, List


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，未配置时使用默认值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量，未配置或空字符串时使用默认值。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


class NatsStartupConfig:
    """NATS Deployment/Service 的启动配置。"""

    def __init__(
        self,
        deployment_name: str = "nats",
        service_name: str = "nats",
        app_label: str = "nats",
        image: str = "nats:2.10",
        replicas: int = 1,
        client_port: int = 4222,
        monitor_port: int = 8222,
        service_type: str = "ClusterIP",
        jetstream: bool = True,
        store_dir: str = "/data",
        max_payload: str = "8388608",
        config_dir: str = "/etc/nats-config",
        config_file_name: str = "nats.conf",
        extra_args: List[str] = None,
        servers: str = "",
    ):
        """初始化 NATS 启动参数，默认值保持和原 k8s/nats.yaml 一致。"""
        self.deployment_name = deployment_name
        self.service_name = service_name
        self.app_label = app_label
        self.image = image
        self.replicas = replicas
        self.client_port = client_port
        self.monitor_port = monitor_port
        self.service_type = service_type
        self.jetstream = jetstream
        self.store_dir = store_dir
        self.max_payload = max_payload
        self.config_dir = config_dir
        self.config_file_name = config_file_name
        self.extra_args = extra_args or []
        self.servers = servers

    @classmethod
    def from_env(cls) -> "NatsStartupConfig":
        """从 server 启动环境变量生成 NATS 启动配置。"""
        service_name = os.getenv("NATS_SERVICE_NAME", "nats")
        client_port = _env_int("NATS_CLIENT_PORT", 4222)
        return cls(
            deployment_name=os.getenv("NATS_DEPLOYMENT_NAME", "nats"),
            service_name=service_name,
            app_label=os.getenv("NATS_APP_LABEL", "nats"),
            image=os.getenv("NATS_IMAGE", "nats:2.10"),
            replicas=_env_int("NATS_REPLICAS", 1),
            client_port=client_port,
            monitor_port=_env_int("NATS_MONITOR_PORT", 8222),
            service_type=os.getenv("NATS_SERVICE_TYPE", "ClusterIP"),
            jetstream=_env_bool("NATS_JETSTREAM", True),
            store_dir=os.getenv("NATS_STORE_DIR", "/data"),
            max_payload=os.getenv("NATS_MAX_PAYLOAD", "8388608"),
            config_dir=os.getenv("NATS_CONFIG_DIR", "/etc/nats-config"),
            config_file_name=os.getenv("NATS_CONFIG_FILE_NAME", "nats.conf"),
            extra_args=shlex.split(os.getenv("NATS_EXTRA_ARGS", "")),
            servers=os.getenv("NATS_SERVERS", f"nats://{service_name}:{client_port}"),
        )

    @property
    def labels(self) -> Dict[str, str]:
        """返回 NATS Deployment/Service 共用的 selector labels。"""
        return {"app": self.app_label}

    def server_args(self) -> List[str]:
        """生成 nats-server 容器启动参数。"""
        args: List[str] = ["-c", f"{self.config_dir}/{self.config_file_name}"]
        args.extend(self.extra_args)
        return args

    def config_text(self) -> str:
        """生成 nats-server 配置文件内容。"""
        lines = [
            f"port: {self.client_port}",
            f"http_port: {self.monitor_port}",
        ]
        if self.max_payload:
            lines.append(f"max_payload: {self.max_payload}")
        if self.jetstream:
            lines.append("jetstream {")
            if self.store_dir:
                lines.append(f"  store_dir: \"{self.store_dir}\"")
            lines.append("}")
        return "\n".join(lines) + "\n"

    def config_map_body(self) -> Dict:
        """生成挂载给 NATS 使用的 Kubernetes ConfigMap body。"""
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{self.deployment_name}-config", "labels": self.labels},
            "data": {
                self.config_file_name: self.config_text(),
            },
        }

    def deployment_body(self) -> Dict:
        """生成 Kubernetes Deployment body。"""
        container = {
            "name": "nats",
            "image": self.image,
            "ports": [
                {"containerPort": self.client_port, "name": "client"},
                {"containerPort": self.monitor_port, "name": "monitor"},
            ],
            "volumeMounts": [
                {
                    "name": "nats-config",
                    "mountPath": self.config_dir,
                }
            ],
        }
        args = self.server_args()
        if args:
            container["args"] = args

        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.deployment_name, "labels": self.labels},
            "spec": {
                "replicas": self.replicas,
                "selector": {"matchLabels": self.labels},
                "template": {
                    "metadata": {"labels": self.labels},
                    "spec": {
                        "containers": [container],
                        "volumes": [
                            {
                                "name": "nats-config",
                                "configMap": {"name": f"{self.deployment_name}-config"},
                            }
                        ],
                    },
                },
            },
        }

    def service_body(self) -> Dict:
        """生成 Kubernetes Service body。"""
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": self.service_name, "labels": self.labels},
            "spec": {
                "selector": self.labels,
                "ports": [
                    {
                        "name": "client",
                        "port": self.client_port,
                        "targetPort": self.client_port,
                    },
                    {
                        "name": "monitor",
                        "port": self.monitor_port,
                        "targetPort": self.monitor_port,
                    },
                ],
                "type": self.service_type,
            },
        }
