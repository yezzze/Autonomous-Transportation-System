#!/usr/bin/env python3
"""JetStream Memory local/global 大帧串行稳定性测试。"""

import argparse
import asyncio
import json
import math
import struct
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_api import NatsComm  # noqa: E402


MIB = 1024 * 1024
RESPONSE = struct.Struct("!QQ")


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def emit(event):
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def fetch_varz(url):
    with urllib.request.urlopen(f"{url.rstrip('/')}/varz", timeout=3) as response:
        value = json.load(response)
    return {
        key: int(value.get(key, 0))
        for key in (
            "in_msgs",
            "out_msgs",
            "in_bytes",
            "out_bytes",
            "slow_consumers",
            "connections",
            "leafnodes",
            "mem",
        )
    }


async def snapshot(monitors):
    values = await asyncio.gather(
        *(asyncio.to_thread(fetch_varz, url) for url in monitors.values())
    )
    return dict(zip(monitors, values))


def delta(before, after):
    return {
        node: {
            key: max(0, after[node][key] - before[node][key])
            for key in ("in_msgs", "out_msgs", "in_bytes", "out_bytes")
        }
        for node in before
    }


async def wait_consumers(comm, cluster, instance_id):
    for _ in range(200):
        status = await comm.memory_frame_stream_status(cluster, instance_id)
        if status["consumer_count"] >= 2:
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Memory frame consumers not ready: {cluster}/{instance_id}")


async def wait_stream_empty(comm, cluster, instance_id):
    for _ in range(200):
        status = await comm.memory_frame_stream_status(cluster, instance_id)
        if (
            status["messages"] == 0
            and status["num_pending"] == 0
            and status["num_ack_pending"] == 0
        ):
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Memory frame stream did not drain: {cluster}/{instance_id}")


async def run_phase(
    *,
    phase,
    requester,
    target_cluster,
    instance_id,
    sequence_start,
    payload,
    monitors,
    args,
):
    before = await snapshot(monitors)
    started = time.monotonic()
    deadline = started + args.duration_sec
    next_send_at = started
    sequence = sequence_start
    attempted = 0
    latencies = []
    errors = []
    subscription_count = None
    max_stream_messages = 0
    max_ack_pending = 0
    next_sample_at = started

    while time.monotonic() < deadline:
        delay = next_send_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if time.monotonic() >= deadline:
            break

        struct.pack_into("!Q", payload, 0, sequence)
        attempted += 1
        request_started = time.monotonic()
        try:
            response = await requester.request_memory_frame(
                target_cluster=target_cluster,
                agent_id=args.agent_id,
                target_instance_id=instance_id,
                payload=payload,
                local_cluster=args.edge_a_cluster_id,
                timeout_sec=args.timeout_sec,
                request_id=f"{phase}-{sequence}",
            )
            response_sequence, response_size = RESPONSE.unpack(response)
            if response_sequence != sequence or response_size != len(payload):
                raise ValueError(
                    f"reply mismatch sequence={response_sequence} size={response_size}"
                )
            latencies.append((time.monotonic() - request_started) * 1000)
        except Exception as exc:
            errors.append(f"sequence={sequence} {type(exc).__name__}: {exc}")

        current_subscriptions = len(requester._nc._subs)
        if subscription_count is None:
            subscription_count = current_subscriptions
        elif current_subscriptions != subscription_count:
            errors.append(
                "requester subscriptions grew: "
                f"baseline={subscription_count} current={current_subscriptions}"
            )
            break

        now = time.monotonic()
        if now >= next_sample_at:
            status = await requester.memory_frame_stream_status(
                target_cluster,
                instance_id,
            )
            max_stream_messages = max(max_stream_messages, status["messages"])
            max_ack_pending = max(max_ack_pending, status["num_ack_pending"])
            emit(
                {
                    "type": "sample",
                    "phase": phase,
                    "elapsed_sec": round(now - started, 3),
                    "sequence": sequence,
                    "last_latency_ms": round(
                        (time.monotonic() - request_started) * 1000,
                        3,
                    ),
                    "stream_messages": status["messages"],
                    "num_pending": status["num_pending"],
                    "num_ack_pending": status["num_ack_pending"],
                    "requester_subscriptions": current_subscriptions,
                }
            )
            next_sample_at = now + args.sample_interval_sec

        sequence += 1
        next_send_at = max(next_send_at + 1.0 / args.fps, time.monotonic())

    final_stream = await wait_stream_empty(requester, target_cluster, instance_id)
    after = await snapshot(monitors)
    traffic = delta(before, after)
    succeeded = len(latencies)
    expected_bytes = succeeded * len(payload)
    local_leak_limit = int(expected_bytes * 0.01)
    local_isolated = all(
        max(traffic[node]["in_bytes"], traffic[node]["out_bytes"])
        <= local_leak_limit
        for node in ("cloud", "edge_b")
    )
    global_nodes_used = all(
        max(traffic[node]["in_bytes"], traffic[node]["out_bytes"])
        >= int(expected_bytes * 0.8)
        for node in ("edge_a", "cloud", "edge_b")
    )
    passed = (
        not errors
        and final_stream["messages"] == 0
        and final_stream["num_pending"] == 0
        and final_stream["num_ack_pending"] == 0
        and (local_isolated if phase == "local" else global_nodes_used)
    )
    summary = {
        "type": "phase_summary",
        "phase": phase,
        "passed": passed,
        "attempted": attempted,
        "succeeded": succeeded,
        "error_count": len(errors),
        "error_examples": errors[:10],
        "elapsed_sec": round(time.monotonic() - started, 3),
        "actual_fps": round(succeeded / args.duration_sec, 3),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "p99": round(percentile(latencies, 0.99), 3),
            "max": round(max(latencies, default=0.0), 3),
        },
        "stream": final_stream,
        "max_stream_messages_at_sample": max_stream_messages,
        "max_ack_pending_at_sample": max_ack_pending,
        "requester_subscriptions": subscription_count,
        "traffic_delta": traffic,
        "local_leak_limit_bytes": local_leak_limit,
        "local_isolated": local_isolated,
        "global_nodes_used": global_nodes_used,
    }
    emit(summary)
    return summary, sequence


