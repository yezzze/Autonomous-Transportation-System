#!/usr/bin/env python3
"""在现有 NATS 上执行 JetStream Memory local/global 大帧稳定性测试。"""

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


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


async def monitor_snapshot(base_url):
    base = base_url.rstrip("/")
    varz, leafz, jsz = await asyncio.gather(
        asyncio.to_thread(fetch_json, f"{base}/varz"),
        asyncio.to_thread(fetch_json, f"{base}/leafz"),
        asyncio.to_thread(fetch_json, f"{base}/jsz?streams=1"),
    )
    leaves = leafz.get("leafs") or []
    return {
        "server": {
            key: int(varz.get(key, 0))
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
        },
        "leaf": {
            "connections": int(leafz.get("leafnodes", 0)),
            "in_msgs": sum(int(item.get("in_msgs", 0)) for item in leaves),
            "out_msgs": sum(int(item.get("out_msgs", 0)) for item in leaves),
            "in_bytes": sum(int(item.get("in_bytes", 0)) for item in leaves),
            "out_bytes": sum(int(item.get("out_bytes", 0)) for item in leaves),
            "rtt": [item.get("rtt") for item in leaves],
        },
        "jetstream": {
            key: int(jsz.get(key, 0))
            for key in (
                "streams",
                "consumers",
                "messages",
                "bytes",
                "memory",
                "storage",
            )
        },
    }


def counter_delta(before, after, section):
    return {
        key: max(0, int(after[section][key]) - int(before[section][key]))
        for key in ("in_msgs", "out_msgs", "in_bytes", "out_bytes")
    }


async def wait_consumers(comm, cluster, instance_id):
    for _ in range(200):
        status = await comm.memory_frame_stream_status(cluster, instance_id)
        if status["consumer_count"] >= 2:
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError("Memory frame consumers did not become ready")


async def wait_drained(comm, cluster, instance_id):
    for _ in range(200):
        status = await comm.memory_frame_stream_status(cluster, instance_id)
        if (
            status["messages"] == 0
            and status["num_pending"] == 0
            and status["num_ack_pending"] == 0
        ):
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Memory frame stream did not drain: {status}")


