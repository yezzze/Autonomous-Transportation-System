#!/usr/bin/env python3
"""验证 Agent 自主管理的每实例 JetStream local/global 路由与清理。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_api import NatsComm  # noqa: E402


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


async def wait_stream_messages(
    comm: NatsComm,
    cluster: str,
    instance_id: str,
    expected: int,
):
    for _ in range(100):
        status = await comm.workflow_stream_status(cluster, instance_id)
        if status["messages"] == expected:
            return status
        await asyncio.sleep(0.01)
    raise RuntimeError(
        f"Stream message count did not become {expected}: {status}"
    )


async def run(args) -> None:
    edge_a = NatsComm(servers=[args.edge_a_nats_url])
    edge_b = NatsComm(servers=[args.edge_b_nats_url])
    orchestrator = NatsComm(servers=[args.edge_a_nats_url])
    try:
        stream_a = await edge_a.start_workflow_stream(
            agent_id=args.agent_a,
            instance_id=args.instance_a,
            local_cluster=args.edge_a_cluster_id,
        )
        stream_a_repeated = await edge_a.start_workflow_stream(
            agent_id=args.agent_a,
            instance_id=args.instance_a,
            local_cluster=args.edge_a_cluster_id,
        )
        if stream_a_repeated != stream_a:
            raise RuntimeError(
                f"repeated provision changed instance stream: "
                f"{stream_a_repeated} != {stream_a}"
            )
        stream_b = await edge_b.start_workflow_stream(
            agent_id=args.agent_b,
            instance_id=args.instance_b,
            local_cluster=args.edge_b_cluster_id,
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
        local_before_ack = await orchestrator.workflow_stream_status(
            target_cluster=args.edge_a_cluster_id,
            instance_id=args.instance_a,
        )
        if local_before_ack["messages"] != 1:
            raise RuntimeError(
                f"local Stream did not retain pending message: "
                f"{local_before_ack}"
            )
        local_payload = await receive_one(
            edge_a,
            local_subject,
            f"{args.agent_a}-local-integration",
        )
        local_after_ack = await wait_stream_messages(
            orchestrator,
            args.edge_a_cluster_id,
            args.instance_a,
            0,
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
        managed_streams = await orchestrator.list_workflow_streams(
            args.edge_a_cluster_id
        )
        if [item["stream"] for item in managed_streams] != [stream_a["stream"]]:
            raise RuntimeError(
                f"managed Stream listing mismatch: {managed_streams}"
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

        await edge_a.close()
        await edge_b.close()
        deleted_a = not (
            await orchestrator.workflow_stream_status(
                target_cluster=args.edge_a_cluster_id,
                instance_id=args.instance_a,
            )
        )["exists"]
        deleted_b = not (
            await orchestrator.workflow_stream_status(
                target_cluster=args.edge_b_cluster_id,
                instance_id=args.instance_b,
            )
        )["exists"]
        deleted_again = await orchestrator.delete_workflow_stream(
            target_cluster=args.edge_a_cluster_id,
            instance_id=args.instance_a,
        )
        if not deleted_a or not deleted_b or deleted_again:
            raise RuntimeError(
                "Agent-owned Stream cleanup is not idempotent: "
                f"first=({deleted_a}, {deleted_b}), again={deleted_again}"
            )
        cleanup = {
            args.edge_a_cluster_id: await stream_names(
                orchestrator,
                args.edge_a_cluster_id,
            ),
            args.edge_b_cluster_id: await stream_names(
                orchestrator,
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
                        "before_ack": local_before_ack,
                        "after_ack": local_after_ack,
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
                    "agent_lifecycle": {
                        "edge_a_deleted_on_close": deleted_a,
                        "edge_b_deleted_on_close": deleted_b,
                        "delete_missing_edge_a": deleted_again,
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
