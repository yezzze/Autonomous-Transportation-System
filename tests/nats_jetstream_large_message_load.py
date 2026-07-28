import argparse
import asyncio
import hashlib
import json
import struct
import time
import uuid
from typing import List

import nats
from nats.js import api


def percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * ratio))
    return ordered[index]


def build_payload(sequence: int, size_bytes: int) -> bytes:
    return struct.pack("!Q", sequence) + bytes([sequence % 251]) * (
        size_bytes - 8
    )


async def connect(servers: str):
    return await nats.connect(
        servers=[item.strip() for item in servers.split(",") if item.strip()],
        connect_timeout=5,
        reconnect_time_wait=1,
        max_reconnect_attempts=10,
        pending_size=128 * 1024 * 1024,
    )


async def run(args) -> int:
    producer_nc = await connect(args.producer_servers)
    consumer_nc = await connect(args.consumer_servers)
    producer_js = producer_nc.jetstream(domain=args.target_domain)
    consumer_js = consumer_nc.jetstream(domain=args.target_domain)
    suffix = uuid.uuid4().hex[:12]
    stream = f"LARGE_TEST_{suffix}"
    subject = f"large.test.{suffix}"
    durable = f"LARGE_TEST_CONSUMER_{suffix}"
    size_bytes = int(args.size_mib * 1024 * 1024)
    payload = build_payload(0, size_bytes)
    digest = hashlib.sha256(payload).digest()

    await consumer_js.add_stream(
        config=api.StreamConfig(
            name=stream,
            subjects=[subject],
            retention=api.RetentionPolicy.WORK_QUEUE,
            storage=api.StorageType.FILE,
            discard=api.DiscardPolicy.NEW,
            max_bytes=int(args.stream_max_mib * 1024 * 1024),
            max_msg_size=size_bytes + 1024,
            num_replicas=args.replicas,
        )
    )
    subscription = await consumer_js.pull_subscribe(
        subject,
        durable=durable,
        stream=stream,
    )

    publish_times = []
    fetch_times = []
    ack_times = []
    totals = []
    errors = []
    sequence = 0
    interval = 1.0 / args.fps
    started = time.monotonic()
    deadline = started + args.duration_sec
    next_send = started

    print(
        "START "
        f"stream={stream} domain={args.target_domain} "
        f"producer={args.producer_servers} consumer={args.consumer_servers} "
        f"size_bytes={size_bytes} target_fps={args.fps} "
        f"duration_sec={args.duration_sec}",
        flush=True,
    )

    try:
        while time.monotonic() < deadline:
            delay = next_send - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            frame_started = time.monotonic()
            try:
                publish_started = time.monotonic()
                await producer_js.publish(
                    subject,
                    payload,
                    timeout=args.timeout_sec,
                )
                publish_ms = (time.monotonic() - publish_started) * 1000

                fetch_started = time.monotonic()
                messages = await subscription.fetch(
                    1,
                    timeout=args.timeout_sec,
                )
                fetch_ms = (time.monotonic() - fetch_started) * 1000
                if len(messages) != 1:
                    raise RuntimeError(
                        f"expected one message, received {len(messages)}"
                    )
                message = messages[0]
                if len(message.data) != size_bytes:
                    raise ValueError("received message size mismatch")
                if hashlib.sha256(message.data).digest() != digest:
                    raise ValueError("received message digest mismatch")
                if struct.unpack_from("!Q", message.data)[0] != 0:
                    raise ValueError("received message marker mismatch")

                ack_started = time.monotonic()
                await message.ack_sync(timeout=args.timeout_sec)
                ack_ms = (time.monotonic() - ack_started) * 1000
                total_ms = (time.monotonic() - frame_started) * 1000
                publish_times.append(publish_ms)
                fetch_times.append(fetch_ms)
                ack_times.append(ack_ms)
                totals.append(total_ms)
                if args.verbose:
                    print(
                        json.dumps(
                            {
                                "sequence": sequence,
                                "publish_ms": round(publish_ms, 3),
                                "fetch_ms": round(fetch_ms, 3),
                                "ack_ms": round(ack_ms, 3),
                                "total_ms": round(total_ms, 3),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            sequence += 1
            next_send = max(next_send + interval, time.monotonic())
    finally:
        elapsed = time.monotonic() - started
        stream_info = await consumer_js.stream_info(stream)
        await consumer_js.delete_stream(stream)
        await producer_nc.close()
        await consumer_nc.close()

    result = {
        "target_domain": args.target_domain,
        "frame_bytes": size_bytes,
        "duration_sec": round(elapsed, 3),
        "target_fps": args.fps,
        "completed": len(totals),
        "errors": len(errors),
        "completed_fps": round(len(totals) / elapsed, 3),
        "throughput_mib_sec": round(
            len(totals) * size_bytes / elapsed / 1024 / 1024,
            3,
        ),
        "publish_p50_ms": round(percentile(publish_times, 0.50), 3),
        "publish_p95_ms": round(percentile(publish_times, 0.95), 3),
        "fetch_p50_ms": round(percentile(fetch_times, 0.50), 3),
        "fetch_p95_ms": round(percentile(fetch_times, 0.95), 3),
        "ack_p50_ms": round(percentile(ack_times, 0.50), 3),
        "ack_p95_ms": round(percentile(ack_times, 0.95), 3),
        "roundtrip_p50_ms": round(percentile(totals, 0.50), 3),
        "roundtrip_p95_ms": round(percentile(totals, 0.95), 3),
        "roundtrip_max_ms": round(max(totals, default=0.0), 3),
        "stream_messages_after_ack": stream_info.state.messages,
        "stream_bytes_after_ack": stream_info.state.bytes,
    }
    print(f"RESULT {json.dumps(result, sort_keys=True)}", flush=True)
    for error in errors[:10]:
        print(f"ERROR {error}", flush=True)
    return 0 if totals and not errors else 1


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sequential single-message JetStream large payload load test"
        )
    )
    parser.add_argument(
        "--producer-servers",
        default="nats://127.0.0.1:24223",
    )
    parser.add_argument(
        "--consumer-servers",
        default="nats://127.0.0.1:24223",
    )
    parser.add_argument("--target-domain", default="edge-a")
    parser.add_argument("--size-mib", type=float, default=10)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--duration-sec", type=float, default=10)
    parser.add_argument("--timeout-sec", type=float, default=10)
    parser.add_argument("--stream-max-mib", type=float, default=512)
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.size_mib <= 0:
        parser.error("--size-mib must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.duration_sec <= 0:
        parser.error("--duration-sec must be positive")
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be positive")
    if args.stream_max_mib <= args.size_mib:
        parser.error("--stream-max-mib must exceed --size-mib")
    if args.replicas <= 0:
        parser.error("--replicas must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
