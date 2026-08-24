"""Shared resource parsing and RRDC node selection for Agent images."""
from __future__ import annotations

from typing import Any, Optional

from src.runtime.models import ResourceConfig


def resource_config_for_image(
    image: Any,
    *,
    capability: Optional[str] = None,
) -> ResourceConfig:
    """Resolve image resource requirements to an explicit local node.

    An image-specified node is authoritative.  Otherwise RRDC selects a node
    in the current AOE's Kubernetes cluster that satisfies all requirements.
    """
    metadata = dict(getattr(image, "metadata", None) or {})
    k8s = dict(metadata.get("k8s") or {})
    cpu = float(k8s.get("cpu_cores", 1.0))
    memory_mb = int(k8s.get("memory_mb", 512))
    gpu = int(k8s.get("gpu_count", 0))
    configured_node = str(k8s.get("node_id") or "").strip()

    if configured_node:
        return ResourceConfig(
            cpu_cores=cpu,
            memory_mb=memory_mb,
            node_id=configured_node,
            gpu_count=gpu,
        )

    from src.service.resource_registry import get_resource_registry

    registry = get_resource_registry()
    if not registry.refresh_from_kubernetes():
        raise RuntimeError("RRDC_RESOURCE_REFRESH_FAILED: Kubernetes 资源状态刷新失败")

    effective_capability = str(
        capability or getattr(image, "capability", "") or ""
    ).strip()
    candidates = []
    if effective_capability:
        candidates = registry.query_available_resources(
            min_cpu=cpu,
            min_mem_mb=memory_mb,
            min_gpu=gpu,
            tags=[effective_capability],
        )
    if not candidates:
        candidates = registry.query_available_resources(
            min_cpu=cpu,
            min_mem_mb=memory_mb,
            min_gpu=gpu,
        )
    if not candidates:
        agent_name = str(getattr(image, "name", "") or effective_capability or "unknown")
        raise RuntimeError(
            "RRDC_NO_SUITABLE_NODE: "
            f"Agent {agent_name} 无满足资源需求的本地节点 "
            f"(cpu={cpu}, memory_mb={memory_mb}, gpu={gpu})"
        )

    chosen = candidates[0]
    return ResourceConfig(
        cpu_cores=cpu,
        memory_mb=memory_mb,
        node_id=chosen.node_id,
        gpu_count=gpu,
    )
