"""
QoS 监控与保障 (QoS Monitor)

运行层组件，负责：
- 记录 Agent 调用的延迟、成功/失败情况
- 提供统计查询接口（平均延迟、成功率等）
- 超过告警阈值时触发已注册的告警回调（供 ASD 调度反馈）
- 供资源 Agent 读取多智能体 QoS 日志数据，进行资源调度决策

对应接口文档：智能体运行层接口流程 §2 业务-资源智能体协同（QOS 模块）
"""
import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from src.runtime.models import QoSMetrics

logger = logging.getLogger(__name__)

# 默认告警阈值
DEFAULT_MAX_AVG_LATENCY_MS = 5000.0   # 平均延迟超过 5s 告警
DEFAULT_MIN_SUCCESS_RATE = 0.8        # 成功率低于 80% 告警


class QoSMonitor:
    """
    QoS 监控与保障

    核心接口：
    - record_call()       — 记录一次 Agent 调用（延迟 + 成功/失败）
    - get_metrics()       — 查询单个 Agent 的 QoS 指标
    - get_all_metrics()   — 查询所有 Agent 的 QoS 指标（供资源 Agent 读取）
    - check_threshold()   — 检查是否超过告警阈值
    - reset_metrics()     — 重置某 Agent 的统计
    """

    def __init__(
        self,
        max_avg_latency_ms: float = DEFAULT_MAX_AVG_LATENCY_MS,
        min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    ):
        # agent_id → QoSMetrics
        self._metrics: Dict[str, QoSMetrics] = {}
        self.max_avg_latency_ms = max_avg_latency_ms
        self.min_success_rate = min_success_rate
        # 告警回调列表：每个回调签名为 fn(agent_id: str, metrics: QoSMetrics)
        self._alert_callbacks: List[Callable[[str, QoSMetrics], None]] = []
        logger.info(
            f"QoSMonitor 初始化完成 "
            f"(max_latency={max_avg_latency_ms}ms, min_success_rate={min_success_rate})"
        )

    # ------------------------------------------------------------------
    # 告警回调注册
    # ------------------------------------------------------------------

    def register_alert_callback(
        self,
        fn: Callable[[str, QoSMetrics], None],
    ):
        """
        注册 QoS 告警回调。

        当 record_call() 触发阈值时，所有已注册回调均会被调用。
        典型用途：ALRE 注册回调 → 调用 AgentScheduler.redeploy_agent()

        Args:
            fn: 回调函数，签名 fn(agent_id: str, metrics: QoSMetrics)
        """
        self._alert_callbacks.append(fn)
        logger.info(
            f"[QoS] 注册告警回调: {getattr(fn, '__name__', repr(fn))}, "
            f"当前共 {len(self._alert_callbacks)} 个回调"
        )

    # ------------------------------------------------------------------
    # 记录接口
    # ------------------------------------------------------------------

    def record_call(
        self,
        agent_id: str,
        latency_ms: float,
        success: bool,
    ):
        """
        记录一次 Agent 调用

        Args:
            agent_id:   Agent 标识
            latency_ms: 本次调用延迟（毫秒）
            success:    是否成功
        """
        if agent_id not in self._metrics:
            self._metrics[agent_id] = QoSMetrics(agent_id=agent_id)

        m = self._metrics[agent_id]
        m.total_calls += 1
        m.total_latency_ms += latency_ms
        m.min_latency_ms = min(m.min_latency_ms, latency_ms)
        m.max_latency_ms = max(m.max_latency_ms, latency_ms)
        if success:
            m.success_count += 1
        else:
            m.failure_count += 1
        m.last_updated = datetime.utcnow().isoformat()

        logger.debug(
            f"[QoS] 记录调用: agent={agent_id}, "
            f"latency={latency_ms:.1f}ms, success={success}"
        )

        # 主动检查告警，触发时调用所有注册的回调
        if self.check_threshold(agent_id):
            logger.warning(
                f"[QoS] ⚠️  QoS 告警: agent_id={agent_id}, "
                f"avg_latency={m.avg_latency_ms:.1f}ms, "
                f"success_rate={m.success_rate:.1%}"
            )
            for cb in self._alert_callbacks:
                try:
                    cb(agent_id, m)
                except Exception as cb_err:
                    logger.warning(f"[QoS] 告警回调异常（已忽略）: {cb_err}")

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_metrics(self, agent_id: str) -> Optional[QoSMetrics]:
        """查询单个 Agent 的 QoS 指标"""
        return self._metrics.get(agent_id)

    def get_all_metrics(self) -> List[QoSMetrics]:
        """
        获取所有 Agent 的 QoS 指标

        供资源 Agent（SBOX2）读取，用于通信/计算资源调度决策。
        对应接口文档：SBOX2→QOS 读取多智能体 QoS 日志数据
        """
        return list(self._metrics.values())

    def get_metrics_dict(self) -> List[Dict]:
        """以字典形式返回所有指标（用于 API 响应）"""
        return [m.to_dict() for m in self._metrics.values()]

    def check_threshold(self, agent_id: str) -> bool:
        """
        检查指定 Agent 是否超过告警阈值

        规则：
        - 平均延迟 > max_avg_latency_ms
        - 成功率 < min_success_rate（且已有至少 5 次调用）

        Returns:
            True 表示告警触发
        """
        m = self._metrics.get(agent_id)
        if not m or m.total_calls == 0:
            return False

        if m.avg_latency_ms > self.max_avg_latency_ms:
            return True

        if m.total_calls >= 5 and m.success_rate < self.min_success_rate:
            return True

        return False

    def get_alert_agents(self) -> List[str]:
        """返回所有触发告警的 Agent ID 列表"""
        return [aid for aid in self._metrics if self.check_threshold(aid)]

    def reset_metrics(self, agent_id: str) -> bool:
        """重置某 Agent 的统计数据"""
        if agent_id in self._metrics:
            self._metrics[agent_id] = QoSMetrics(agent_id=agent_id)
            logger.info(f"[QoS] 重置指标: agent_id={agent_id}")
            return True
        return False

    def get_summary(self) -> Dict:
        """返回整体 QoS 概览"""
        metrics = list(self._metrics.values())
        if not metrics:
            return {"total_agents": 0, "alert_count": 0}

        return {
            "total_agents": len(metrics),
            "total_calls": sum(m.total_calls for m in metrics),
            "overall_success_rate": round(
                sum(m.success_count for m in metrics)
                / max(sum(m.total_calls for m in metrics), 1),
                4,
            ),
            "avg_latency_ms": round(
                sum(m.avg_latency_ms for m in metrics) / len(metrics), 2
            ),
            "alert_count": len(self.get_alert_agents()),
            "alert_agents": self.get_alert_agents(),
        }


# ======================================================================
# 单例访问
# ======================================================================
_monitor_instance: Optional[QoSMonitor] = None


def get_qos_monitor() -> QoSMonitor:
    """获取全局 QoSMonitor 单例"""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = QoSMonitor()
    return _monitor_instance
