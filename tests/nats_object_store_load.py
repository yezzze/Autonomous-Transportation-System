import argparse
import asyncio
import hashlib
import json
import os
import struct
import time
import uuid
from typing import Dict, List

import nats
from nats.js import api
from nats.js.errors import NotFoundError


def percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * ratio))
    return ordered[index]


def build_payload(sequence: int, size_bytes: int) -> bytes:
    if size_bytes < 8:
        raise ValueError("payload size must be at least 8 bytes")
    return struct.pack("!Q", sequence) + bytes([sequence % 251]) * (
        size_bytes - 8
    )


def server_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def connect(servers: str):
    return await nats.connect(
        servers=server_list(servers),
        connect_timeout=5,
        reconnect_time_wait=1,
        max_reconnect_attempts=10,
        pending_size=128 * 1024 * 1024,
    )


async def recreate_bucket(js, bucket: str, args):
    try:
        await js.delete_object_store(bucket)
    except NotFoundError:
        pass
    return await js.create_object_store(
        bucket=bucket,
        config=api.ObjectStoreConfig(
            bucket=bucket,
            description="K8S_demo large-frame Object Store load test",
            ttl=args.bucket_ttl_sec,
            max_bytes=int(args.bucket_max_mib * 1024 * 1024),
            storage=api.StorageType.FILE,
            replicas=args.replicas,
        ),
    )


