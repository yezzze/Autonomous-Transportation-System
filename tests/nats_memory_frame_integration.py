import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict

import nats
from nats.errors import TimeoutError as NatsTimeoutError

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime_api import NatsComm  # noqa: E402


async def wait_consumers(
    comm: NatsComm,
    cluster: str,
    instance_id: str,
    expected: int = 2,
) -> None:
    for _ in range(100):
        status = await comm.memory_frame_stream_status(
            cluster,
            instance_id,
        )
        if status["consumer_count"] >= expected:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"Memory frame consumers not ready: {cluster}/{instance_id}"
    )


async def expect_timeout(subscription, label: str) -> None:
    try:
        await subscription.next_msg(timeout=0.5)
    except NatsTimeoutError:
        return
    raise RuntimeError(f"LeafNode deny failed: {label}")


async def test_leafnode_deny(args) -> Dict[str, bool]:
    edge_a = await nats.connect(args.edge_a)
    edge_b = await nats.connect(args.edge_b)
    hub = await nats.connect(args.hub)
    local_probe = (
        "frame.local.edge-a.agent.deny.instance.probe.infer"
    )
    global_probe = (
        "frame.global.edge-b.agent.deny.instance.probe.infer"
    )
    try:
        edge_import = await edge_a.subscribe(local_probe)
        await edge_a.flush()
        await asyncio.sleep(0.2)
        await hub.publish(local_probe, b"must-not-import")
        await hub.flush()
        await expect_timeout(edge_import, "deny_imports frame.local.>")
        await edge_import.unsubscribe()

        hub_export = await hub.subscribe(local_probe)
        await hub.flush()
        await asyncio.sleep(0.2)
        await edge_a.publish(local_probe, b"must-not-export")
        await edge_a.flush()
        await expect_timeout(hub_export, "deny_exports frame.local.>")
        await hub_export.unsubscribe()

        edge_global = await edge_b.subscribe(global_probe)
        await edge_b.flush()
        await asyncio.sleep(0.2)
        await edge_a.publish(global_probe, b"global-allowed")
        await edge_a.flush()
        message = await edge_global.next_msg(timeout=2)
        if message.data != b"global-allowed":
            raise RuntimeError("frame.global payload mismatch")
        await edge_global.unsubscribe()
        return {
            "deny_imports_local": True,
            "deny_exports_local": True,
            "global_allowed": True,
        }
    finally:
        await asyncio.gather(
            edge_a.close(),
            edge_b.close(),
            hub.close(),
        )


async def run(args) -> None:
    orchestrator = NatsComm(
        servers=[args.edge_a],
        jetstream_domain="edge-a",
    )
    receiver_a = NatsComm(
        servers=[args.edge_a],
        jetstream_domain="edge-a",
    )
    receiver_b = NatsComm(
        servers=[args.edge_b],
        jetstream_domain="edge-b",
    )
    instances = {
        "edge-a": "memory-local-a",
        "edge-b": "memory-global-b",
    }
    tasks = []
    payload = bytes([37]) * args.size_bytes
    expected = hashlib.sha256(payload).digest()

    async def handler(message):
        if hashlib.sha256(message.data).digest() != expected:
            raise ValueError("frame digest mismatch")
        return b"ok:" + expected

    try:
        await orchestrator.provision_memory_frame_stream(
            "edge-a",
            "detector",
            instances["edge-a"],
        )
        await orchestrator.provision_memory_frame_stream(
            "edge-b",
            "detector",
            instances["edge-b"],
        )
        tasks = [
            asyncio.create_task(
                receiver_a.serve_memory_frames(
                    agent_id="detector",
                    instance_id=instances["edge-a"],
                    local_cluster="edge-a",
                    handler=handler,
                )
            ),
            asyncio.create_task(
                receiver_b.serve_memory_frames(
                    agent_id="detector",
                    instance_id=instances["edge-b"],
                    local_cluster="edge-b",
                    handler=handler,
                )
            ),
        ]
        await wait_consumers(
            orchestrator,
            "edge-a",
            instances["edge-a"],
        )
        await wait_consumers(
            orchestrator,
            "edge-b",
            instances["edge-b"],
        )

        local_result = await orchestrator.request_memory_frame(
            target_cluster="edge-a",
            agent_id="detector",
            target_instance_id=instances["edge-a"],
            payload=payload,
            local_cluster="edge-a",
            request_id="local-frame-1",
        )
        global_result = await orchestrator.request_memory_frame(
            target_cluster="edge-b",
            agent_id="detector",
            target_instance_id=instances["edge-b"],
            payload=payload,
            local_cluster="edge-a",
            request_id="global-frame-1",
        )
        if local_result != b"ok:" + expected:
            raise RuntimeError("local Memory frame response mismatch")
        if global_result != b"ok:" + expected:
            raise RuntimeError("global Memory frame response mismatch")

        local_status = await orchestrator.memory_frame_stream_status(
            "edge-a",
            instances["edge-a"],
        )
        global_status = await orchestrator.memory_frame_stream_status(
            "edge-b",
            instances["edge-b"],
        )
        hub_js = orchestrator._nc.jetstream(domain="hub")
        hub_streams = [
            info.config.name for info in await hub_js.streams_info()
        ]
        if local_status["messages"] or global_status["messages"]:
            raise RuntimeError("ACK did not drain Memory frame Stream")
        if any(name.startswith("FRAME_") for name in hub_streams):
            raise RuntimeError(
                f"cloud hub must not store frame Streams: {hub_streams}"
            )
        deny = await test_leafnode_deny(args)
        print(
            json.dumps(
                {
                    "status": "passed",
                    "size_bytes": args.size_bytes,
                    "local": local_status,
                    "global": global_status,
                    "hub_streams": hub_streams,
                    "leafnode": deny,
                },
                sort_keys=True,
            )
        )
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for cluster, instance_id in instances.items():
            try:
                await orchestrator.delete_memory_frame_stream(
                    cluster,
                    instance_id,
                )
            except Exception:
                pass
        await asyncio.gather(
            orchestrator.close(),
            receiver_a.close(),
            receiver_b.close(),
        )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-a", default="nats://127.0.0.1:24223")
    parser.add_argument("--edge-b", default="nats://127.0.0.1:24224")
    parser.add_argument("--hub", default="nats://127.0.0.1:24222")
    parser.add_argument("--size-bytes", type=int, default=1024 * 1024)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