async def main(args):
    target_cluster = args.target_cluster or args.cluster
    worker_nats_url = args.worker_nats_url or args.nats_url
    route_scope = "local" if target_cluster == args.cluster else "global"
    requester = NatsComm(
        servers=[args.nats_url],
        jetstream_domain=args.cluster,
    )
    worker = NatsComm(
        servers=[worker_nats_url],
        jetstream_domain=target_cluster,
    )
    payload = bytearray(args.payload_bytes)
    task = None
    summary = None

    async def handler(message):
        sequence = struct.unpack_from("!Q", message.data, 0)[0]
        return RESPONSE.pack(sequence, len(message.data))

    try:
        task = asyncio.create_task(
            worker.serve_memory_frames(
                agent_id=args.agent_id,
                instance_id=args.instance_id,
                local_cluster=target_cluster,
                handler=handler,
                poll_timeout_sec=0.2,
                max_inflight=1,
            )
        )
        await requester.connect(ensure_stream=False)
        await wait_consumers(requester, target_cluster, args.instance_id)

        before = await monitor_snapshot(args.monitor_url)
        started = time.monotonic()
        deadline = started + args.duration_sec
        next_send_at = started
        next_sample_at = started
        attempted = 0
        succeeded = 0
        latencies = []
        errors = []
        max_stream_messages = 0
        max_num_pending = 0
        max_ack_pending = 0
        requester_subscriptions = None

        while time.monotonic() < deadline:
            delay = next_send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if time.monotonic() >= deadline:
                break

            sequence = attempted
            attempted += 1
            struct.pack_into("!Q", payload, 0, sequence)
            request_started = time.monotonic()
            try:
                response = await requester.request_memory_frame(
                    target_cluster=target_cluster,
                    agent_id=args.agent_id,
                    target_instance_id=args.instance_id,
                    payload=payload,
                    local_cluster=args.cluster,
                    timeout_sec=args.timeout_sec,
                    request_id=f"edge-{route_scope}-{sequence}",
                )
                response_sequence, response_size = RESPONSE.unpack(response)
                if response_sequence != sequence or response_size != len(payload):
                    raise ValueError(
                        f"reply mismatch sequence={response_sequence} "
                        f"size={response_size}"
                    )
                succeeded += 1
                latencies.append((time.monotonic() - request_started) * 1000)
            except Exception as exc:
                errors.append(f"sequence={sequence} {type(exc).__name__}: {exc}")

            subscriptions = len(requester._nc._subs)
            if requester_subscriptions is None:
                requester_subscriptions = subscriptions
            elif subscriptions != requester_subscriptions:
                errors.append(
                    "requester subscriptions grew: "
                    f"baseline={requester_subscriptions} current={subscriptions}"
                )
                break

            now = time.monotonic()
            if now >= next_sample_at:
                stream = await requester.memory_frame_stream_status(
                    target_cluster,
                    args.instance_id,
                )
                monitor = await monitor_snapshot(args.monitor_url)
                max_stream_messages = max(max_stream_messages, stream["messages"])
                max_num_pending = max(max_num_pending, stream["num_pending"])
                max_ack_pending = max(
                    max_ack_pending,
                    stream["num_ack_pending"],
                )
                emit(
                    {
                        "type": "sample",
                        "elapsed_sec": round(now - started, 3),
                        "sequence": sequence,
                        "last_latency_ms": round(
                            (time.monotonic() - request_started) * 1000,
                            3,
                        ),
                        "stream_messages": stream["messages"],
                        "num_pending": stream["num_pending"],
                        "num_ack_pending": stream["num_ack_pending"],
                        "requester_subscriptions": subscriptions,
                        "slow_consumers": monitor["server"]["slow_consumers"],
                        "nats_memory": monitor["server"]["mem"],
                        "jetstream_memory": monitor["jetstream"]["memory"],
                        "leaf_connections": monitor["leaf"]["connections"],
                        "leaf_rtt": monitor["leaf"]["rtt"],
                    }
                )
                next_sample_at = now + args.sample_interval_sec

            next_send_at = max(
                next_send_at + 1.0 / args.fps,
                time.monotonic(),
            )

        stream = await wait_drained(requester, target_cluster, args.instance_id)
        after = await monitor_snapshot(args.monitor_url)
        server_delta = counter_delta(before, after, "server")
        leaf_delta = counter_delta(before, after, "leaf")
        local_leak_limit = max(10 * MIB, int(succeeded * len(payload) * 0.01))
        local_isolated = max(
            leaf_delta["in_bytes"],
            leaf_delta["out_bytes"],
        ) <= local_leak_limit
        expected_bytes = succeeded * len(payload)
        global_routed = (
            leaf_delta["out_bytes"] >= int(expected_bytes * 0.8)
        )
        route_check_passed = (
            local_isolated if route_scope == "local" else global_routed
        )
        no_redelivery = all(
            consumer["num_redelivered"] == 0
            for consumer in stream["consumers"]
        )
        passed = (
            not errors
            and succeeded == attempted
            and stream["messages"] == 0
            and stream["num_pending"] == 0
            and stream["num_ack_pending"] == 0
            and no_redelivery
            and after["server"]["slow_consumers"] == 0
            and after["leaf"]["connections"] == before["leaf"]["connections"]
            and route_check_passed
        )
        summary = {
            "type": "run_summary",
            "passed": passed,
            "requester_cluster": args.cluster,
            "target_cluster": target_cluster,
            "route_scope": route_scope,
            "instance_id": args.instance_id,
            "payload_bytes": args.payload_bytes,
            "duration_sec": round(time.monotonic() - started, 3),
            "attempted": attempted,
            "succeeded": succeeded,
            "error_count": len(errors),
            "error_examples": errors[:10],
            "actual_fps": round(succeeded / args.duration_sec, 3),
            "latency_ms": {
                "p50": round(percentile(latencies, 0.50), 3),
                "p95": round(percentile(latencies, 0.95), 3),
                "p99": round(percentile(latencies, 0.99), 3),
                "max": round(max(latencies, default=0.0), 3),
            },
            "stream": stream,
            "max_stream_messages_at_sample": max_stream_messages,
            "max_num_pending_at_sample": max_num_pending,
            "max_ack_pending_at_sample": max_ack_pending,
            "requester_subscriptions": requester_subscriptions,
            "server_delta": server_delta,
            "leaf_delta": leaf_delta,
            "local_leak_limit_bytes": local_leak_limit,
            "local_isolated": local_isolated,
            "expected_payload_bytes": expected_bytes,
            "global_routed": global_routed,
            "route_check_passed": route_check_passed,
            "no_redelivery": no_redelivery,
            "before": before,
            "after": after,
        }
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        if summary is not None:
            after_close = await requester.memory_frame_stream_status(
                target_cluster,
                args.instance_id,
            )
            summary["deleted_on_close"] = not after_close["exists"]
            summary["passed"] = summary["passed"] and not after_close["exists"]
            emit(summary)
        await requester.close()

    return 0 if summary and summary["passed"] else 1


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", default="nats://nats:4222")
    parser.add_argument("--worker-nats-url")
    parser.add_argument("--monitor-url", default="http://nats:8222")
    parser.add_argument("--cluster", default="edge-a")
    parser.add_argument("--target-cluster")
    parser.add_argument("--agent-id", default="edge-local-soak-agent")
    parser.add_argument("--instance-id", default="edge-local-soak-instance")
    parser.add_argument("--payload-mib", type=float, default=15.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--duration-sec", type=float, default=300.0)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--sample-interval-sec", type=float, default=5.0)
    args = parser.parse_args()
    for name in (
        "payload_mib",
        "fps",
        "duration_sec",
        "timeout_sec",
        "sample_interval_sec",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    args.payload_bytes = int(args.payload_mib * MIB)
    if args.payload_bytes < RESPONSE.size:
        parser.error("--payload-mib is too small")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
