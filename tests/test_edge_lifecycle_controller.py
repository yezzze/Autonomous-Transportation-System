import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from control_api.lifecycle import (
    AGENT_ID_LABEL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    STREAM_ENABLED_LABEL,
    ControllerSettings,
    CreateInstanceRequest,
    EdgeLifecycleController,
    LifecycleError,
)


async def immediate(func, *args, **kwargs):
    return func(*args, **kwargs)


def ready_pod(name="detector-1", uid="pod-uid-a", agent_id="detector"):
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace="default",
            uid=uid,
            labels={
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                AGENT_ID_LABEL: agent_id,
                STREAM_ENABLED_LABEL: "true",
            },
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(name="agent", image="detector:v1")
            ]
        ),
        status=client.V1PodStatus(
            phase="Running",
            conditions=[
                client.V1PodCondition(
                    type="Ready",
                    status="True",
                )
            ],
            container_statuses=[],
        ),
    )


class FakeCoreApi:
    def __init__(self):
        self.pods = {}
        self.deleted = []

    def read_namespaced_pod(self, name, namespace):
        pod = self.pods.get((namespace, name))
        if pod is None:
            raise ApiException(status=404, reason="Not Found")
        return pod

    def create_namespaced_pod(self, namespace, body):
        body.metadata.namespace = namespace
        body.metadata.uid = f"uid-{body.metadata.name}"
        body.status = client.V1PodStatus(
            phase="Pending",
            conditions=[],
            container_statuses=[],
        )
        self.pods[(namespace, body.metadata.name)] = body
        return body

    def delete_namespaced_pod(self, name, namespace, **_kwargs):
        pod = self.pods.pop((namespace, name), None)
        if pod is None:
            raise ApiException(status=404, reason="Not Found")
        self.deleted.append((namespace, name))
        return SimpleNamespace()

    def list_namespaced_pod(self, namespace, **_kwargs):
        return SimpleNamespace(
            items=[
                pod
                for (pod_namespace, _), pod in self.pods.items()
                if pod_namespace == namespace
            ]
        )


def settings(**overrides):
    values = {
        "cluster_id": "edge-a",
        "allowed_namespaces": {"default"},
        "agent_nats_servers": "nats://nats:4222",
        "agent_service_account": None,
        "image_pull_secrets": [],
        "stream_prefix": "WF",
        "stream_max_bytes": "512MiB",
        "stream_provision_timeout_sec": 120,
        "reconcile_interval_sec": 0,
        "orphan_grace_sec": 0,
        "delete_empty_orphan_streams": True,
    }
    values.update(overrides)
    return ControllerSettings(**values)


def stream_status(messages=0, exists=True):
    return {
        "exists": exists,
        "stream": "WF_pod-uid-a",
        "domain": "edge-a",
        "instance_id": "pod-uid-a",
        "messages": messages,
        "bytes": messages * 100,
        "consumer_count": 1,
        "num_pending": messages,
        "num_ack_pending": 0,
        "consumers": [],
    }


class EdgeLifecycleControllerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.core = FakeCoreApi()
        self.nats = AsyncMock()
        self.nats.provision_workflow_stream.return_value = {
            "stream": "WF_uid-detector-1",
            "domain": "edge-a",
        }
        self.nats.workflow_stream_status.return_value = stream_status()
        self.nats.delete_workflow_stream.return_value = True
        self.controller = EdgeLifecycleController(
            core_api=self.core,
            nats=self.nats,
            settings=settings(),
            call_sync=immediate,
        )

    def test_build_pod_injects_instance_identity_and_rejects_reserved_env(self):
        request = CreateInstanceRequest(
            name="detector-1",
            agent_id="detector",
            image="detector:v1",
            env={"MODEL": "v2"},
        )

        pod = self.controller.build_pod(request)
        env = {item.name: item for item in pod.spec.containers[0].env}

        self.assertEqual(env["CLUSTER_ID"].value, "edge-a")
        self.assertEqual(env["AGENT_ID"].value, "detector")
        self.assertEqual(
            env["AGENT_INSTANCE_ID"].value_from.field_ref.field_path,
            "metadata.uid",
        )
        with self.assertRaises(ValidationError):
            CreateInstanceRequest(
                name="detector-2",
                agent_id="detector",
                image="detector:v1",
                env={"CLUSTER_ID": "wrong"},
            )

    async def test_create_pod_then_provision_stream(self):
        request = CreateInstanceRequest(
            name="detector-1",
            agent_id="detector",
            image="detector:v1",
        )

        result = await self.controller.create_instance(request)

        self.assertTrue(result["created"])
        self.assertEqual(result["instance_id"], "uid-detector-1")
        self.nats.provision_workflow_stream.assert_awaited_once_with(
            target_cluster="edge-a",
            agent_id="detector",
            instance_id="uid-detector-1",
        )

    async def test_repeated_create_reuses_same_pod_and_stream(self):
        pod = ready_pod()
        self.core.pods[("default", "detector-1")] = pod
        request = CreateInstanceRequest(
            name="detector-1",
            agent_id="detector",
            image="detector:v1",
        )

        first = await self.controller.create_instance(request)
        second = await self.controller.create_instance(request)

        self.assertFalse(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["instance_id"], second["instance_id"])
        self.assertEqual(
            self.nats.provision_workflow_stream.await_count,
            2,
        )

    async def test_stream_failure_rolls_back_new_pod(self):
        self.nats.provision_workflow_stream.side_effect = RuntimeError(
            "JetStream unavailable"
        )
        request = CreateInstanceRequest(
            name="detector-1",
            agent_id="detector",
            image="detector:v1",
        )

        with self.assertRaises(LifecycleError) as raised:
            await self.controller.create_instance(request)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn(("default", "detector-1"), self.core.pods)
        self.assertEqual(self.core.deleted, [("default", "detector-1")])

    async def test_delete_refuses_nonempty_stream_without_force(self):
        pod = ready_pod()
        self.core.pods[("default", "detector-1")] = pod
        self.nats.workflow_stream_status.return_value = stream_status(messages=3)

        with self.assertRaises(LifecycleError) as raised:
            await self.controller.delete_instance(
                namespace="default",
                name="detector-1",
                drain_timeout_sec=0,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(("default", "detector-1"), self.core.pods)
        self.nats.delete_workflow_stream.assert_not_awaited()

    async def test_force_delete_reports_dropped_messages(self):
        pod = ready_pod()
        self.core.pods[("default", "detector-1")] = pod
        self.nats.workflow_stream_status.return_value = stream_status(messages=3)

        result = await self.controller.delete_instance(
            namespace="default",
            name="detector-1",
            drain_timeout_sec=0,
            force=True,
        )

        self.assertTrue(result["pod_deleted"])
        self.assertTrue(result["stream_deleted"])
        self.assertEqual(result["dropped_messages"], 3)
        self.nats.delete_workflow_stream.assert_awaited_once_with(
            target_cluster="edge-a",
            instance_id="pod-uid-a",
        )

    async def test_reconcile_deletes_only_empty_orphan_stream(self):
        pod = ready_pod()
        self.core.pods[("default", "detector-1")] = pod
        created = datetime.now(timezone.utc).isoformat()
        self.nats.list_workflow_streams.return_value = [
            {
                "stream": "WF_pod-uid-a",
                "instance_id": "pod-uid-a",
                "messages": 0,
                "created": created,
            },
            {
                "stream": "WF_orphan-empty",
                "instance_id": "orphan-empty",
                "messages": 0,
                "created": created,
            },
            {
                "stream": "WF_orphan-pending",
                "instance_id": "orphan-pending",
                "messages": 2,
                "created": created,
            },
        ]

        result = await self.controller.reconcile_once()

        self.nats.provision_workflow_stream.assert_awaited_once_with(
            target_cluster="edge-a",
            agent_id="detector",
            instance_id="pod-uid-a",
        )
        self.nats.delete_workflow_stream.assert_awaited_once_with(
            target_cluster="edge-a",
            instance_id="orphan-empty",
        )
        self.assertEqual(
            result["deleted_empty_orphans"],
            ["WF_orphan-empty"],
        )
        self.assertEqual(len(result["orphan_streams"]), 2)

    async def test_reconcile_observes_grace_period_without_created_time(self):
        self.controller = EdgeLifecycleController(
            core_api=self.core,
            nats=self.nats,
            settings=settings(orphan_grace_sec=300),
            call_sync=immediate,
        )
        self.nats.list_workflow_streams.return_value = [
            {
                "stream": "WF_orphan-empty",
                "instance_id": "orphan-empty",
                "messages": 0,
                "created": None,
            }
        ]

        first = await self.controller.reconcile_once()

        self.nats.delete_workflow_stream.assert_not_awaited()
        self.assertLess(first["orphan_streams"][0]["age_sec"], 1)

        self.controller._orphan_first_seen["orphan-empty"] = (
            datetime.now(timezone.utc) - timedelta(seconds=301)
        )
        second = await self.controller.reconcile_once()

        self.nats.delete_workflow_stream.assert_awaited_once_with(
            target_cluster="edge-a",
            instance_id="orphan-empty",
        )
        self.assertEqual(
            second["deleted_empty_orphans"],
            ["WF_orphan-empty"],
        )


if __name__ == "__main__":
    unittest.main()
