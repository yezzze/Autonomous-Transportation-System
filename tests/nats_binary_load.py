import argparse
import asyncio
import os
import struct
import time
from typing import List

from runtime_api import NatsBinaryMessage, NatsComm


def percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * ratio))
    return ordered[index]


def build_payload(sequence: int, size_bytes: int) -> bytes:
    if size_bytes < 8:
        raise ValueError("payload size must be at least 8 bytes")
    payload = bytearray(size_bytes)
    struct.pack_into("!Q", payload, 0, sequence)
    return bytes(payload)


async def register_responder(
    comm: NatsComm,
    args,
    responder_cluster: str,
) -> None:
    async def handler(message: NatsBinaryMessage) -> bytes:
        sequence = struct.unpack_from("!Q", message.data, 0)[0]
        return struct.pack("!QQ", sequence, len(message.data))

    subjects = comm.frame_subscription_subjects(
        agent_id=args.agent_id,
        operation=args.operation,
        local_cluster=responder_cluster,
    )
    await comm.subscribe_frame_bytes(
        agent_id=args.agent_id,
        handler=handler,
        operation=args.operation,
        local_cluster=responder_cluster,
        queue=args.queue,
    )
    print(
        f"READY local_subject={subjects[0]} global_subject={subjects[1]}",
        flush=True,
    )


async def run_client(comm: NatsComm, args) -> int:
    size_bytes = int(args.size_mib * 1024 * 1024)
    request_count = int(args.fps * args.duration_sec)
    interval = 1.0 / args.fps
    latencies_ms: List[float] = []
    errors = []
    started = time.monotonic()
    next_send = started

    subject = comm.frame_subject(
        target_cluster=args.target_cluster,
        agent_id=args.agent_id,
        operation=args.operation,
        local_cluster=args.local_cluster,
    )
    print(
        f"START subject={subject} requests={request_count} "
        f"payload_bytes={size_bytes} target_fps={args.fps}",
        flush=True,
    )

    for sequence in range(request_count):
        delay = next_send - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        request_started = time.monotonic()
        try:
            response = await comm.request_frame_bytes(
                target_cluster=args.target_cluster,
                agent_id=args.agent_id,
                payload=build_payload(sequence, size_bytes),
                operation=args.operation,
                local_cluster=args.local_cluster,
                timeout_sec=args.timeout_sec,
            )
            response_sequence, response_size = struct.unpack("!QQ", response)
            if response_sequence != sequence or response_size != size_bytes:
                raise ValueError(
                    f"invalid response sequence={response_sequence} "
                    f"size={response_size}"
                )
            latencies_ms.append((time.monotonic() - request_started) * 1000)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        next_send += interval

    elapsed = time.monotonic() - started
    completed = len(latencies_ms)
    print(
        "RESULT "
        f"sent={request_count} completed={completed} errors={len(errors)} "
        f"elapsed_sec={elapsed:.3f} completed_fps={completed / elapsed:.3f} "
        f"latency_p50_ms={percentile(latencies_ms, 0.50):.1f} "
        f"latency_p95_ms={percentile(latencies_ms, 0.95):.1f} "
        f"latency_max_ms={max(latencies_ms, default=0.0):.1f}",
        flush=True,
    )
    for error in errors[:10]:
        print(f"ERROR {error}", flush=True)
    return 0 if not errors and completed == request_count else 1


async def run(args) -> int:
    os.environ["CLUSTER_ID"] = args.local_cluster
    if args.servers:
        os.environ["NATS_SERVERS"] = args.servers

    if args.mode == "server":
        comm = NatsComm()
        try:
            await register_responder(comm, args, args.local_cluster)
            await asyncio.Event().wait()
        finally:
            await comm.close()
        return 0

    if args.mode == "client":
        comm = NatsComm()
        try:
            return await run_client(comm, args)
        finally:
            await comm.close()

    responder = NatsComm()
    requester = NatsComm()
    try:
        await register_responder(responder, args, args.target_cluster)
        return await run_client(requester, args)
    finally:
        await requester.close()
        await responder.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Core NATS local/global binary frame load test"
    )
    parser.add_argument(
        "--mode",
        choices=("loopback", "server", "client"),
        default="loopback",
    )
    parser.add_argument(
        "--servers",
        default=os.environ.get("NATS_SERVERS", "nats://127.0.0.1:4222"),
    )
    parser.add_argument(
        "--local-cluster",
        default=os.environ.get("CLUSTER_ID", "edge-a"),
    )
    parser.add_argument("--target-cluster", default="edge-a")
    parser.add_argument("--agent-id", default="binary-load")
    parser.add_argument("--operation", default="infer")
    parser.add_argument("--queue", default="binary-load-workers")
    parser.add_argument("--size-mib", type=float, default=10)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--duration-sec", type=float, default=10)
    parser.add_argument("--timeout-sec", type=float, default=5)
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.duration_sec <= 0:
        parser.error("--duration-sec must be positive")
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be positive")
    if args.size_mib <= 0:
        parser.error("--size-mib must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