async def run(args) -> int:
    producer_nc = await connect(args.producer_servers)
    consumer_nc = await connect(args.consumer_servers)
    bucket = args.bucket or f"FRAME_TEST_{uuid.uuid4().hex[:12]}"
    producer_js = producer_nc.jetstream(domain=args.target_domain)
    consumer_js = consumer_nc.jetstream(domain=args.target_domain)
    await recreate_bucket(consumer_js, bucket, args)
    producer_store = await producer_js.object_store(bucket)
    consumer_store = await consumer_js.object_store(bucket)

    size_bytes = int(args.size_mib * 1024 * 1024)
    chunk_bytes = int(args.chunk_kib * 1024)
    deadline = time.monotonic() + args.duration_sec
    interval = 1.0 / args.fps
    next_send = time.monotonic()
    records: List[Dict] = []
    errors = []
    sequence = 0
    started = time.monotonic()

    print(
        "START "
        f"bucket={bucket} domain={args.target_domain} "
        f"producer={args.producer_servers} consumer={args.consumer_servers} "
        f"size_bytes={size_bytes} chunk_bytes={chunk_bytes} "
        f"target_fps={args.fps} duration_sec={args.duration_sec}",
        flush=True,
    )

    try:
        while time.monotonic() < deadline:
            delay = next_send - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            frame_started = time.monotonic()
            name = f"{args.instance_id}/{sequence}-{uuid.uuid4().hex}"
            payload = build_payload(sequence, size_bytes)
            expected_digest = hashlib.sha256(payload).hexdigest()
            uploaded = False
            try:
                upload_started = time.monotonic()
                info = await producer_store.put(
                    name,
                    payload,
                    meta=api.ObjectMeta(
                        name=name,
                        description=f"sequence={sequence}",
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Frame-Sequence": str(sequence),
                        },
                        options=api.ObjectMetaOptions(
                            max_chunk_size=chunk_bytes,
                        ),
                    ),
                )
                upload_ms = (time.monotonic() - upload_started) * 1000
                uploaded = True

                download_started = time.monotonic()
                result = await consumer_store.get(name)
                download_ms = (time.monotonic() - download_started) * 1000
                actual_digest = hashlib.sha256(result.data).hexdigest()
                if len(result.data) != size_bytes:
                    raise ValueError(
                        f"size mismatch: {len(result.data)} != {size_bytes}"
                    )
                if actual_digest != expected_digest:
                    raise ValueError("downloaded frame digest mismatch")

                delete_started = time.monotonic()
                await consumer_store.delete(name)
                delete_ms = (time.monotonic() - delete_started) * 1000
                total_ms = (time.monotonic() - frame_started) * 1000
                record = {
                    "sequence": sequence,
                    "bytes": size_bytes,
                    "chunks": info.chunks,
                    "upload_ms": round(upload_ms, 3),
                    "download_ms": round(download_ms, 3),
                    "delete_ms": round(delete_ms, 3),
                    "total_ms": round(total_ms, 3),
                }
                records.append(record)
                if args.verbose:
                    print(json.dumps(record, sort_keys=True), flush=True)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if uploaded:
                    try:
                        await consumer_store.delete(name)
                    except Exception:
                        pass
            sequence += 1
            next_send = max(next_send + interval, time.monotonic())
    finally:
        elapsed = time.monotonic() - started
        status = await consumer_store.status()
        if not args.keep_bucket:
            await consumer_js.delete_object_store(bucket)
        await producer_nc.close()
        await consumer_nc.close()

    totals = [item["total_ms"] for item in records]
    uploads = [item["upload_ms"] for item in records]
    downloads = [item["download_ms"] for item in records]
    result = {
        "bucket": bucket,
        "target_domain": args.target_domain,
        "frame_bytes": size_bytes,
        "chunk_bytes": chunk_bytes,
        "chunks_per_frame": records[0]["chunks"] if records else None,
        "duration_sec": round(elapsed, 3),
        "target_fps": args.fps,
        "completed": len(records),
        "errors": len(errors),
        "completed_fps": round(len(records) / elapsed, 3),
        "throughput_mib_sec": round(
            len(records) * size_bytes / elapsed / 1024 / 1024,
            3,
        ),
        "upload_p50_ms": round(percentile(uploads, 0.50), 3),
        "upload_p95_ms": round(percentile(uploads, 0.95), 3),
        "download_p50_ms": round(percentile(downloads, 0.50), 3),
        "download_p95_ms": round(percentile(downloads, 0.95), 3),
        "roundtrip_p50_ms": round(percentile(totals, 0.50), 3),
        "roundtrip_p95_ms": round(percentile(totals, 0.95), 3),
        "roundtrip_max_ms": round(max(totals, default=0.0), 3),
        "bucket_bytes_before_cleanup": status.stream_info.state.bytes,
    }
    print(f"RESULT {json.dumps(result, sort_keys=True)}", flush=True)
    for error in errors[:10]:
        print(f"ERROR {error}", flush=True)
    return 0 if records and not errors else 1


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sequential NATS JetStream Object Store frame put/get/delete test"
        )
    )
    parser.add_argument(
        "--producer-servers",
        default=os.environ.get(
            "NATS_PRODUCER_SERVERS",
            "nats://127.0.0.1:24223",
        ),
    )
    parser.add_argument(
        "--consumer-servers",
        default=os.environ.get(
            "NATS_CONSUMER_SERVERS",
            "nats://127.0.0.1:24223",
        ),
    )
    parser.add_argument("--target-domain", default="edge-a")
    parser.add_argument("--instance-id", default="object-store-load")
    parser.add_argument("--bucket")
    parser.add_argument("--size-mib", type=float, default=10)
    parser.add_argument("--chunk-kib", type=int, default=128)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--duration-sec", type=float, default=10)
    parser.add_argument("--bucket-max-mib", type=float, default=512)
    parser.add_argument("--bucket-ttl-sec", type=float, default=300)
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--keep-bucket", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.size_mib <= 0:
        parser.error("--size-mib must be positive")
    if args.chunk_kib <= 0:
        parser.error("--chunk-kib must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.duration_sec <= 0:
        parser.error("--duration-sec must be positive")
    if args.bucket_max_mib <= args.size_mib:
        parser.error("--bucket-max-mib must exceed --size-mib")
    if args.bucket_ttl_sec <= 0:
        parser.error("--bucket-ttl-sec must be positive")
    if args.replicas <= 0:
        parser.error("--replicas must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