async def main(args):
    monitors = {
        "edge_a": args.edge_a_monitor_url,
        "cloud": args.cloud_monitor_url,
        "edge_b": args.edge_b_monitor_url,
    }
    requester = NatsComm(servers=[args.edge_a_nats_url])
    worker_a = NatsComm(servers=[args.edge_a_nats_url])
    worker_b = NatsComm(servers=[args.edge_b_nats_url])
    instance_a = "memory-soak-local-a"
    instance_b = "memory-soak-global-b"
    payload = bytearray(args.payload_bytes)
    tasks = []

    async def handler(message):
        sequence = struct.unpack_from("!Q", message.data, 0)[0]
        return RESPONSE.pack(sequence, len(message.data))

    try:
        tasks = [
            asyncio.create_task(
                worker_a.serve_memory_frames(
                    agent_id=args.agent_id,
                    instance_id=instance_a,
                    local_cluster=args.edge_a_cluster_id,
                    handler=handler,
                    poll_timeout_sec=0.2,
                    max_inflight=1,
                )
            ),
            asyncio.create_task(
                worker_b.serve_memory_frames(
                    agent_id=args.agent_id,
                    instance_id=instance_b,
                    local_cluster=args.edge_b_cluster_id,
                    handler=handler,
                    poll_timeout_sec=0.2,
                    max_inflight=1,
                )
            ),
        ]
        await requester.connect(ensure_stream=False)
        await wait_consumers(requester, args.edge_a_cluster_id, instance_a)
        await wait_consumers(requester, args.edge_b_cluster_id, instance_b)

        local, sequence = await run_phase(
            phase="local",
            requester=requester,
            target_cluster=args.edge_a_cluster_id,
            instance_id=instance_a,
            sequence_start=0,
            payload=payload,
            monitors=monitors,
            args=args,
        )
        global_result, _ = await run_phase(
            phase="global",
            requester=requester,
            target_cluster=args.edge_b_cluster_id,
            instance_id=instance_b,
            sequence_start=sequence,
            payload=payload,
            monitors=monitors,
            args=args,
        )
        passed = local["passed"] and global_result["passed"]
        emit(
            {
                "type": "run_summary",
                "passed": passed,
                "payload_bytes": args.payload_bytes,
                "duration_sec_per_phase": args.duration_sec,
                "total_succeeded": local["succeeded"] + global_result["succeeded"],
                "total_errors": (
                    local["error_count"] + global_result["error_count"]
                ),
                "phases": [local, global_result],
            }
        )
        return 0 if passed else 1
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            requester.close(),
            worker_a.close(),
            worker_b.close(),
            return_exceptions=True,
        )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-a-nats-url", default="nats://127.0.0.1:24223")
    parser.add_argument("--edge-b-nats-url", default="nats://127.0.0.1:24224")
    parser.add_argument("--edge-a-monitor-url", default="http://127.0.0.1:28223")
    parser.add_argument("--edge-b-monitor-url", default="http://127.0.0.1:28224")
    parser.add_argument("--cloud-monitor-url", default="http://127.0.0.1:28222")
    parser.add_argument("--edge-a-cluster-id", default="edge-a")
    parser.add_argument("--edge-b-cluster-id", default="edge-b")
    parser.add_argument("--agent-id", default="memory-soak-agent")
    parser.add_argument("--payload-mib", type=float, default=15.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--duration-sec", type=float, default=150.0)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--sample-interval-sec", type=float, default=5.0)
    args = parser.parse_args()
    for name in ("payload_mib", "fps", "duration_sec", "timeout_sec"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    args.payload_bytes = int(args.payload_mib * MIB)
    if args.payload_bytes < RESPONSE.size:
        parser.error("--payload-mib is too small")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
