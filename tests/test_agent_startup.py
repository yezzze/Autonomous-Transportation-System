import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.service.agent_startup import AgentStartupConfig


AGENT_ENV_KEYS = [
    "AGENT_DEPLOY_BACKEND",
    "K8S_NAMESPACE",
    "AGENT_CONTAINER_NAME",
    "AGENT_CONTAINER_PORT",
    "AGENT_SERVICE_PORT",
    "AGENT_SERVICE_TYPE",
    "AGENT_GRPC_CONTAINER_PORT",
    "AGENT_GRPC_SERVICE_PORT",
    "AGENT_GRPC_SERVICE_TYPE",
    "AGENT_GRPC_NODE_PORT",
    "AGENT_A_NODE_PORT",
    "AGENT_IMAGE_PULL_POLICY",
    "AGENT_ENABLE_HEALTH_PROBE",
    "AGENT_HEALTH_PATH",
    "AGENT_READINESS_INITIAL_DELAY_SECONDS",
    "AGENT_READINESS_PERIOD_SECONDS",
    "AGENT_LIVENESS_INITIAL_DELAY_SECONDS",
    "AGENT_LIVENESS_PERIOD_SECONDS",
    "AGENT_LOCAL_NODE_IDS",
    "AGENT_SUBPROCESS_HOST",
    "AGENT_SUBPROCESS_SCRIPT",
    "AGENT_SUBPROCESS_READY_TIMEOUT",
    "AGENT_SUBPROCESS_EXTRA_ARGS",
    "AGENT_GRPC_ID_PREFIXES",
    "AGENT_GRPC_IMAGE_MARKERS",
    "AGENT_GRPC_CAPABILITIES",
]


def _clear_agent_env(monkeypatch):
    for key in AGENT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_agent_startup_defaults_match_existing_behavior(monkeypatch):
    _clear_agent_env(monkeypatch)

    cfg = AgentStartupConfig.from_env()

    assert cfg.deploy_backend == "subprocess"
    assert cfg.k8s_namespace == "default"
    assert cfg.k8s_ports(False) == (8000, 8000, "ClusterIP", "http")
    assert cfg.k8s_ports(True) == (50051, 50051, "NodePort", "grpc")
    assert cfg.image_pull_policy == "IfNotPresent"
    assert cfg.health_probes(8000) == {}
    assert cfg.is_local_node("localhost")
    assert cfg.is_grpc_entry("agent-grpc", "agent-grpc:v1", "agent-grpc")
    assert cfg.subprocess_command("python", 9000) == ["python", "agent_server.py", "9000"]


def test_agent_startup_reads_env(monkeypatch):
    _clear_agent_env(monkeypatch)

    monkeypatch.setenv("AGENT_DEPLOY_BACKEND", "kubernetes")
    monkeypatch.setenv("K8S_NAMESPACE", "demo")
    monkeypatch.setenv("AGENT_CONTAINER_NAME", "worker")
    monkeypatch.setenv("AGENT_CONTAINER_PORT", "8080")
    monkeypatch.setenv("AGENT_SERVICE_PORT", "80")
    monkeypatch.setenv("AGENT_SERVICE_TYPE", "LoadBalancer")
    monkeypatch.setenv("AGENT_GRPC_CONTAINER_PORT", "15051")
    monkeypatch.setenv("AGENT_GRPC_SERVICE_PORT", "50051")
    monkeypatch.setenv("AGENT_GRPC_SERVICE_TYPE", "ClusterIP")
    monkeypatch.setenv("AGENT_GRPC_NODE_PORT", "30051")
    monkeypatch.setenv("AGENT_IMAGE_PULL_POLICY", "Always")
    monkeypatch.setenv("AGENT_ENABLE_HEALTH_PROBE", "true")
    monkeypatch.setenv("AGENT_HEALTH_PATH", "/ready")
    monkeypatch.setenv("AGENT_LOCAL_NODE_IDS", "local-a,local-b")
    monkeypatch.setenv("AGENT_SUBPROCESS_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_SUBPROCESS_SCRIPT", "custom_agent.py")
    monkeypatch.setenv("AGENT_SUBPROCESS_READY_TIMEOUT", "3.5")
    monkeypatch.setenv("AGENT_SUBPROCESS_EXTRA_ARGS", "--foo bar")
    monkeypatch.setenv("AGENT_GRPC_ID_PREFIXES", "grpc-entry")
    monkeypatch.setenv("AGENT_GRPC_IMAGE_MARKERS", "grpc-image")
    monkeypatch.setenv("AGENT_GRPC_CAPABILITIES", "grpc-cap")

    cfg = AgentStartupConfig.from_env()

    assert cfg.deploy_backend == "kubernetes"
    assert cfg.k8s_namespace == "demo"
    assert cfg.agent_container_name == "worker"
    assert cfg.k8s_ports(False) == (8080, 80, "LoadBalancer", "http")
    assert cfg.k8s_ports(True) == (15051, 50051, "ClusterIP", "grpc")
    assert cfg.grpc_node_port == 30051
    assert cfg.image_pull_policy == "Always"
    assert cfg.is_local_node("local-a")
    assert not cfg.is_local_node("localhost")
    assert cfg.is_grpc_entry("grpc-entry-1", "image:v1", "x")
    assert cfg.is_grpc_entry("agent", "my-grpc-image:v1", "x")
    assert cfg.is_grpc_entry("agent", "image:v1", "grpc-cap")
    assert cfg.subprocess_command("python", 9000) == [
        "python",
        "custom_agent.py",
        "9000",
        "--foo",
        "bar",
    ]
    assert cfg.subprocess_ready_timeout == 3.5
    assert cfg.health_probes(8080)["readinessProbe"]["httpGet"] == {
        "path": "/ready",
        "port": 8080,
    }
