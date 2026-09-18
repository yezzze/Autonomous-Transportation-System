"""运行层导出的 Prometheus 指标。

该模块只负责定义并更新 Prometheus 指标，不承担阈值判断和扩缩容决策。
这样可以保持职责边界：运行层提供可观测数据，Prometheus 计算告警条件，
编排层根据告警和资源预算决定后续动作。
"""

from prometheus_client import Counter, Histogram


# Counter 用于统计调用总数。status 标签区分成功和失败调用，便于 PromQL
# 计算指定时间窗口内的失败率。
AGENT_CALLS = Counter(
    "agent_calls_total",
    "运行层 QoS Monitor 观测到的 Agent 调用总数。",
    ("agent_id", "status"),
)

# Histogram 记录每次调用的耗时分布。Prometheus 会为每个 bucket 生成累计
# 计数，因此可使用 histogram_quantile() 计算 p50、p95 和 p99 等分位数。
AGENT_CALL_LATENCY = Histogram(
    "agent_call_latency_seconds",
    "运行层 QoS Monitor 观测到的 Agent 调用时延。",
    ("agent_id",),
    # Prometheus 时长指标统一使用秒。桶边界覆盖 50ms 到 60s 的 Agent 调用。
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

# A2A 调用方测量的完整往返时延与网络残差。instance_id 来自 Agent 响应；
# 旧版 Agent 或连接失败时使用 unknown，保持向后兼容。
A2A_TOTAL_LATENCY = Histogram(
    "a2a_total_latency_seconds",
    "A2A 调用方从发送请求到收到响应的完整往返时延。",
    ("agent_id", "instance_id", "status"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

A2A_NETWORK_LATENCY = Histogram(
    "a2a_network_seconds",
    "A2A 完整往返时延减去 Agent 服务端总耗时得到的网络残差。",
    ("agent_id", "instance_id", "status"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# 非 A2A 路径与编排端到端耗时使用独立指标，避免与 Agent 实例运行时延混合。
ORCHESTRATION_TASK_LATENCY = Histogram(
    "orchestration_task_latency_seconds",
    "编排层任务执行耗时。",
    ("execution_kind", "status"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)

# 一次完整应用工作流从进入冻结计划执行到成功、失败或取消的端到端耗时。
# app_id 与 workflow_handle 用于区分应用及其每次部署形成的工作流；
# deployment_mode 与 execution_mode 均为受控低基数标签。
APPLICATION_WORKFLOW_DURATION = Histogram(
    "application_workflow_duration_seconds",
    "完整应用工作流执行时延，不包含规划、部署和调度等待时间。",
    ("app_id", "workflow_handle", "deployment_mode", "execution_mode", "status"),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)


def observe_agent_call(agent_id: str, latency_ms: float, success: bool) -> None:
    """记录一次 Agent 调用，并向调用方隐藏 Prometheus 客户端实现细节。"""
    # 将布尔值转换为稳定的低基数标签，避免使用错误文本等高基数内容。
    status = "success" if success else "failure"
    AGENT_CALLS.labels(agent_id=agent_id, status=status).inc()
    # QoSMonitor 使用毫秒，而 Prometheus 时长指标约定使用秒；负数时延按 0 处理。
    AGENT_CALL_LATENCY.labels(agent_id=agent_id).observe(max(latency_ms, 0.0) / 1000)


def observe_a2a_call(
    *,
    agent_id: str,
    instance_id: str,
    status: str,
    total_latency_ms: float,
    network_ms: float | None,
) -> None:
    """记录调用方能够可靠测量的 A2A 完整 RTT 与可选网络残差。"""
    labels = {
        "agent_id": agent_id or "unknown",
        "instance_id": instance_id or "unknown",
        "status": status or "unknown",
    }
    A2A_TOTAL_LATENCY.labels(**labels).observe(max(total_latency_ms, 0.0) / 1000)
    if network_ms is not None:
        A2A_NETWORK_LATENCY.labels(**labels).observe(max(network_ms, 0.0) / 1000)


def observe_orchestration_task(
    *,
    execution_kind: str,
    status: str,
    latency_ms: float,
) -> None:
    """记录编排路径耗时，不将其误标记为 Agent 实例运行时延。"""
    allowed_kinds = {
        "builtin",
        "mcp",
        "a2a",
        "local_subworkflow",
        "remote_subworkflow",
        "error",
    }
    normalized_kind = execution_kind if execution_kind in allowed_kinds else "error"
    ORCHESTRATION_TASK_LATENCY.labels(
        execution_kind=normalized_kind,
        status=status or "unknown",
    ).observe(max(latency_ms, 0.0) / 1000)


def observe_application_workflow(
    *,
    app_id: str,
    workflow_handle: str,
    deployment_mode: str,
    execution_mode: str,
    status: str,
    latency_seconds: float,
) -> None:
    """记录一次完整应用工作流执行，标签值归一化以限制基数。"""
    normalized_deployment = (
        deployment_mode if deployment_mode in {"deploy_only", "standard"} else "standard"
    )
    normalized_execution = (
        execution_mode if execution_mode in {"single", "scheduled"} else "single"
    )
    normalized_status = status if status in {"success", "error", "cancelled"} else "error"
    APPLICATION_WORKFLOW_DURATION.labels(
        app_id=app_id or "unknown",
        workflow_handle=workflow_handle or "unknown",
        deployment_mode=normalized_deployment,
        execution_mode=normalized_execution,
        status=normalized_status,
    ).observe(max(latency_seconds, 0.0))
