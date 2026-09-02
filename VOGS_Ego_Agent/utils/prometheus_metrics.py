"""Agent Template 的分阶段时延采集范式。"""

import math
import os
import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Mapping, Optional

from prometheus_client import Counter, Gauge, Histogram, Summary


AGENT_ID = os.getenv("AGENT_ID", "agent-template")
INSTANCE_ID = (
    os.getenv("AGENT_INSTANCE_ID")
    or os.getenv("INSTANCE_ID")
    or os.getenv("HOSTNAME", f"{AGENT_ID}-local")
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
AGENT_NATS_INPUT_WAIT = Histogram(
    "agent_nats_input_wait_seconds",
    "Agent 等待 NATS 输入数据的时间。",
    _LABELS,
    buckets=_LATENCY_BUCKETS,
)
AGENT_EXECUTION = Histogram(
    "agent_execution_seconds",
    "Agent 核心业务逻辑执行时间。",
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

# 使用固定标签集合，避免请求内容产生高基数时间序列。metric_name 应是开发者
# 定义的稳定名称，例如 "miou"。
_PERFORMANCE_LABELS = ("agent_id", "instance_id", "metric_name")
_PERFORMANCE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

AGENT_PERFORMANCE = Summary(
    "agent_performance",
    "Agent 自定义性能指标的观测值。",
    _PERFORMANCE_LABELS,
)
AGENT_PERFORMANCE_LATEST = Gauge(
    "agent_performance_latest",
    "Agent 自定义性能指标最近一次观测值。",
    _PERFORMANCE_LABELS,
)


@dataclass
class AgentCallTiming:
    """单次 A2A 调用的服务端分阶段耗时，单位均为毫秒。"""

    schema_version: str = "1"
    agent_id: str = AGENT_ID
    instance_id: str = INSTANCE_ID
    task_id: str = "unknown"
    queue_wait_ms: float = 0.0
    nats_input_wait_ms: float = 0.0
    execution_ms: float = 0.0
    nats_output_publish_ms: float = 0.0
    server_total_ms: float = 0.0
    performance: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        values = asdict(self)
        # 性能指标作为 A2A metadata 中与 qos 并列的字段返回，不混入 QoS 结构。
        values.pop("performance", None)
        return {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in values.items()
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


def observe_performance_metric(metric_name: str, value: float) -> None:
    """上报一个性能指标，并记录到当前 A2A 调用的 metadata。"""
    if not isinstance(metric_name, str) or not _PERFORMANCE_NAME_PATTERN.fullmatch(
        metric_name
    ):
        raise ValueError(
            "performance metric name must match "
            "[a-zA-Z_][a-zA-Z0-9_]{0,63}"
        )
    if isinstance(value, bool):
        raise TypeError("performance metric value must be a finite number")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("performance metric value must be a finite number") from exc
    if not math.isfinite(numeric_value):
        raise ValueError("performance metric value must be finite")

    labels = {
        "agent_id": AGENT_ID,
        "instance_id": INSTANCE_ID,
        "metric_name": metric_name,
    }
    AGENT_PERFORMANCE.labels(**labels).observe(numeric_value)
    AGENT_PERFORMANCE_LATEST.labels(**labels).set(numeric_value)

    timing = get_current_timing()
    if timing is not None:
        timing.performance[metric_name] = numeric_value


def observe_performance_metrics(metrics: Mapping[str, float]) -> None:
    """批量上报同一次评估产生的多个性能指标。"""
    if not isinstance(metrics, Mapping):
        raise TypeError("performance metrics must be a mapping")
    for metric_name, value in metrics.items():
        observe_performance_metric(metric_name, value)


def observe_call(timing: AgentCallTiming, status: str) -> None:
    """在调用结束时一次性写入所有 Prometheus 指标。"""
    labels = {
        "agent_id": timing.agent_id,
        "instance_id": timing.instance_id,
        "status": status,
    }
    AGENT_CALLS.labels(**labels).inc()
    AGENT_QUEUE_WAIT.labels(**labels).observe(max(timing.queue_wait_ms, 0.0) / 1000)
    AGENT_NATS_INPUT_WAIT.labels(**labels).observe(max(timing.nats_input_wait_ms, 0.0) / 1000)
    AGENT_EXECUTION.labels(**labels).observe(max(timing.execution_ms, 0.0) / 1000)
    AGENT_NATS_OUTPUT_PUBLISH.labels(**labels).observe(
        max(timing.nats_output_publish_ms, 0.0) / 1000
    )
    AGENT_SERVER_TOTAL.labels(**labels).observe(max(timing.server_total_ms, 0.0) / 1000)
