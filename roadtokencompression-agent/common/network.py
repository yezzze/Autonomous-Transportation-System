from __future__ import annotations

import time
from typing import Any

from common.timing import record_time


def transfer_delay_ms(
    num_bytes: int,
    *,
    bandwidth_mbps: float,
    one_way_latency_ms: float,
) -> float:
    """计算单向链路的固定传播时延与串行化时延。"""
    if num_bytes < 0:
        raise ValueError("传输字节数不得小于0")
    if bandwidth_mbps <= 0:
        raise ValueError("链路带宽必须大于0")
    if one_way_latency_ms < 0:
        raise ValueError("链路固定时延不得小于0")
    serialization_ms = num_bytes * 8.0 / (
        bandwidth_mbps * 1_000_000.0
    ) * 1000.0
    return one_way_latency_ms + serialization_ms


def simulate_transfer(
    timings: dict[str, float],
    key: str,
    num_bytes: int,
    link_config: dict[str, Any],
) -> None:
    delay_ms = transfer_delay_ms(
        num_bytes,
        bandwidth_mbps=float(link_config["bandwidth_mbps"]),
        one_way_latency_ms=float(link_config["one_way_latency_ms"]),
    )
    timings[f"{key}_configured"] = round(delay_ms, 3)
    with record_time(timings, key):
        time.sleep(delay_ms / 1000.0)
