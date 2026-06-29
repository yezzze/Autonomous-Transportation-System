"""编排层 Alertmanager webhook 告警接收器。

Alertmanager 可能因为网络故障重复投递同一告警，因此接收器使用告警
fingerprint 作为稳定主键，保存每条告警的最新状态。
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class AlertmanagerReceiver:
    """保存最新告警状态，并为后续编排策略控制器提供统一读取入口。"""

    def __init__(self) -> None:
        # fingerprint -> 最新告警内容。当前是单进程内存实现，适合单副本验证。
        self._alerts: Dict[str, Dict[str, Any]] = {}
        # FastAPI 可能并发处理多个 webhook 请求，锁用于保护字典读写一致性。
        self._lock = threading.Lock()

    def receive(self, payload: Dict[str, Any]) -> Dict[str, int]:
        """
        幂等应用一份 Alertmanager webhook 请求。

        Alertmanager 可能重试投递，因此使用每条告警携带的 fingerprint
        作为稳定标识；重复请求只会覆盖同一条记录，不会产生重复告警。
        """
        accepted = 0
        resolved = 0
        # 一个 webhook 请求中可能包含同一分组下的多条告警。
        alerts = payload.get("alerts", [])

        with self._lock:
            for alert in alerts:
                fingerprint = alert.get("fingerprint")
                if not fingerprint:
                    # 缺少 fingerprint 时无法保证幂等性，因此拒绝保存该条告警。
                    logger.warning("[Alertmanager] ignored alert without fingerprint")
                    continue

                # 优先使用单条告警状态；兼容只在请求顶层提供状态的 payload。
                status = alert.get("status", payload.get("status", "firing"))
                record = {
                    **alert,
                    "status": status,
                    # 记录编排层实际收到告警的时间，便于排查通知链路延迟。
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
                # 同一 fingerprint 再次到达时覆盖旧状态，实现幂等更新。
                self._alerts[fingerprint] = record
                accepted += 1
                resolved += int(status == "resolved")

                # 记录策略控制器最关心的标签，但不在接收路径中直接执行扩缩容。
                labels = alert.get("labels", {})
                logger.warning(
                    "[Alertmanager] status=%s alert=%s agent=%s action=%s fingerprint=%s",
                    status,
                    labels.get("alertname", "unknown"),
                    labels.get("agent_id", "unknown"),
                    labels.get("action", "none"),
                    fingerprint,
                )

        return {"accepted": accepted, "resolved": resolved}

    def list_alerts(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """返回编排层已知告警的最新状态，可选择只返回仍在触发的告警。"""
        with self._lock:
            # 在锁内复制快照，避免调用方遍历时字典被 webhook 请求修改。
            alerts = list(self._alerts.values())
        if active_only:
            alerts = [alert for alert in alerts if alert["status"] == "firing"]
        return alerts

    def reset(self) -> None:
        """清空接收器状态，仅用于隔离测试用例。"""
        with self._lock:
            self._alerts.clear()


# 进程内单例确保 API 写入和策略控制器读取的是同一个告警状态集合。
_receiver = AlertmanagerReceiver()


def get_alertmanager_receiver() -> AlertmanagerReceiver:
    """获取编排层共享的 Alertmanager 告警接收器。"""
    return _receiver
