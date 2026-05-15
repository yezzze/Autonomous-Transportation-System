import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.service.nats_startup import NatsStartupConfig


NATS_ENV_KEYS = [
    "NATS_DEPLOYMENT_NAME",
    "NATS_SERVICE_NAME",
    "NATS_APP_LABEL",
    "NATS_IMAGE",
    "NATS_REPLICAS",
    "NATS_CLIENT_PORT",
    "NATS_MONITOR_PORT",
    "NATS_SERVICE_TYPE",
    "NATS_JETSTREAM",
    "NATS_STORE_DIR",
    "NATS_MAX_PAYLOAD",
    "NATS_CONFIG_DIR",
    "NATS_CONFIG_FILE_NAME",
    "NATS_EXTRA_ARGS",
    "NATS_SERVERS",
    "AGENT_NATS_SERVERS",
    "AGENT_DEPLOY_BACKEND",
]


def _clear_nats_env(monkeypatch):
    for key in NATS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_nats_startup_defaults_match_demo_nats(monkeypatch):
    _clear_nats_env(monkeypatch)

    cfg = NatsStartupConfig.from_env()

    deployment = cfg.deployment_body()
    service = cfg.service_body()
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert cfg.servers == "nats://nats:4222"
    assert container["image"] == "nats:2.10"
    assert container["args"] == ["-c", "/etc/nats-config/nats.conf"]
    assert cfg.config_text() == (
        "port: 4222\n"
        "http_port: 8222\n"
        "max_payload: 8388608\n"
        "jetstream {\n"
        "  store_dir: \"/data\"\n"
        "}\n"
    )
    assert cfg.config_map_body()["data"]["nats.conf"] == cfg.config_text()
    assert container["ports"] == [
        {"containerPort": 4222, "name": "client"},
        {"containerPort": 8222, "name": "monitor"},
    ]
    assert service["metadata"]["name"] == "nats"
    assert service["spec"]["type"] == "ClusterIP"


def test_nats_startup_reads_env(monkeypatch):
    _clear_nats_env(monkeypatch)

    monkeypatch.setenv("NATS_DEPLOYMENT_NAME", "nats-main")
    monkeypatch.setenv("NATS_SERVICE_NAME", "nats-bus")
    monkeypatch.setenv("NATS_APP_LABEL", "nats-bus")
    monkeypatch.setenv("NATS_IMAGE", "nats:2.11")
    monkeypatch.setenv("NATS_REPLICAS", "2")
    monkeypatch.setenv("NATS_CLIENT_PORT", "4223")
    monkeypatch.setenv("NATS_MONITOR_PORT", "8223")
    monkeypatch.setenv("NATS_SERVICE_TYPE", "NodePort")
    monkeypatch.setenv("NATS_STORE_DIR", "/jetstream")
    monkeypatch.setenv("NATS_MAX_PAYLOAD", "4194304")
    monkeypatch.setenv("NATS_CONFIG_DIR", "/config")
    monkeypatch.setenv("NATS_CONFIG_FILE_NAME", "server.conf")
    monkeypatch.setenv("NATS_EXTRA_ARGS", "--debug --trace")

    cfg = NatsStartupConfig.from_env()
    deployment = cfg.deployment_body()
    service = cfg.service_body()
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert cfg.servers == "nats://nats-bus:4223"
    assert deployment["metadata"]["name"] == "nats-main"
    assert deployment["spec"]["replicas"] == 2
    assert container["image"] == "nats:2.11"
    assert container["args"] == ["-c", "/config/server.conf", "--debug", "--trace"]
    assert cfg.config_text() == (
        "port: 4223\n"
        "http_port: 8223\n"
        "max_payload: 4194304\n"
        "jetstream {\n"
        "  store_dir: \"/jetstream\"\n"
        "}\n"
    )
    assert service["metadata"]["name"] == "nats-bus"
    assert service["spec"]["ports"][0]["port"] == 4223
    assert service["spec"]["ports"][1]["port"] == 8223
    assert service["spec"]["type"] == "NodePort"


def test_kubernetes_agent_nats_uses_in_cluster_service(monkeypatch):
    _clear_nats_env(monkeypatch)
    monkeypatch.setenv("AGENT_DEPLOY_BACKEND", "kubernetes")
    monkeypatch.setenv("NATS_SERVERS", "nats://127.0.0.1:14222")

    from src.service.agent_scheduler import AgentScheduler

    scheduler = AgentScheduler()

    assert scheduler.nats_servers == "nats://127.0.0.1:14222"
    assert scheduler.agent_nats_servers == "nats://nats:4222"


def test_nats_startup_can_disable_jetstream(monkeypatch):
    _clear_nats_env(monkeypatch)

    monkeypatch.setenv("NATS_JETSTREAM", "false")
    monkeypatch.setenv("NATS_EXTRA_ARGS", "--port=4222")

    cfg = NatsStartupConfig.from_env()

    assert cfg.server_args() == ["-c", "/etc/nats-config/nats.conf", "--port=4222"]
    assert cfg.config_text() == (
        "port: 4222\n"
        "http_port: 8222\n"
        "max_payload: 8388608\n"
    )
