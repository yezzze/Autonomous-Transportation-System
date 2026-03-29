"""
QoS → ASD 反馈闭环集成测试

测试场景：
  T1 - 5 次失败调用触发 QoS 告警 → redeploy_agent 被调用
  T2 - redeploy 成功后 reset_metrics，新实例不再立即触发告警
  T3 - 冷却窗口内重复失败 → 回调只触发一次 redeploy（防 storm）
  T4 - 全部成功调用 → check_threshold=False → no redeploy
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import pytest
from unittest.mock import MagicMock, patch

from src.runtime.qos_monitor import QoSMonitor
from src.service.agent_scheduler import AgentScheduler, DeploymentRecord


# ============================================================
# 辅助函数
# ============================================================

def _make_monitor(max_latency=5000.0, min_success_rate=0.8) -> QoSMonitor:
    """每个测试独立创建 QoSMonitor，避免全局单例状态污染"""
    return QoSMonitor(
        max_avg_latency_ms=max_latency,
        min_success_rate=min_success_rate,
    )


def _fake_record(scheduler: AgentScheduler, agent_id: str) -> DeploymentRecord:
    """向 scheduler 注入一条假部署记录，使 redeploy_agent 可以找到历史记录"""
    import uuid as _uuid
    dep_id = f"dep-{_uuid.uuid4().hex[:8]}"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        agent_id=agent_id,
        image_id="langmanus/agent:latest",
        node_id="localhost",
        cpu_cores=1.0,
        memory_mb=512,
        status="running",
    )
    scheduler._deployments[dep_id] = rec
    scheduler._agent_index.setdefault(agent_id, []).append(dep_id)
    return rec


# ============================================================
# T1 — 5 次失败调用触发告警 → redeploy_agent 被调用
# ============================================================

def test_qos_alert_triggers_redeploy():
    """
    record_call() 连续 5 次失败后 check_threshold=True，
    注册的回调应触发 AgentScheduler.redeploy_agent()。
    """
    monitor = _make_monitor(min_success_rate=0.8)
    agent_id = "search_agent_001"

    redeploy_calls = []

    def _cb(aid, metrics):
        redeploy_calls.append(aid)

    monitor.register_alert_callback(_cb)

    # 前 4 次失败（total_calls < 5，不触发告警）
    for _ in range(4):
        monitor.record_call(agent_id, latency_ms=100.0, success=False)

    assert len(redeploy_calls) == 0, "少于 5 次调用不应触发告警"

    # 第 5 次失败触发告警
    monitor.record_call(agent_id, latency_ms=100.0, success=False)

    assert len(redeploy_calls) == 1
    assert redeploy_calls[0] == agent_id


# ============================================================
# T2 — redeploy 后 reset_metrics，新实例不再立即触发告警
# ============================================================

def test_redeploy_resets_qos_metrics():
    """
    AgentScheduler.redeploy_agent() 成功后应调用 get_qos_monitor().reset_metrics(agent_id)，
    使 check_threshold 重新变为 False。
    用 mock 的 get_qos_monitor 验证 reset_metrics 被调用，且调用后阈值不再触发。
    """
    monitor = _make_monitor(min_success_rate=0.8)
    agent_id = "compute_agent_002"

    # 连续失败让 monitor 进入告警状态
    for _ in range(5):
        monitor.record_call(agent_id, latency_ms=100.0, success=False)

    assert monitor.check_threshold(agent_id) is True, "初始应处于告警状态"

    # 调用 reset_metrics（模拟 redeploy 后操作）
    monitor.reset_metrics(agent_id)

    assert monitor.check_threshold(agent_id) is False, "reset 后告警应清除"
    # reset_metrics 将指标归零（total_calls=0），check_threshold 因此返回 False
    m = monitor.get_metrics(agent_id)
    assert m is not None and m.total_calls == 0, "reset 后 total_calls 应为 0"


def test_redeploy_agent_calls_reset_metrics():
    """
    实际 AgentScheduler.redeploy_agent() 成功时，
    验证它会调用 get_qos_monitor().reset_metrics(agent_id)。
    """
    agent_id = "nlp_agent_003"
    scheduler = AgentScheduler()
    _fake_record(scheduler, agent_id)

    mock_monitor = MagicMock()

    with patch("src.runtime.qos_monitor.get_qos_monitor", return_value=mock_monitor):
        # deploy_agent 内部会启动 subprocess，用 mock 绕过
        with patch.object(scheduler, "deploy_agent") as mock_deploy:
            fake_record = _fake_record(scheduler, agent_id)
            mock_deploy.return_value = fake_record
            with patch.object(scheduler, "shutdown_agent"):
                scheduler.redeploy_agent(agent_id)

    mock_monitor.reset_metrics.assert_called_once_with(agent_id)


# ============================================================
# T3 — 冷却窗口内重复告警只触发一次 redeploy
# ============================================================

def test_cooldown_prevents_redeploy_storm():
    """
    _qos_redeploy_cb 在冷却期（60s）内收到多次告警时，只应触发一次 redeploy。
    通过直接测试回调闭包逻辑来验证，不启动 FastAPI。
    """
    import threading as _threading

    cooldown_secs = 60
    _redeploy_cooldown: dict = {}
    redeploy_calls = []

    def _qos_redeploy_cb(agent_id: str, metrics) -> None:
        """与 agent_server._start_qos_asd_feedback 中等效的回调逻辑"""
        now = time.monotonic()
        last = _redeploy_cooldown.get(agent_id, 0.0)
        if now - last < cooldown_secs:
            return
        _redeploy_cooldown[agent_id] = now

        def _do():
            redeploy_calls.append(agent_id)

        _threading.Thread(target=_do, daemon=True).start()

    monitor = _make_monitor(min_success_rate=0.8)
    agent_id = "vision_agent_004"
    monitor.register_alert_callback(_qos_redeploy_cb)

    # 触发第一次告警（5 次失败）
    for _ in range(5):
        monitor.record_call(agent_id, latency_ms=100.0, success=False)

    # 继续失败，此时每次 record_call 都触发告警检查，但冷却窗口内应跳过
    for _ in range(5):
        monitor.record_call(agent_id, latency_ms=100.0, success=False)

    # 等待后台线程完成
    time.sleep(0.05)

    assert len(redeploy_calls) == 1, (
        f"冷却窗口内应只触发 1 次 redeploy，实际触发 {len(redeploy_calls)} 次"
    )


# ============================================================
# T4 — 全部成功调用 → no alert → no redeploy
# ============================================================

def test_no_alert_on_healthy_agent():
    """
    成功率 100% 且延迟正常时，check_threshold 应始终为 False，回调不触发。
    """
    monitor = _make_monitor(max_latency=5000.0, min_success_rate=0.8)
    agent_id = "code_agent_005"

    alert_fired = []
    monitor.register_alert_callback(lambda aid, m: alert_fired.append(aid))

    for _ in range(10):
        monitor.record_call(agent_id, latency_ms=50.0, success=True)

    assert monitor.check_threshold(agent_id) is False
    assert len(alert_fired) == 0, "健康 Agent 不应触发告警"
