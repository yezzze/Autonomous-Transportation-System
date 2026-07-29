#!/usr/bin/env python3
"""模拟一个 Agent 实例的 WF/FRAME 创建、消费和关闭删除生命周期。"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime_api import NatsComm  # noqa: E402


async def wait_until_ready(
    observer: NatsComm,
    cluster: str,
    instance_id: str,
    timeout_sec: float,
):
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while True:
        workflow = await observer.workflow_stream_status(
            cluster,
            instance_id,
        )
        frame = await observer.memory_frame_stream_status(
            cluster,
            instance_id,
        )
        if (
            workflow["exists"]
            and workflow["consumer_count"] == 2
            and frame["exists"]
            and frame["consumer_count"] == 2
        ):
            return workflow, frame
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                "Agent Stream/Consumer readiness timeout: "
                f"workflow={workflow}, frame={frame}"
            )
        await asyncio.sleep(0.05)


async def wait_until_drained(
    observer: NatsComm,
    cluster: str,
    instance_id: str,
    timeout_sec: float,
):
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while True:
        workflow = await observer.workflow_stream_status(
            cluster,
            instance_id,
        )
        frame = await observer.memory_frame_stream_status(
            cluster,
            instance_id,
        )
        if workflow["messages"] == 0 and frame["messages"] == 0:
            return workflow, frame
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                "Agent Stream drain timeout: "
                f"workflow={workflow}, frame={frame}"
            )
        await asyncio.sleep(0.05)


async def run(args) -> None:
    instance_id = args.instance_id or f"manual-{uuid.uuid4().hex}"
    os.environ["AGENT_ID"] = args.agent_id
    os.environ["AGENT_INSTANCE_ID"] = instance_id
    os.environ["CLUSTER_ID"] = args.cluster
    os.environ["NATS_JETSTREAM_DOMAIN"] = args.cluster

    agent = NatsComm(
        servers=[args.nats_url],
        jetstream_domain=args.cluster,
    )
    observer = NatsComm(
        servers=[args.nats_url],
        jetstream_domain=args.cluster,
    )
    workflow_received = asyncio.Event()
    frame_payload = bytes([37]) * args.frame_size_bytes
    frame_digest = hashlib.sha256(frame_payload).digest()
    tasks = []

    async def workflow_handler(payload):
        if payload != {"kind": "lifecycle", "sequence": 1}:
            raise ValueError(f"unexpected workflow payload: {payload}")
        workflow_received.set()

    async def frame_handler(message):
        if hashlib.sha256(message.data).digest() != frame_digest:
            raise ValueError("frame digest mismatch")
        return b"frame-ok:" + frame_digest

    try:
        tasks = [
            asyncio.create_task(
                agent.serve_workflow(
                    agent_id=args.agent_id,
                    durable=instance_id,
                    local_cluster=args.cluster,
                    handler=workflow_handler,
                    poll_timeout_sec=0.2,
                )
            ),
            asyncio.create_task(
                agent.serve_memory_frames(
                    agent_id=args.agent_id,
                    local_cluster=args.cluster,
                    handler=frame_handler,
                    poll_timeout_sec=0.2,
                    max_inflight=1,
                )
            ),
        ]
        workflow_ready, frame_ready = await wait_until_ready(
            observer,
            args.cluster,
            instance_id,
            args.timeout_sec,
        )

        workflow_ack = await observer.send_workflow(
            target_cluster=args.cluster,
            agent_id=args.agent_id,
            target_instance_id=instance_id,
            payload={"kind": "lifecycle", "sequence": 1},
            local_cluster=args.cluster,
        )
        await asyncio.wait_for(
            workflow_received.wait(),
            timeout=args.timeout_sec,
        )
        frame_reply = await observer.request_memory_frame(
            target_cluster=args.cluster,
            agent_id=args.agent_id,
            target_instance_id=instance_id,
            payload=frame_payload,
            local_cluster=args.cluster,
            timeout_sec=args.timeout_sec,
            request_id="lifecycle-frame-1",
        )
        expected_reply = b"frame-ok:" + frame_digest
        if frame_reply != expected_reply:
            raise RuntimeError("unexpected frame reply")

        workflow_drained, frame_drained = await wait_until_drained(
            observer,
            args.cluster,
            instance_id,
            args.timeout_sec,
        )

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        await agent.close()

        workflow_after = await observer.workflow_stream_status(
            args.cluster,
            instance_id,
        )
        frame_after = await observer.memory_frame_stream_status(
            args.cluster,
            instance_id,
        )
        if workflow_after["exists"] or frame_after["exists"]:
            raise RuntimeError(
                "Agent close did not delete instance Streams: "
                f"workflow={workflow_after}, frame={frame_after}"
            )

        print(
            json.dumps(
                {
                    "status": "passed",
                    "cluster": args.cluster,
                    "agent_id": args.agent_id,
                    "instance_id": instance_id,
                    "frame_size_bytes": args.frame_size_bytes,
                    "created": {
                        "workflow": workflow_ready["stream"],
                        "frame": frame_ready["stream"],
                    },
                    "workflow_pub_ack": workflow_ack,
                    "drained": {
                        "workflow_messages": workflow_drained["messages"],
                        "frame_messages": frame_drained["messages"],
                    },
                    "deleted_on_close": {
                        "workflow": not workflow_after["exists"],
                        "frame": not frame_after["exists"],
                    },
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await agent.close()
        await observer.close()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--nats-url",
        default="nats://127.0.0.1:24223",
    )
    parser.add_argument("--cluster", default="edge-a")
    parser.add_argument("--agent-id", default="lifecycle-agent")
    parser.add_argument("--instance-id")
    parser.add_argument(
        "--frame-size-bytes",
        type=int,
        default=15 * 1024 * 1024,
    )
    parser.add_argument("--timeout-sec", type=float, default=60)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
