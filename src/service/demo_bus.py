"""
Demo Event Bus — 演示模式专用事件总线

仅在 DEMO_MODE=1 时激活。
工作流节点通过 get_demo_bus().publish() 注入结构化事件，
/demo/ws WebSocket 端点订阅并推送到浏览器前端，驱动可视化动画。
"""

import asyncio
import logging
import os
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

DEMO_MODE: bool = os.getenv("DEMO_MODE", "0") == "1"

# VehicleB 故障模拟状态（全局，跨请求共享）
_vehicleB_failed: bool = False


def set_vehicleB_failed(failed: bool) -> None:
    global _vehicleB_failed
    _vehicleB_failed = failed
    logger.info(f"[Demo] VehicleB 故障状态: {'开启' if failed else '关闭'}")


def is_vehicleB_failed() -> bool:
    return _vehicleB_failed


class DemoEventBus:
    """AsyncIO 广播事件总线 — 支持多订阅者 fan-out"""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """
        同步发布事件（可在 async 和非 async 上下文调用）。
        若 DEMO_MODE=0 则为空操作。
        """
        if not DEMO_MODE:
            return
        event = {"type": event_type, "payload": payload or {}}
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._broadcast_sync, event)
            else:
                logger.debug(f"[DemoEventBus] 无运行中事件循环，丢弃: {event_type}")
        except RuntimeError:
            logger.debug(f"[DemoEventBus] 无事件循环，丢弃: {event_type}")

    def _broadcast_sync(self, event: dict) -> None:
        """在事件循环线程中向所有订阅者广播（调用方已持有 GIL）"""
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 慢消费者，丢弃本次消息

    async def subscribe(self) -> AsyncIterator[dict]:
        """
        异步生成器，每个 WebSocket 连接调用一次。
        持续产出广播到本总线的事件，直到连接断开（finally 自动注销）。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._subscribers.append(q)
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


_bus: DemoEventBus | None = None


def get_demo_bus() -> DemoEventBus:
    global _bus
    if _bus is None:
        _bus = DemoEventBus()
    return _bus
