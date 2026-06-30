"""Perception2IntermediateFeature Agent 的 Prometheus 时延指标。"""

import os
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Optional

from prometheus_client import Counter, Histogram


AGENT_ID = os.getenv("AGENT_ID", "perception2intermediatefeature-agent")
INSTANCE_ID = os.getenv("INSTANCE_ID") or os.getenv(
    "HOSTNAME",
    f"{AGENT_ID}-local",
)

_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 4, 5, 7.5, 10, 30, 60)
_LABELS = ("agent_id", "instance_id", "status")

AGENT_CALLS = Counter(
    "agent_calls_total",
    "Agent 实例处理的 A2A 调用总数。",
    _LABELS,
)
AGENT_QUEUE_WAIT = Histogram(
    "agent_queue_wait_seconds",
    "A2A 请求等待 Agent 实例执行槽的时间。",
    _LABELS,
    buckets=_LATENCY_BUCKETS,
)
AGENT_MCP_FETCH = Histogram(
    "agent_mcp_fetch_seconds",
    "Agent 从 MCP 获取点云感知数据的时间。",
    _LABELS,
    buckets=_LATENCY_BUCKETS,
)
AGENT_EXECUTION = Histogram(
    "agent_execution_seconds",
    "Agent 解码输入并执行核心模型推理的时间。",
    _LABELS,
    buckets=_LATENCY_BUCKETS,
)
AGENT_NATS_OUTPUT_PUBLISH = Histogram(
    "agent_nats_output_publish_seconds",
    "Agent 向 NATS 发布输出数据的时间。",
    _LABELS,
    buckets=_LATENCY_BUCKETS,
)
AGENT_SERVER_TOTAL = Histogram(
    "agent_server_total_seconds",
    "A2A 请求进入 Agent 端点到服务端完成处理的总时间。",
    _LABELS,
    buckets=_LATENCY_BUCKETS,
)


@dataclass
class AgentCallTiming:
    """单次 A2A 调用的服务端分阶段耗时，单位均为毫秒。"""

    schema_version: str = "1"
    agent_id: str = AGENT_ID
    instance_id: str = INSTANCE_ID
    task_id: str = "unknown"
    queue_wait_ms: float = 0.0
    mcp_fetch_ms: float = 0.0
    execution_ms: float = 0.0
    nats_output_publish_ms: float = 0.0
    server_total_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in asdict(self).items()
        }


_current_timing: ContextVar[Optional[AgentCallTiming]] = ContextVar(
    "agent_call_timing",
    default=None,
)


def set_current_timing(timing: AgentCallTiming):
    """绑定当前异步请求的时延记录，并返回用于恢复上下文的 token。"""
    return _current_timing.set(timing)


def reset_current_timing(token) -> None:
    """恢复请求进入前的时延上下文。"""
    _current_timing.reset(token)


def get_current_timing() -> Optional[AgentCallTiming]:
    """获取当前异步请求的时延记录。"""
    return _current_timing.get()


def observe_call(timing: AgentCallTiming, status: str) -> None:
    """在调用结束时一次性写入所有 Prometheus 指标。"""
    labels = {
        "agent_id": timing.agent_id,
        "instance_id": timing.instance_id,
        "status": status,
    }
    AGENT_CALLS.labels(**labels).inc()
    AGENT_QUEUE_WAIT.labels(**labels).observe(max(timing.queue_wait_ms, 0.0) / 1000)
    AGENT_MCP_FETCH.labels(**labels).observe(max(timing.mcp_fetch_ms, 0.0) / 1000)
    AGENT_EXECUTION.labels(**labels).observe(max(timing.execution_ms, 0.0) / 1000)
    AGENT_NATS_OUTPUT_PUBLISH.labels(**labels).observe(
        max(timing.nats_output_publish_ms, 0.0) / 1000
    )
    AGENT_SERVER_TOTAL.labels(**labels).observe(max(timing.server_total_ms, 0.0) / 1000)
