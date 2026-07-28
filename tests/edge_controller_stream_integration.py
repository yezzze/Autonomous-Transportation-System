import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from kubernetes import client
from kubernetes.client.exceptions import ApiException

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from control_api.lifecycle import (  # noqa: E402
    ControllerSettings,
    EdgeLifecycleController,
)
from control_api.main import app  # noqa: E402
from runtime_api import NatsComm  # noqa: E402


class MemoryCoreApi:
    def __init__(self):
        self.pods = {}

    def read_namespaced_pod(self, name, namespace):
        pod = self.pods.get((namespace, name))
        if pod is None:
            raise ApiException(status=404, reason="Not Found")
        return pod

    def create_namespaced_pod(self, namespace, body):
        body.metadata.namespace = namespace
        body.metadata.uid = f"uid-{body.metadata.name}"
        body.status = client.V1PodStatus(
            phase="Running",
            conditions=[
                client.V1PodCondition(type="Ready", status="True")
            ],
            container_statuses=[],
        )
        self.pods[(namespace, body.metadata.name)] = body
        return body

    def delete_namespaced_pod(self, name, namespace, **_kwargs):
        pod = self.pods.pop((namespace, name), None)
        if pod is None:
            raise ApiException(status=404, reason="Not Found")
        return SimpleNamespace()

    def list_namespaced_pod(self, namespace, **_kwargs):
        return SimpleNamespace(
            items=[
                pod
                for (pod_namespace, _name), pod in self.pods.items()
                if pod_namespace == namespace
            ]
        )


async def immediate(func, *args, **kwargs):
    return func(*args, **kwargs)


def settings(args) -> ControllerSettings:
    return ControllerSettings(
        cluster_id=args.cluster,
        allowed_namespaces={"default"},
        agent_nats_servers=args.server,
        agent_service_account=None,
        image_pull_secrets=[],
        stream_prefix="WF",
        stream_max_bytes="512MiB",
        frame_stream_prefix="FRAME",
        frame_stream_max_bytes="512MiB",
        frame_stream_max_age_sec=120,
        frame_ack_wait_sec=60,
        frame_max_deliver=3,
        stream_provision_timeout_sec=10,
        reconcile_interval_sec=0,
        orphan_grace_sec=0,
        delete_empty_orphan_streams=True,
    )


async def run(args) -> None:
    core = MemoryCoreApi()
    nats_comm = NatsComm(
        servers=[args.server],
        jetstream_domain=args.cluster,
    )
    controller = EdgeLifecycleController(
        core_api=core,
        nats=nats_comm,
        settings=settings(args),
        call_sync=immediate,
    )
    app.state.controller = controller
    app.state.api_token = args.token
    instance_id = f"uid-{args.name}"
    headers = {"Authorization": f"Bearer {args.token}"}
    body = {
        "name": args.name,
        "namespace": "default",
        "agent_id": args.agent,
        "image": "controller-stream-test:latest",
        "workflow_stream": True,
        "frame_stream": True,
    }
    transport = httpx.ASGITransport(app=app)

    await controller.start()
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://controller.test",
        ) as http:
            created = await http.post(
                "/v1/instances",
                headers=headers,
                json=body,
            )
            created.raise_for_status()
            create_result = created.json()
            if create_result["instance_id"] != instance_id:
                raise RuntimeError("controller returned an unexpected Pod UID")

            workflow = await nats_comm.workflow_stream_status(
                args.cluster,
                instance_id,
            )
            frame = await nats_comm.memory_frame_stream_status(
                args.cluster,
                instance_id,
            )
            if not workflow["exists"] or not frame["exists"]:
                raise RuntimeError("controller did not create both Streams")
            if frame["storage"] != "memory":
                raise RuntimeError(
                    f"frame Stream storage must be memory: {frame}"
                )

            first_streams = {
                info.config.name
                for info in await nats_comm._nc.jetstream(
                    domain=args.cluster
                ).streams_info()
            }
            repeated = await http.post(
                "/v1/instances",
                headers=headers,
                json=body,
            )
            repeated.raise_for_status()
            repeat_result = repeated.json()
            second_streams = {
                info.config.name
                for info in await nats_comm._nc.jetstream(
                    domain=args.cluster
                ).streams_info()
            }
            if repeat_result["created"]:
                raise RuntimeError("repeated create must reuse the Pod")
            if first_streams != second_streams:
                raise RuntimeError("repeated create changed Stream resources")

            status = await http.get(
                f"/v1/instances/default/{args.name}",
                headers=headers,
            )
            status.raise_for_status()
            status_result = status.json()
            if not status_result["stream"]["exists"]:
                raise RuntimeError("workflow Stream status is missing")
            if not status_result["frame_stream"]["exists"]:
                raise RuntimeError("frame Stream status is missing")

            deleted = await http.delete(
                f"/v1/instances/default/{args.name}",
                headers=headers,
                params={
                    "instance_id": instance_id,
                    "drain_timeout_sec": 5,
                },
            )
            deleted.raise_for_status()
            delete_result = deleted.json()
            if not delete_result["pod_deleted"]:
                raise RuntimeError("controller did not delete the Pod")
            if not delete_result["workflow_stream_deleted"]:
                raise RuntimeError(
                    "controller did not delete the workflow Stream"
                )
            if not delete_result["frame_stream_deleted"]:
                raise RuntimeError(
                    "controller did not delete the frame Stream"
                )

            workflow_after = await nats_comm.workflow_stream_status(
                args.cluster,
                instance_id,
            )
            frame_after = await nats_comm.memory_frame_stream_status(
                args.cluster,
                instance_id,
            )
            if workflow_after["exists"] or frame_after["exists"]:
                raise RuntimeError("instance Streams remain after DELETE")

            print(
                json.dumps(
                    {
                        "status": "passed",
                        "create": {
                            "instance_id": create_result["instance_id"],
                            "workflow_stream": (
                                create_result["workflow_stream"]["stream"]
                            ),
                            "frame_stream": (
                                create_result["frame_stream"]["stream"]
                            ),
                        },
                        "repeated_create_reused": True,
                        "query_reported_both_streams": True,
                        "delete": delete_result,
                        "streams_absent_after_delete": True,
                    },
                    sort_keys=True,
                )
            )
    finally:
        try:
            await nats_comm.delete_workflow_stream(
                args.cluster,
                instance_id,
            )
            await nats_comm.delete_memory_frame_stream(
                args.cluster,
                instance_id,
            )
        except Exception:
            pass
        await controller.close()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="nats://127.0.0.1:24223")
    parser.add_argument("--cluster", default="edge-a")
    parser.add_argument("--name", default="controller-stream-test")
    parser.add_argument("--agent", default="detector")
    parser.add_argument("--token", default="controller-test-token")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
