#!/usr/bin/env python3
"""验证编排器管理的每实例 JetStream local/global 路由与清理。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_api import NatsComm


async def receive_one(
    comm: NatsComm,
    subject: str,
    durable: str,
):
    messages = await comm.receive(
        subject=subject,
        durable=durable,
        batch=1,
        timeout_sec=10,
    )
    if len(messages) != 1:
        raise RuntimeError(f"expected one message on {subject}, got {len(messages)}")
    await messages[0].ack()
    return messages[0].payload


async def stream_names(comm: NatsComm, domain: str):
    js = comm._nc.jetstream(domain=domain)
    return sorted(info.config.name for info in await js.streams_info())


async def run(args) -> None:
    edge_a = NatsComm(servers=[args.edge_a_nats_url])
    edge_b = NatsComm(servers=[args.edge_b_nats_url])
    orchestrator = NatsComm(servers=[args.edge_a_nats_url])
    try:
        stream_a = await orchestrator.provision_workflow_stream(
            target_cluster=args.edge_a_cluster_id,
            agent_id=args.agent_a,
            instance_id=args.instance_a,
        )
        stream_a_repeated = await orchestrator.provision_workflow_stream(
            target_cluster=args.edge_a_cluster_id,
            agent_id=args.agent_a,
            instance_id=args.instance_a,
        )
        if stream_a_repeated != stream_a:
            raise RuntimeError(
                f"repeated provision changed instance stream: "
                f"{stream_a_repeated} != {stream_a}"
            )
        stream_b = await orchestrator.provision_workflow_stream(
            target_cluster=args.edge_b_cluster_id,
            agent_id=args.agent_b,
            instance_id=args.instance_b,
        )

        local_subject = orchestrator.workflow_subject(
            target_cluster=args.edge_a_cluster_id,
            agent_id=args.agent_a,
            target_instance_id=args.instance_a,
            local_cluster=args.edge_a_cluster_id,
        )
        global_subject = orchestrator.workflow_subject(
            target_cluster=args.edge_b_cluster_id,
            agent_id=args.agent_b,
            target_instance_id=args.instance_b,
            local_cluster=args.edge_a_cluster_id,
        )

        await orchestrator.send_workflow(
            target_cluster=args.edge_a_cluster_id,
            agent_id=args.agent_a,
            target_instance_id=args.instance_a,
            payload={"route": "local", "sequence": 1},
            local_cluster=args.edge_a_cluster_id,
        )
        local_payload = await receive_one(
            edge_a,
            local_subject,
            f"{args.agent_a}-local-integration",
        )

        await orchestrator.send_workflow(
            target_cluster=args.edge_b_cluster_id,
            agent_id=args.agent_b,
            target_instance_id=args.instance_b,
            payload={"route": "global", "sequence": 2},
            local_cluster=args.edge_a_cluster_id,
        )
        global_payload = await receive_one(
            edge_b,
            global_subject,
            f"{args.agent_b}-global-integration",
        )
        edge_a_streams = await stream_names(edge_a, args.edge_a_cluster_id)
        edge_b_streams = await stream_names(edge_b, args.edge_b_cluster_id)
        hub_streams = await stream_names(edge_a, "hub")

        expected_a = stream_a["stream"]
        expected_b = stream_b["stream"]
        if edge_a_streams != [expected_a]:
            raise RuntimeError(f"unexpected edge-a streams: {edge_a_streams}")
        if edge_b_streams != [expected_b]:
            raise RuntimeError(f"unexpected edge-b streams: {edge_b_streams}")
        if hub_streams:
            raise RuntimeError(f"cloud hub must not store workflow: {hub_streams}")

        deleted_a = await orchestrator.delete_workflow_stream(
            target_cluster=args.edge_a_cluster_id,
            instance_id=args.instance_a,
        )
        deleted_b = await orchestrator.delete_workflow_stream(
            target_cluster=args.edge_b_cluster_id,
            instance_id=args.instance_b,
        )
        deleted_again = await orchestrator.delete_workflow_stream(
            target_cluster=args.edge_a_cluster_id,
            instance_id=args.instance_a,
        )
        if not deleted_a or not deleted_b or deleted_again:
            raise RuntimeError(
                "instance stream delete is not idempotent: "
                f"first=({deleted_a}, {deleted_b}), again={deleted_again}"
            )
        cleanup = {
            args.edge_a_cluster_id: await stream_names(
                edge_a,
                args.edge_a_cluster_id,
            ),
            args.edge_b_cluster_id: await stream_names(
                edge_b,
                args.edge_b_cluster_id,
            ),
        }
        if any(cleanup.values()):
            raise RuntimeError(f"instance stream cleanup failed: {cleanup}")

        print(
            json.dumps(
                {
                    "local": {
                        "subject": local_subject,
                        "stream": stream_a,
                        "payload": local_payload,
                    },
                    "global": {
                        "subject": global_subject,
                        "stream": stream_b,
                        "payload": global_payload,
                    },
                    "stream_placement": {
                        args.edge_a_cluster_id: edge_a_streams,
                        args.edge_b_cluster_id: edge_b_streams,
                        "hub": hub_streams,
                    },
                    "after_instance_cleanup": cleanup,
                    "delete_idempotency": {
                        "first_edge_a": deleted_a,
                        "first_edge_b": deleted_b,
                        "second_edge_a": deleted_again,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await orchestrator.close()
        await edge_b.close()
        await edge_a.close()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--edge-a-nats-url",
        default="nats://127.0.0.1:24223",
    )
    parser.add_argument(
        "--edge-b-nats-url",
        default="nats://127.0.0.1:24224",
    )
    parser.add_argument("--edge-a-cluster-id", default="edge-a")
    parser.add_argument("--edge-b-cluster-id", default="edge-b")
    parser.add_argument("--agent-a", default="workflow-agent-a")
    parser.add_argument("--agent-b", default="workflow-agent-b")
    parser.add_argument("--instance-a", default="pod-uid-a")
    parser.add_argument("--instance-b", default="pod-uid-b")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
