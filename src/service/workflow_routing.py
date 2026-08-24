"""Validation and immutable runtime routing for application workflows."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


class WorkflowRoutingError(ValueError):
    """A workflow cannot be bound safely to deployed agent instances."""


def plan_signature(tasks: Sequence[Mapping[str, Any]]) -> Tuple[Tuple[str, ...], ...]:
    """Capture topology and system routing fields while ignoring business parameters."""
    signature = []
    for task in tasks:
        parameters = task.get("parameters") or {}
        signature.append((
            str(task.get("task_id", "")),
            str(task.get("assigned_agent_id", "")),
            str(parameters.get("source_cluster", "")),
            str(parameters.get("target_cluster", "")),
            str(parameters.get("target_agent_id", "")),
            str(parameters.get("target_instance_id", "")),
        ))
    return tuple(signature)


def bind_linear_workflow(
    tasks: Sequence[Mapping[str, Any]],
    runtime_instances: Iterable[Mapping[str, Any]],
) -> tuple[List[Dict[str, Any]], Tuple[Tuple[str, ...], ...]]:
    """Validate a linear plan and inject immutable neighbour routing parameters."""
    if len(tasks) < 2:
        raise WorkflowRoutingError("SINGLE_NODE_WORKFLOW: 工作流至少需要两个节点")

    normalized_tasks = [dict(task) for task in tasks]
    if any(task.get("parallel_group") for task in normalized_tasks):
        raise WorkflowRoutingError("NON_LINEAR_WORKFLOW: 当前仅支持线性工作流，不支持并行组")
    if any(task.get("sub_workflow_id") for task in normalized_tasks):
        raise WorkflowRoutingError(
            "REMOTE_BINDING_MISSING: 跨集群节点未提供生命周期管理器的实际实例绑定"
        )

    by_agent: Dict[str, List[Mapping[str, Any]]] = {}
    by_task: Dict[str, List[Mapping[str, Any]]] = {}
    for instance in runtime_instances:
        if instance.get("status") != "running":
            continue
        if instance.get("task_id"):
            by_task.setdefault(str(instance["task_id"]), []).append(instance)
        else:
            by_agent.setdefault(str(instance.get("agent_id", "")), []).append(instance)

    bound_instances: List[Mapping[str, Any]] = []
    for task in normalized_tasks:
        agent_id = str(task.get("assigned_agent_id", ""))
        task_id = str(task.get("task_id", ""))
        matches = by_task.get(task_id, []) or by_agent.get(agent_id, [])
        if not matches:
            raise WorkflowRoutingError(
                f"INSTANCE_MISSING: Agent {agent_id} 没有已订阅且运行中的实际实例"
            )
        if len(matches) > 1:
            raise WorkflowRoutingError(
                f"INSTANCE_AMBIGUOUS: Agent {agent_id} 匹配到多个已订阅且运行中的实例"
            )
        instance = matches[0]
        if not instance.get("instance_id") or not instance.get("cluster_id"):
            raise WorkflowRoutingError(
                f"INSTANCE_BINDING_INCOMPLETE: Agent {agent_id} 缺少 cluster_id 或 instance_id"
            )
        bound_instances.append(instance)

    route_keys = {"source_cluster", "target_cluster", "target_agent_id", "target_instance_id"}
    for index, task in enumerate(normalized_tasks):
        parameters = {
            key: value
            for key, value in dict(task.get("parameters") or {}).items()
            if key not in route_keys
        }
        if index > 0:
            parameters["source_cluster"] = bound_instances[index - 1]["cluster_id"]
        if index + 1 < len(normalized_tasks):
            target = bound_instances[index + 1]
            parameters.update({
                "target_cluster": target["cluster_id"],
                "target_agent_id": target["agent_id"],
                "target_instance_id": target["instance_id"],
            })
        task["parameters"] = parameters

    return normalized_tasks, plan_signature(normalized_tasks)


def assert_frozen_plan(
    tasks: Sequence[Mapping[str, Any]],
    frozen_signature: Sequence[Sequence[str]],
) -> None:
    if plan_signature(tasks) != tuple(tuple(item) for item in frozen_signature):
        raise WorkflowRoutingError(
            "FROZEN_ROUTE_CHANGED: 执行计划已被重规划修改，拒绝向未绑定实例发送请求"
        )
