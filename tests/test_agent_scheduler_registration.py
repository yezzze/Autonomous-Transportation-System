import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.service.agent_scheduler import AgentScheduler
from src.service.agent_startup import AgentStartupConfig


def _service(service_type, *, port=9031, node_port=None, cluster_ip="10.96.0.10"):
    return SimpleNamespace(
        spec=SimpleNamespace(
            type=service_type,
            cluster_ip=cluster_ip,
            ports=[SimpleNamespace(port=port, node_port=node_port)],
        )
    )


def test_node_port_registration_uses_actual_service_and_node_ip(monkeypatch):
    scheduler = AgentScheduler()
    core = SimpleNamespace(
        read_namespaced_service=lambda **_kwargs: _service(
            "NodePort",
            node_port=30391,
        )
    )
    resources = SimpleNamespace(
        get_node=lambda node_id: SimpleNamespace(node_id=node_id, ip="192.168.49.2")
    )
    monkeypatch.setattr(
        "src.service.resource_registry.get_resource_registry",
        lambda: resources,
    )

    endpoint = scheduler._kubernetes_service_registration_endpoint(
        core=core,
        namespace="default",
        service_name="perception2intermediatefeature-agent",
        node_id="minikube",
    )

    assert endpoint == ("192.168.49.2", 30391)


def test_cluster_ip_registration_uses_service_cluster_address():
    scheduler = AgentScheduler()
    core = SimpleNamespace(
        read_namespaced_service=lambda **_kwargs: _service(
            "ClusterIP",
            port=8080,
            cluster_ip="10.96.20.30",
        )
    )

    endpoint = scheduler._kubernetes_service_registration_endpoint(
        core=core,
        namespace="default",
        service_name="internal-agent",
        node_id="minikube",
    )

    assert endpoint == ("10.96.20.30", 8080)


def test_node_port_registration_requires_resolvable_node_ip(monkeypatch):
    scheduler = AgentScheduler()
    core = SimpleNamespace(
        read_namespaced_service=lambda **_kwargs: _service(
            "NodePort",
            node_port=30391,
        )
    )
    monkeypatch.setattr(
        "src.service.resource_registry.get_resource_registry",
        lambda: SimpleNamespace(get_node=lambda _node_id: None),
    )

    with pytest.raises(RuntimeError, match="无法解析 Kubernetes 节点"):
        scheduler._kubernetes_service_registration_endpoint(
            core=core,
            namespace="default",
            service_name="perception2intermediatefeature-agent",
            node_id="missing-node",
        )


def test_register_to_ardc_prefers_explicit_ip(monkeypatch):
    scheduler = AgentScheduler()
    scheduler.startup_config = AgentStartupConfig(
        local_node_ids=["minikube"],
        subprocess_host="127.0.0.1",
    )
    registered = {}
    registry = SimpleNamespace(
        register_agent=lambda **kwargs: registered.update(kwargs),
    )
    monkeypatch.setattr(
        "src.service.agent_registry.get_registry_client",
        lambda: registry,
    )

    scheduler._register_to_ardc(
        agent_id="perception2intermediatefeature-agent",
        node_id="minikube",
        ip="192.168.49.2",
        port=30391,
        capability="perception",
    )

    assert registered["ip"] == "192.168.49.2"
    assert registered["port"] == 30391


def test_register_to_ardc_keeps_subprocess_fallback(monkeypatch):
    scheduler = AgentScheduler()
    scheduler.startup_config = AgentStartupConfig(
        local_node_ids=["localhost"],
        subprocess_host="127.0.0.1",
    )
    registered = {}
    registry = SimpleNamespace(
        register_agent=lambda **kwargs: registered.update(kwargs),
    )
    monkeypatch.setattr(
        "src.service.agent_registry.get_registry_client",
        lambda: registry,
    )

    scheduler._register_to_ardc(
        agent_id="local-agent",
        node_id="localhost",
        port=9123,
        capability="test",
    )

    assert registered["ip"] == "127.0.0.1"
    assert registered["port"] == 9123
