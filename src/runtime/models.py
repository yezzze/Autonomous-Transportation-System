"""
运行层数据模型

对应架构图中的智能体运行层（Runtime）组件的核心数据结构：
- AgentInstance:   运行中的 Agent 实例（ALCM 管理单元）
- ResourceConfig:  Agent 实例的资源配置
- QoSMetrics:      Agent 调用的 QoS 统计指标
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional


# ======================================================================
# 资源配置
# ======================================================================

@dataclass
class ResourceConfig:
    """Agent 实例的资源配置"""
    cpu_cores: float = 1.0      # CPU 核心数
    memory_mb: int = 512        # 内存分配（MB）
    node_id: str = "localhost"  # 部署节点 ID
    gpu_count: int = 0          # GPU 数量（可选）

    def to_dict(self) -> Dict:
        return asdict(self)


# ======================================================================
# Agent 实例（ALCM 管理单元）
# ======================================================================

@dataclass
class AgentInstance:
    """
    运行中的 Agent 实例

    ALCM（生命周期管理）的核心管理单元。
    通过引用计数（ref_count）决定是否可以关闭。

    生命周期：deploying → running → stopping → stopped
    """
    instance_id: str
    agent_id: str
    image_id: str
    status: str = "deploying"       # deploying | running | stopping | stopped | error
    ref_count: int = 0              # 工作流订阅引用计数
    resource_config: ResourceConfig = field(default_factory=ResourceConfig)
    # 订阅此实例的工作流 ID 集合
    subscribed_workflows: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @classmethod
    def create(
        cls,
        agent_id: str,
        image_id: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> "AgentInstance":
        instance_id = f"inst_{agent_id}_{uuid.uuid4().hex[:6]}"
        return cls(
            instance_id=instance_id,
            agent_id=agent_id,
            image_id=image_id,
            resource_config=resource_config or ResourceConfig(),
        )

    def subscribe(self, workflow_id: str):
        """工作流订阅此实例，引用计数 +1"""
        if workflow_id not in self.subscribed_workflows:
            self.subscribed_workflows.append(workflow_id)
            self.ref_count += 1
            self.updated_at = datetime.utcnow().isoformat()

    def unsubscribe(self, workflow_id: str) -> int:
        """工作流退订此实例，引用计数 -1，返回剩余引用数"""
        if workflow_id in self.subscribed_workflows:
            self.subscribed_workflows.remove(workflow_id)
            self.ref_count = max(0, self.ref_count - 1)
            self.updated_at = datetime.utcnow().isoformat()
        return self.ref_count

    def set_status(self, status: str):
        self.status = status
        self.updated_at = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict:
        return {
            "instance_id": self.instance_id,
            "agent_id": self.agent_id,
            "image_id": self.image_id,
            "status": self.status,
            "ref_count": self.ref_count,
            "subscribed_workflows": self.subscribed_workflows,
            "resource_config": self.resource_config.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ======================================================================
# QoS 统计指标
# ======================================================================

@dataclass
class QoSMetrics:
    """
    Agent 调用的 QoS 统计指标

    用于 QoS 监控组件（QoSMonitor）的统计单元。
    """
    agent_id: str
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    last_updated: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_calls if self.total_calls > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total_calls if self.total_calls > 0 else 0.0

    def to_dict(self) -> Dict:
        return {
            "agent_id": self.agent_id,
            "total_calls": self.total_calls,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "min_latency_ms": self.min_latency_ms if self.min_latency_ms != float("inf") else 0,
            "max_latency_ms": self.max_latency_ms,
            "last_updated": self.last_updated,
        }
