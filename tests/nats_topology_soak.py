#!/usr/bin/env python3
"""Core NATS local/global 大帧请求响应稳定性测试。"""

import argparse
import asyncio
import json
import math
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_api import NatsBinaryMessage, NatsComm  # noqa: E402


MIB = 1024 * 1024
RESPONSE_STRUCT = struct.Struct("!QQ")
MONITORED_COUNTERS = ("in_msgs", "out_msgs", "in_bytes", "out_bytes")


def emit(event: Mapping[str, Any]) -> None:
    print(
        json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def percentile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def monitor_endpoint(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def fetch_json(url: str, timeout_sec: float) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "nats-topology-soak"},
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        return json.loads(response.read().decode("utf-8"))


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def connection_metrics(connz: Mapping[str, Any]) -> Dict[str, int]:
    connections = connz.get("connections") or []
    pending_values = [integer(item.get("pending_bytes")) for item in connections]
    pending_messages = [integer(item.get("pending_msgs")) for item in connections]
    subscriptions = []
    for item in connections:
        value = item.get("subscriptions", item.get("num_subscriptions", 0))
        subscriptions.append(len(value) if isinstance(value, list) else integer(value))
    return {
        "connections": integer(connz.get("num_connections", len(connections))),
        "pending_bytes": sum(pending_values),
        "max_pending_bytes": max(pending_values, default=0),
        "pending_msgs": sum(pending_messages),
        "subscriptions": sum(subscriptions),
    }


async def fetch_node_metrics(
    name: str,
    monitor_url: str,
    timeout_sec: float,
) -> Tuple[str, Dict[str, Any]]:
    varz_url = monitor_endpoint(monitor_url, "/varz")
    connz_url = monitor_endpoint(monitor_url, "/connz?subs=1")
    try:
        varz, connz = await asyncio.gather(
            asyncio.to_thread(fetch_json, varz_url, timeout_sec),
            asyncio.to_thread(fetch_json, connz_url, timeout_sec),
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return name, {
            "monitor_error": f"{type(exc).__name__}: {exc}",
            "monitor_url": monitor_url,
        }

    metrics = {
        "server_name": varz.get("server_name", name),
        "in_msgs": integer(varz.get("in_msgs")),
        "out_msgs": integer(varz.get("out_msgs")),
        "in_bytes": integer(varz.get("in_bytes")),
        "out_bytes": integer(varz.get("out_bytes")),
        "slow_consumers": integer(varz.get("slow_consumers")),
        "mem": integer(varz.get("mem")),
        "leafnodes": integer(varz.get("leafnodes")),
    }
    metrics.update(connection_metrics(connz))
    return name, metrics


async def take_snapshot(
    monitors: Mapping[str, str],
    timeout_sec: float,
) -> Dict[str, Dict[str, Any]]:
    results = await asyncio.gather(
        *(
            fetch_node_metrics(name, monitor_url, timeout_sec)
            for name, monitor_url in monitors.items()
        )
    )
    return dict(results)


def snapshot_delta(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = {}
    for node in before:
        result[node] = {}
        for key in MONITORED_COUNTERS:
            result[node][key] = max(
                0,
                integer(after.get(node, {}).get(key))
                - integer(before.get(node, {}).get(key)),
            )
    return result


def client_pending_bytes(comm: NatsComm) -> int:
    value = getattr(comm._nc, "pending_data_size", 0)
    if callable(value):
        value = value()
    return integer(value)


@dataclass
class ResponderState:
    received: int = 0
    handled: int = 0
    errors: int = 0
    last_sequence: Optional[int] = None
    last_received_at: Optional[float] = None


@dataclass
class RequestState:
    status: str = "idle"
    sequence: Optional[int] = None
    request_started_at: Optional[float] = None
    last_completed_at: Optional[float] = None
    client_pending_bytes: int = 0


@dataclass
class PhaseResult:
    phase: str
    subject: str
    attempted: int
    succeeded: int
    errors: List[str]
    elapsed_sec: float
    latencies_ms: List[float]
    responder_received: int
    responder_handled: int
    monitor_errors: int
    byte_delta: Dict[str, Dict[str, int]]
    route_checks: List[Dict[str, Any]] = field(default_factory=list)
    diagnoses: List[str] = field(default_factory=list)

    @property
    def actual_fps(self) -> float:
        return self.succeeded / self.elapsed_sec if self.elapsed_sec else 0.0

    @property
    def passed(self) -> bool:
        return (
            not self.errors
            and self.monitor_errors == 0
            and all(check["passed"] for check in self.route_checks)
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "type": "phase_summary",
            "phase": self.phase,
            "passed": self.passed,
            "subject": self.subject,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "errors": len(self.errors),
            "error_examples": self.errors[:10],
            "elapsed_sec": round(self.elapsed_sec, 3),
            "actual_fps": round(self.actual_fps, 3),
            "latency_ms": {
                "p50": round(percentile(self.latencies_ms, 0.50), 3),
                "p95": round(percentile(self.latencies_ms, 0.95), 3),
                "p99": round(percentile(self.latencies_ms, 0.99), 3),
                "max": round(max(self.latencies_ms, default=0.0), 3),
            },
            "responder_received": self.responder_received,
            "responder_handled": self.responder_handled,
            "monitor_errors": self.monitor_errors,
            "byte_delta": self.byte_delta,
            "route_checks": self.route_checks,
            "diagnoses": self.diagnoses,
        }


class MonitorSampler:
    def __init__(
        self,
        monitors: Mapping[str, str],
        interval_sec: float,
        timeout_sec: float,
        requester: NatsComm,
        request_state: RequestState,
    ) -> None:
        self.monitors = monitors
        self.interval_sec = interval_sec
        self.timeout_sec = timeout_sec
        self.requester = requester
        self.request_state = request_state
        self.monitor_errors = 0
        self.latest: Dict[str, Dict[str, Any]] = {}
        self.max_client_pending_bytes = 0
        self.max_server_pending_bytes = {name: 0 for name in monitors}

    async def run(self, phase: str, stop: asyncio.Event, started: float) -> None:
        while not stop.is_set():
            sample_started = time.monotonic()
            nodes = await take_snapshot(self.monitors, self.timeout_sec)
            pending = client_pending_bytes(self.requester)
            self.request_state.client_pending_bytes = pending
            self.max_client_pending_bytes = max(self.max_client_pending_bytes, pending)
            for name, metrics in nodes.items():
                if "monitor_error" in metrics:
                    self.monitor_errors += 1
                self.max_server_pending_bytes[name] = max(
                    self.max_server_pending_bytes[name],
                    integer(metrics.get("max_pending_bytes")),
                )
            self.latest = nodes
            emit(
                {
                    "type": "monitor_sample",
                    "phase": phase,
                    "timestamp": time.time(),
                    "elapsed_sec": round(time.monotonic() - started, 3),
                    "request": {
                        "status": self.request_state.status,
                        "sequence": self.request_state.sequence,
                        "waiting_sec": (
                            round(
                                time.monotonic()
                                - self.request_state.request_started_at,
                                3,
                            )
                            if self.request_state.request_started_at is not None
                            and self.request_state.status == "waiting_reply"
                            else 0.0
                        ),
                        "client_pending_bytes": pending,
                    },
                    "nodes": nodes,
                }
            )
            remaining = self.interval_sec - (time.monotonic() - sample_started)
            if remaining <= 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass


def build_route_checks(
    phase: str,
    succeeded: int,
    payload_bytes: int,
    delta: Mapping[str, Mapping[str, int]],
    minimum_ratio: float,
    local_leak_ratio: float,
) -> List[Dict[str, Any]]:
    expected = succeeded * payload_bytes
    if expected <= 0:
        return [
            {
                "name": "有成功请求可供流量校验",
                "passed": False,
                "actual_bytes": 0,
                "required_bytes": payload_bytes,
            }
        ]

    def traffic(node: str) -> int:
        metrics = delta.get(node, {})
        return max(integer(metrics.get("in_bytes")), integer(metrics.get("out_bytes")))

    checks: List[Dict[str, Any]] = []
    if phase == "local":
        minimum = int(expected * minimum_ratio)
        leak_limit = int(expected * local_leak_ratio)
        checks.append(
            {
                "name": "local 数据经过 edge-a",
                "passed": traffic("edge_a") >= minimum,
                "actual_bytes": traffic("edge_a"),
                "required_bytes": minimum,
            }
        )
        for node in ("cloud", "edge_b"):
            checks.append(
                {
                    "name": f"local 数据未传播到 {node}",
                    "passed": traffic(node) <= leak_limit,
                    "actual_bytes": traffic(node),
                    "maximum_bytes": leak_limit,
                }
            )
    else:
        minimum = int(expected * minimum_ratio)
        for node in ("edge_a", "cloud", "edge_b"):
            checks.append(
                {
                    "name": f"global 数据经过 {node}",
                    "passed": traffic(node) >= minimum,
                    "actual_bytes": traffic(node),
                    "required_bytes": minimum,
                }
            )
    return checks


def diagnose(
    phase: str,
    result: PhaseResult,
    sampler: MonitorSampler,
    responder: ResponderState,
    payload_bytes: int,
) -> List[str]:
    diagnoses: List[str] = []
    delta = result.byte_delta

    def traffic(node: str) -> int:
        metrics = delta.get(node, {})
        return max(integer(metrics.get("in_bytes")), integer(metrics.get("out_bytes")))

    pending_threshold = max(MIB, payload_bytes // 2)
    backlog_threshold = payload_bytes * 2
    if sampler.max_client_pending_bytes >= backlog_threshold:
        diagnoses.append(
            "发送端 NATS 客户端 pending 持续堆积，优先检查 edge-a 写入带宽、"
            "客户端 pending_size 和连接重连状态"
        )

    backlog_nodes = [
        name
        for name, pending in sampler.max_server_pending_bytes.items()
        if pending >= backlog_threshold
    ]
    if backlog_nodes:
        diagnoses.append(
            "NATS 服务端连接 pending 超过两帧（"
            + ",".join(backlog_nodes)
            + "），对应下游连接消费或网络发送速度不足"
        )
    transient_nodes = [
        name
        for name, pending in sampler.max_server_pending_bytes.items()
        if pending_threshold <= pending < backlog_threshold
    ]
    if transient_nodes and not backlog_nodes:
        diagnoses.append(
            "监控采样命中一条帧正在发送时的瞬时 pending（"
            + ",".join(transient_nodes)
            + "），峰值未超过两帧，不属于队列累积"
        )

    if result.errors:
        if traffic("edge_a") < payload_bytes:
            diagnoses.append(
                "请求数据未完整进入 edge-a，最可能卡在发送端连接、客户端缓冲或 edge-a 接收"
            )
        elif phase == "global" and traffic("cloud") < payload_bytes:
            diagnoses.append(
                "edge-a 已收到请求但 cloud 未出现对应流量，最可能卡在 Edge LeafNode 到 Hub 链路"
            )
        elif phase == "global" and traffic("edge_b") < payload_bytes:
            diagnoses.append(
                "cloud 已出现请求流量但 edge-b 未出现对应流量，最可能卡在 Hub 路由或 edge-b LeafNode"
            )
        elif responder.received < result.attempted:
            diagnoses.append(
                "数据已到达目标 NATS，但接收处理计数不足，优先检查目标 subject、订阅是否在线和订阅 pending"
            )
        elif responder.handled >= result.succeeded:
            diagnoses.append(
                "接收端已处理请求但发送端仍有失败，最可能卡在 reply inbox 的返回链路或请求超时设置"
            )

    failed_checks = [check["name"] for check in result.route_checks if not check["passed"]]
    if failed_checks:
        diagnoses.append("流量路径校验失败：" + "；".join(failed_checks))
    if result.monitor_errors:
        diagnoses.append(
            "监控接口采样失败，无法完整判断流量路径；检查三端 HTTP monitor URL 和 /varz、/connz 可达性"
        )
    if not diagnoses and result.passed:
        diagnoses.append("未发现 pending 堆积、路由泄漏或请求响应停顿")
    return diagnoses


async def register_responder(
    comm: NatsComm,
    cluster_id: str,
    agent_id: str,
    operation: str,
    queue: str,
    process_delay_ms: float,
    state: ResponderState,
) -> Tuple[Any, Any]:
    async def handler(message: NatsBinaryMessage) -> bytes:
        state.received += 1
        state.last_received_at = time.monotonic()
        try:
            if len(message.data) < RESPONSE_STRUCT.size:
                raise ValueError(f"帧长度过小: {len(message.data)}")
            sequence = struct.unpack_from("!Q", message.data, 0)[0]
            state.last_sequence = sequence
            if process_delay_ms:
                await asyncio.sleep(process_delay_ms / 1000.0)
            state.handled += 1
            return RESPONSE_STRUCT.pack(sequence, len(message.data))
        except Exception:
            state.errors += 1
            raise

    subscriptions = await comm.subscribe_frame_bytes(
        agent_id=agent_id,
        handler=handler,
        operation=operation,
        local_cluster=cluster_id,
        queue=queue,
        max_inflight=1,
    )
    emit(
        {
            "type": "responder_ready",
            "cluster": cluster_id,
            "servers": comm.servers,
            "subjects": comm.frame_subscription_subjects(
                agent_id=agent_id,
                operation=operation,
                local_cluster=cluster_id,
            ),
        }
    )
    return subscriptions


async def run_phase(
    phase: str,
    requester: NatsComm,
    target_cluster: str,
    source_cluster: str,
    responder: ResponderState,
    monitors: Mapping[str, str],
    args: argparse.Namespace,
    sequence_start: int,
) -> Tuple[PhaseResult, int]:
    subject = requester.frame_subject(
        target_cluster=target_cluster,
        agent_id=args.agent_id,
        operation=args.operation,
        local_cluster=source_cluster,
    )
    request_state = RequestState()
    sampler = MonitorSampler(
        monitors=monitors,
        interval_sec=args.sample_interval_sec,
        timeout_sec=args.monitor_timeout_sec,
        requester=requester,
        request_state=request_state,
    )
    before = await take_snapshot(monitors, args.monitor_timeout_sec)
    baseline_errors = sum(
        1
        for metrics in before.values()
        if "monitor_error" in metrics
    )
    emit(
        {
            "type": "phase_start",
            "phase": phase,
            "subject": subject,
            "payload_bytes": args.payload_bytes,
            "duration_sec": args.duration_sec,
            "max_fps": args.fps,
            "baseline": before,
        }
    )

    stop_sampling = asyncio.Event()
    phase_started = time.monotonic()
    sample_task = asyncio.create_task(
        sampler.run(phase, stop_sampling, phase_started)
    )
    deadline = phase_started + args.duration_sec
    next_send_at = phase_started
    sequence = sequence_start
    attempted = 0
    succeeded = 0
    errors: List[str] = []
    latencies_ms: List[float] = []
    payload = bytearray(args.payload_bytes)

    try:
        while time.monotonic() < deadline:
            delay = next_send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if time.monotonic() >= deadline:
                break

            struct.pack_into("!Q", payload, 0, sequence)
            attempted += 1
            request_started = time.monotonic()
            request_state.status = "waiting_reply"
            request_state.sequence = sequence
            request_state.request_started_at = request_started
            try:
                response = await requester.request_frame_bytes(
                    target_cluster=target_cluster,
                    agent_id=args.agent_id,
                    payload=payload,
                    operation=args.operation,
                    local_cluster=source_cluster,
                    timeout_sec=args.timeout_sec,
                )
                if len(response) != RESPONSE_STRUCT.size:
                    raise ValueError(f"回复长度错误: {len(response)}")
                response_sequence, response_size = RESPONSE_STRUCT.unpack(response)
                if response_sequence != sequence:
                    raise ValueError(
                        f"回复序号错误: expected={sequence} actual={response_sequence}"
                    )
                if response_size != args.payload_bytes:
                    raise ValueError(
                        f"回复帧长度错误: expected={args.payload_bytes} actual={response_size}"
                    )
                succeeded += 1
                latencies_ms.append((time.monotonic() - request_started) * 1000)
                request_state.status = "idle"
                request_state.last_completed_at = time.monotonic()
            except Exception as exc:
                error = (
                    f"sequence={sequence} {type(exc).__name__}: {exc}"
                )
                errors.append(error)
                request_state.status = "error"
                emit(
                    {
                        "type": "request_error",
                        "phase": phase,
                        "elapsed_sec": round(time.monotonic() - phase_started, 3),
                        "error": error,
                        "client_pending_bytes": client_pending_bytes(requester),
                        "latest_monitor": sampler.latest,
                    }
                )
            finally:
                request_state.request_started_at = None

            sequence += 1
            next_send_at = max(next_send_at + (1.0 / args.fps), time.monotonic())
    finally:
        stop_sampling.set()
        await sample_task

    elapsed_sec = time.monotonic() - phase_started
    after = await take_snapshot(monitors, args.monitor_timeout_sec)
    final_errors = sum(
        1
        for metrics in after.values()
        if "monitor_error" in metrics
    )
    byte_delta = snapshot_delta(before, after)
    result = PhaseResult(
        phase=phase,
        subject=subject,
        attempted=attempted,
        succeeded=succeeded,
        errors=errors,
        elapsed_sec=elapsed_sec,
        latencies_ms=latencies_ms,
        responder_received=responder.received,
        responder_handled=responder.handled,
        monitor_errors=baseline_errors + sampler.monitor_errors + final_errors,
        byte_delta=byte_delta,
    )
    result.route_checks = build_route_checks(
        phase=phase,
        succeeded=succeeded,
        payload_bytes=args.payload_bytes,
        delta=byte_delta,
        minimum_ratio=args.traffic_min_ratio,
        local_leak_ratio=args.local_leak_max_ratio,
    )
    result.diagnoses = diagnose(
        phase=phase,
        result=result,
        sampler=sampler,
        responder=responder,
        payload_bytes=args.payload_bytes,
    )
    emit(result.summary())
    return result, sequence


async def async_main(args: argparse.Namespace) -> int:
    monitors = {
        "edge_a": args.edge_a_monitor_url,
        "cloud": args.cloud_monitor_url,
        "edge_b": args.edge_b_monitor_url,
    }
    requester = NatsComm(servers=[args.edge_a_nats_url])
    local_responder_comm = NatsComm(servers=[args.edge_a_nats_url])
    global_responder_comm = NatsComm(servers=[args.edge_b_nats_url])
    local_state = ResponderState()
    global_state = ResponderState()

    try:
        if args.phase in {"local", "both"}:
            await register_responder(
                comm=local_responder_comm,
                cluster_id=args.edge_a_cluster_id,
                agent_id=args.agent_id,
                operation=args.operation,
                queue=f"{args.queue}-local",
                process_delay_ms=args.process_delay_ms,
                state=local_state,
            )
        if args.phase in {"global", "both"}:
            await register_responder(
                comm=global_responder_comm,
                cluster_id=args.edge_b_cluster_id,
                agent_id=args.agent_id,
                operation=args.operation,
                queue=f"{args.queue}-global",
                process_delay_ms=args.process_delay_ms,
                state=global_state,
            )
        await requester.connect(ensure_stream=False)

        results = []
        next_sequence = 0
        if args.phase in {"local", "both"}:
            local_result, next_sequence = await run_phase(
                phase="local",
                requester=requester,
                target_cluster=args.edge_a_cluster_id,
                source_cluster=args.edge_a_cluster_id,
                responder=local_state,
                monitors=monitors,
                args=args,
                sequence_start=next_sequence,
            )
            results.append(local_result)
        if args.phase in {"global", "both"}:
            global_result, next_sequence = await run_phase(
                phase="global",
                requester=requester,
                target_cluster=args.edge_b_cluster_id,
                source_cluster=args.edge_a_cluster_id,
                responder=global_state,
                monitors=monitors,
                args=args,
                sequence_start=next_sequence,
            )
            results.append(global_result)
        summaries = [result.summary() for result in results]
        passed = all(result.passed for result in results)
        emit(
            {
                "type": "run_summary",
                "passed": passed,
                "requested_phase": args.phase,
                "payload_bytes": args.payload_bytes,
                "duration_sec_per_phase": args.duration_sec,
                "max_fps": args.fps,
                "total_attempted": sum(item["attempted"] for item in summaries),
                "total_succeeded": sum(item["succeeded"] for item in summaries),
                "total_errors": sum(item["errors"] for item in summaries),
                "phases": summaries,
            }
        )
        return 0 if passed else 1
    finally:
        await asyncio.gather(
            requester.close(),
            local_responder_comm.close(),
            global_responder_comm.close(),
            return_exceptions=True,
        )


def positive_float(parser: argparse.ArgumentParser, name: str, value: float) -> None:
    if value <= 0:
        parser.error(f"{name} 必须大于 0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "验证 Core NATS local/global 大帧链路；"
            "--duration-sec 是每个阶段的持续时间"
        )
    )
    parser.add_argument(
        "--phase",
        choices=("local", "global", "both"),
        default="both",
    )
    parser.add_argument(
        "--edge-a-nats-url",
        default="nats://127.0.0.1:24223",
    )
    parser.add_argument(
        "--edge-b-nats-url",
        default="nats://127.0.0.1:24224",
    )
    parser.add_argument(
        "--edge-a-monitor-url",
        default="http://127.0.0.1:28223",
    )
    parser.add_argument(
        "--edge-b-monitor-url",
        default="http://127.0.0.1:28224",
    )
    parser.add_argument(
        "--cloud-monitor-url",
        default="http://127.0.0.1:28222",
    )
    parser.add_argument("--edge-a-cluster-id", default="edge-a")
    parser.add_argument("--edge-b-cluster-id", default="edge-b")
    parser.add_argument("--agent-id", default="topology-soak-agent")
    parser.add_argument("--operation", default="infer")
    parser.add_argument("--queue", default="topology-soak-workers")
    parser.add_argument("--payload-mib", type=float, default=15.0)
    parser.add_argument(
        "--fps",
        type=float,
        default=10.0,
        help="最大发送速率；请求严格串行，不保证达到该速率",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=300.0,
        help="local 和 global 每个阶段各自持续的秒数",
    )
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--sample-interval-sec", type=float, default=1.0)
    parser.add_argument("--monitor-timeout-sec", type=float, default=3.0)
    parser.add_argument("--process-delay-ms", type=float, default=0.0)
    parser.add_argument(
        "--traffic-min-ratio",
        type=float,
        default=0.80,
        help="目标节点最少应观察到的有效载荷字节比例",
    )
    parser.add_argument(
        "--local-leak-max-ratio",
        type=float,
        default=0.25,
        help="local 阶段 cloud/edge-b 允许的背景流量比例上限",
    )
    args = parser.parse_args()

    for name in (
        "fps",
        "duration_sec",
        "timeout_sec",
        "sample_interval_sec",
        "monitor_timeout_sec",
        "payload_mib",
    ):
        positive_float(parser, f"--{name.replace('_', '-')}", getattr(args, name))
    if args.process_delay_ms < 0:
        parser.error("--process-delay-ms 不能小于 0")
    if not 0 < args.traffic_min_ratio <= 1:
        parser.error("--traffic-min-ratio 必须在 (0, 1] 范围内")
    if not 0 <= args.local_leak_max_ratio < 1:
        parser.error("--local-leak-max-ratio 必须在 [0, 1) 范围内")
    args.payload_bytes = int(args.payload_mib * MIB)
    if args.payload_bytes < RESPONSE_STRUCT.size:
        parser.error("--payload-mib 对应的帧长度过小")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main(parse_args())))
    except KeyboardInterrupt:
        emit({"type": "run_interrupted", "message": "用户中断测试"})
        raise SystemExit(130)
