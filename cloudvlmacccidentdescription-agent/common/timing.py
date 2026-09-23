from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator


def perf_counter_ms() -> float:
    return time.perf_counter_ns() / 1_000_000.0


@contextmanager
def record_time(target: dict[str, float], name: str) -> Iterator[None]:
    started = time.perf_counter_ns()
    try:
        yield
    finally:
        target[name] = round((time.perf_counter_ns() - started) / 1_000_000.0, 3)


def elapsed_ms(started_ns: int) -> float:
    return round((time.perf_counter_ns() - started_ns) / 1_000_000.0, 3)
