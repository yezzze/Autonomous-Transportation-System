import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from runtime_api.jetstream_stream import ensure_jetstream_stream
from runtime_api.nats_comm import NatsComm


class NatsWorkflowRoutingTest(unittest.TestCase):
    def test_agent_environment_defines_default_instance_stream(self):
        with patch.dict(
            os.environ,
            {
                "CLUSTER_ID": "edge-a",
                "AGENT_ID": "detector",
                "AGENT_INSTANCE_ID": "pod-uid-env",
                "NATS_WORKFLOW_STREAM_PREFIX": "WF",
            },
            clear=True,
        ):
            comm = NatsComm()

        self.assertEqual(comm.stream, "WF_pod-uid-env")
        self.assertEqual(
            comm.stream_subjects,
            [
                "workflow.local.edge-a.agent.detector."
                "instance.pod-uid-env.>",
                "workflow.global.edge-a.agent.detector."
                "instance.pod-uid-env.>",
            ],
        )
        self.assertEqual(
            comm._default_workflow_identity,
            ("edge-a", "detector", "pod-uid-env"),
        )
        self.assertEqual(comm.jetstream_domain, "edge-a")

    def test_agent_domain_must_match_local_cluster(self):
        with patch.dict(
            os.environ,
            {
                "CLUSTER_ID": "edge-a",
                "AGENT_ID": "detector",
                "AGENT_INSTANCE_ID": "pod-uid-env",
                "NATS_JETSTREAM_DOMAIN": "hub",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "NATS_JETSTREAM_DOMAIN must equal CLUSTER_ID",
            ):
                NatsComm()

    def test_non_agent_client_keeps_hub_default_domain(self):
        with patch.dict(os.environ, {}, clear=True):
            comm = NatsComm()

        self.assertEqual(comm.jetstream_domain, "hub")

    def test_incomplete_agent_environment_does_not_fall_back_to_shared_stream(self):
        with patch.dict(
            os.environ,
            {"AGENT_INSTANCE_ID": "pod-uid-env"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "CLUSTER_ID is required"):
                NatsComm()

    def test_explicit_stream_keeps_legacy_compatibility_mode(self):
        with patch.dict(
            os.environ,
            {
                "CLUSTER_ID": "edge-a",
                "AGENT_ID": "detector",
                "AGENT_INSTANCE_ID": "pod-uid-env",
                "NATS_STREAM": "CUSTOM",
                "NATS_STREAM_SUBJECTS": "custom.>",
            },
            clear=True,
        ):
            comm = NatsComm()

        self.assertEqual(comm.stream, "CUSTOM")
        self.assertEqual(comm.stream_subjects, ["custom.>"])
        self.assertIsNone(comm._default_workflow_identity)

    def test_workflow_subject_uses_local_scope_for_same_cluster(self):
        comm = NatsComm()

        subject = comm.workflow_subject(
            target_cluster="edge-a",
            agent_id="detector",
            target_instance_id="pod-uid-a",
            local_cluster="edge-a",
        )

        self.assertEqual(
            subject,
            "workflow.local.edge-a.agent.detector.instance.pod-uid-a.in",
        )

    def test_workflow_subject_uses_global_scope_for_other_cluster(self):
        comm = NatsComm()

        subject = comm.workflow_subject(
            target_cluster="edge-b",
            agent_id="detector",
            target_instance_id="pod-uid-b",
            operation="start",
            local_cluster="edge-a",
        )

        self.assertEqual(
            subject,
            "workflow.global.edge-b.agent.detector.instance.pod-uid-b.start",
        )

    def test_workflow_subscription_subjects_include_both_routes(self):
        comm = NatsComm()

        subjects = comm.workflow_subscription_subjects(
            agent_id="detector",
            local_cluster="edge-a",
            instance_id="pod-uid-a",
        )

        self.assertEqual(
            subjects,
            (
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.in",
                "workflow.global.edge-a.agent.detector.instance.pod-uid-a.in",
            ),
        )

    def test_each_instance_has_an_independent_stream(self):
        comm = NatsComm()

        self.assertEqual(comm.workflow_stream_name("pod-uid-a"), "WF_pod-uid-a")
        self.assertNotEqual(
            comm.workflow_stream_name("pod-uid-a"),
            comm.workflow_stream_name("pod-uid-b"),
        )
        self.assertEqual(
            comm.workflow_stream_subjects(
                "edge-a",
                "detector",
                "pod-uid-a",
            ),
            (
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.>",
                "workflow.global.edge-a.agent.detector.instance.pod-uid-a.>",
            ),
        )

    def test_instance_id_rejects_unsafe_stream_characters(self):
        comm = NatsComm()

        with self.assertRaises(ValueError):
            comm.workflow_stream_name("pod/uid")


class NatsWorkflowApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_create_returns_connected_environment_instance(self):
        with patch.dict(
            os.environ,
            {
                "CLUSTER_ID": "edge-a",
                "AGENT_ID": "detector",
                "AGENT_INSTANCE_ID": "pod-uid-env",
            },
            clear=True,
        ), patch.object(
            NatsComm,
            "connect",
            new=AsyncMock(),
        ) as connect:
            comm = await NatsComm.create()

        self.assertEqual(comm.stream, "WF_pod-uid-env")
        self.assertEqual(
            comm.stream_subjects,
            [
                "workflow.local.edge-a.agent.detector."
                "instance.pod-uid-env.>",
                "workflow.global.edge-a.agent.detector."
                "instance.pod-uid-env.>",
            ],
        )
        connect.assert_awaited_once_with()
        comm._closed = True

    async def test_connect_creates_and_registers_environment_instance_stream(self):
        with patch.dict(
            os.environ,
            {
                "CLUSTER_ID": "edge-a",
                "AGENT_ID": "detector",
                "AGENT_INSTANCE_ID": "pod-uid-env",
                "NATS_JETSTREAM_DOMAIN": "edge-a",
            },
            clear=True,
        ):
            comm = NatsComm()

        client = Mock()
        client.is_connected = False
        client.connect = AsyncMock()
        js = Mock()
        client.jetstream = Mock(return_value=js)
        comm._nc = client

        with patch(
            "runtime_api.nats_comm.ensure_jetstream_stream",
            new=AsyncMock(),
        ) as ensure_stream:
            await comm.connect()

        client.jetstream.assert_called_once_with(domain="edge-a")
        ensure_stream.assert_awaited_once_with(
            js,
            name="WF_pod-uid-env",
            subjects=[
                "workflow.local.edge-a.agent.detector."
                "instance.pod-uid-env.>",
                "workflow.global.edge-a.agent.detector."
                "instance.pod-uid-env.>",
            ],
            replace_subjects=True,
        )
        self.assertEqual(
            comm._managed_workflow_streams,
            {("edge-a", "pod-uid-env")},
        )
        comm._managed_workflow_streams.clear()
        comm._closed = True

    async def test_instance_stream_replaces_stale_subjects(self):
        js = Mock()
        info = Mock()
        info.config.subjects = ["workflow.>"]
        info.config.max_bytes = 0
        info.config.discard = ""
        js.stream_info = AsyncMock(return_value=info)
        js.update_stream = AsyncMock()

        result = await ensure_jetstream_stream(
            js,
            name="WF_pod-uid-a",
            subjects=[
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.>",
                "workflow.global.edge-a.agent.detector.instance.pod-uid-a.>",
            ],
            replace_subjects=True,
        )

        applied = js.update_stream.await_args.args[0]
        self.assertEqual(
            applied.subjects,
            [
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.>",
                "workflow.global.edge-a.agent.detector.instance.pod-uid-a.>",
            ],
        )
        self.assertTrue(result["updated"])

    async def test_target_domain_context_is_reused_without_creating_stream(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = Mock()
        comm._nc.jetstream = Mock(return_value=js)

        with patch(
            "runtime_api.nats_comm.ensure_jetstream_stream",
            new=AsyncMock(),
        ) as ensure_stream:
            first, first_stream = await comm._jetstream_for_subject(
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.in"
            )
            second, second_stream = await comm._jetstream_for_subject(
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.start"
            )

        self.assertIs(first, js)
        self.assertIs(second, js)
        self.assertEqual(first_stream, "WF_pod-uid-a")
        self.assertEqual(second_stream, "WF_pod-uid-a")
        comm._nc.jetstream.assert_called_once_with(domain="edge-a")
        ensure_stream.assert_not_awaited()

    async def test_send_workflow_routes_to_exact_instance(self):
        comm = NatsComm()
        comm.send = AsyncMock(return_value={"stream": "WF_pod-uid-b", "seq": 1})

        result = await comm.send_workflow(
            target_cluster="edge-b",
            agent_id="detector",
            target_instance_id="pod-uid-b",
            payload={"workflow_id": "wf-1"},
            local_cluster="edge-a",
        )

        self.assertEqual(result["seq"], 1)
        comm.send.assert_awaited_once_with(
            "workflow.global.edge-b.agent.detector.instance.pod-uid-b.in",
            {"workflow_id": "wf-1"},
        )

    async def test_send_constrains_workflow_publish_to_target_stream(self):
        comm = NatsComm()
        js = AsyncMock()
        js.publish.return_value = SimpleNamespace(
            stream="WF_pod-uid-b",
            seq=7,
        )
        comm._jetstream_for_subject = AsyncMock(
            return_value=(js, "WF_pod-uid-b")
        )
        subject = (
            "workflow.global.edge-b.agent.detector."
            "instance.pod-uid-b.in"
        )

        result = await comm.send(subject, {"workflow_id": "wf-1"})

        self.assertEqual(result["stream"], "WF_pod-uid-b")
        js.publish.assert_awaited_once_with(
            subject,
            b'{"workflow_id":"wf-1"}',
            stream="WF_pod-uid-b",
        )

    async def test_send_rejects_ack_from_non_target_stream(self):
        comm = NatsComm()
        js = AsyncMock()
        js.publish.return_value = SimpleNamespace(stream="HUB_SHARED", seq=1)
        comm._jetstream_for_subject = AsyncMock(
            return_value=(js, "WF_pod-uid-b")
        )

        with self.assertRaisesRegex(RuntimeError, "非目标 Stream"):
            await comm.send(
                "workflow.global.edge-b.agent.detector."
                "instance.pod-uid-b.in",
                {"workflow_id": "wf-1"},
            )

    async def test_provisions_instance_stream(self):
        comm = NatsComm()
        js = AsyncMock()
        comm._jetstream_for_subject = AsyncMock(
            return_value=(js, "WF_pod-uid-a")
        )

        with patch(
            "runtime_api.nats_comm.ensure_jetstream_stream",
            new=AsyncMock(),
        ) as ensure_stream:
            result = await comm.provision_workflow_stream(
                target_cluster="edge-a",
                agent_id="detector",
                instance_id="pod-uid-a",
            )
            repeated = await comm.provision_workflow_stream(
                target_cluster="edge-a",
                agent_id="detector",
                instance_id="pod-uid-a",
            )

        self.assertEqual(repeated, result)
        self.assertEqual(
            result,
            {
                "stream": "WF_pod-uid-a",
                "domain": "edge-a",
                "agent_id": "detector",
                "instance_id": "pod-uid-a",
                "subjects": [
                    "workflow.local.edge-a.agent.detector.instance.pod-uid-a.>",
                    "workflow.global.edge-a.agent.detector.instance.pod-uid-a.>",
                ],
            },
        )
        self.assertEqual(ensure_stream.await_count, 2)
        for call in ensure_stream.await_args_list:
            self.assertEqual(call.args, (js,))
            self.assertEqual(
                call.kwargs,
                {
                    "name": "WF_pod-uid-a",
                    "subjects": [
                        "workflow.local.edge-a.agent.detector.instance.pod-uid-a.>",
                        "workflow.global.edge-a.agent.detector.instance.pod-uid-a.>",
                    ],
                    "replace_subjects": True,
                },
            )

    async def test_deletes_finished_instance_stream(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = AsyncMock()
        comm._nc.jetstream = Mock(return_value=js)

        deleted = await comm.delete_workflow_stream(
            target_cluster="edge-a",
            instance_id="pod-uid-a",
        )

        self.assertTrue(deleted)
        comm._nc.jetstream.assert_called_once_with(domain="edge-a")
        js.delete_stream.assert_awaited_once_with("WF_pod-uid-a")

    async def test_workflow_stream_status_aggregates_consumer_pending(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = Mock()
        js.stream_info = AsyncMock(
            return_value=SimpleNamespace(
                created=datetime(2026, 7, 24, tzinfo=timezone.utc),
                config=SimpleNamespace(
                    subjects=[
                        "workflow.local.edge-a.agent.detector."
                        "instance.pod-uid-a.>"
                    ]
                ),
                state=SimpleNamespace(messages=4, bytes=1024),
            )
        )
        js.consumers_info = AsyncMock(
            return_value=[
                SimpleNamespace(
                    name="worker-local",
                    num_pending=2,
                    num_ack_pending=1,
                    num_redelivered=0,
                ),
                SimpleNamespace(
                    name="worker-global",
                    num_pending=1,
                    num_ack_pending=0,
                    num_redelivered=1,
                ),
            ]
        )
        comm._nc.jetstream = Mock(return_value=js)

        result = await comm.workflow_stream_status(
            target_cluster="edge-a",
            instance_id="pod-uid-a",
        )

        self.assertTrue(result["exists"])
        self.assertEqual(result["messages"], 4)
        self.assertEqual(result["num_pending"], 3)
        self.assertEqual(result["num_ack_pending"], 1)
        self.assertEqual(result["consumer_count"], 2)

    async def test_list_workflow_streams_filters_legacy_streams(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = Mock()
        created = datetime(2026, 7, 24, tzinfo=timezone.utc)
        js.streams_info = AsyncMock(
            return_value=[
                SimpleNamespace(
                    created=created,
                    config=SimpleNamespace(
                        name="WF_pod-uid-a",
                        subjects=["workflow.local.edge-a.>"],
                    ),
                    state=SimpleNamespace(
                        messages=0,
                        bytes=0,
                        consumer_count=1,
                    ),
                ),
                SimpleNamespace(
                    created=created,
                    config=SimpleNamespace(
                        name="WORKFLOW_LEGACY",
                        subjects=["legacy.workflow.>"],
                    ),
                    state=SimpleNamespace(
                        messages=0,
                        bytes=0,
                        consumer_count=0,
                    ),
                ),
            ]
        )
        comm._nc.jetstream = Mock(return_value=js)

        result = await comm.list_workflow_streams("edge-a")

        self.assertEqual([item["stream"] for item in result], ["WF_pod-uid-a"])
        self.assertEqual(result[0]["instance_id"], "pod-uid-a")

    async def test_waits_for_controller_provisioned_stream(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = Mock()
        info = Mock()
        info.config.subjects = [
            "workflow.local.edge-a.agent.detector.instance.pod-uid-a.>",
            "workflow.global.edge-a.agent.detector.instance.pod-uid-a.>",
        ]
        js.stream_info = AsyncMock(return_value=info)
        comm._nc.jetstream = Mock(return_value=js)

        result = await comm.wait_workflow_stream(
            agent_id="detector",
            instance_id="pod-uid-a",
            local_cluster="edge-a",
            timeout_sec=1,
        )

        self.assertEqual(result["stream"], "WF_pod-uid-a")
        comm.connect.assert_awaited_once_with(ensure_stream=False)
        js.stream_info.assert_awaited_once_with("WF_pod-uid-a")

    async def test_serve_workflow_registers_instance_local_and_global(self):
        comm = NatsComm()
        comm.start_workflow_stream = AsyncMock()
        comm.serve = AsyncMock()
        handler = AsyncMock()

        await comm.serve_workflow(
            agent_id="detector",
            durable="pod-uid-a",
            handler=handler,
            local_cluster="edge-a",
            instance_id="pod-uid-a",
            max_inflight=2,
        )

        comm.start_workflow_stream.assert_awaited_once_with(
            agent_id="detector",
            local_cluster="edge-a",
            instance_id="pod-uid-a",
        )
        self.assertEqual(comm.serve.await_count, 2)
        self.assertEqual(
            [call.kwargs["subject"] for call in comm.serve.await_args_list],
            [
                "workflow.local.edge-a.agent.detector.instance.pod-uid-a.in",
                "workflow.global.edge-a.agent.detector.instance.pod-uid-a.in",
            ],
        )
        self.assertEqual(
            {
                call.kwargs["durable"]
                for call in comm.serve.await_args_list
            },
            {"pod-uid-a-local", "pod-uid-a-global"},
        )

    async def test_start_registers_workflow_stream_for_close_cleanup(self):
        comm = NatsComm()
        comm.provision_workflow_stream = AsyncMock(
            return_value={
                "stream": "WF_pod-uid-a",
                "domain": "edge-a",
            }
        )

        result = await comm.start_workflow_stream(
            agent_id="detector",
            instance_id="pod-uid-a",
            local_cluster="edge-a",
        )

        self.assertEqual(result["stream"], "WF_pod-uid-a")
        self.assertEqual(
            comm._managed_workflow_streams,
            {("edge-a", "pod-uid-a")},
        )

    async def test_close_deletes_managed_workflow_stream(self):
        comm = NatsComm()
        comm._managed_workflow_streams.add(("edge-a", "pod-uid-a"))
        comm.delete_workflow_stream = AsyncMock(return_value=True)

        await comm.close()
        await comm.close()

        comm.delete_workflow_stream.assert_awaited_once_with(
            target_cluster="edge-a",
            instance_id="pod-uid-a",
        )
        self.assertEqual(comm._managed_workflow_streams, set())

    async def test_controller_managed_workflow_only_waits(self):
        comm = NatsComm()
        comm.wait_workflow_stream = AsyncMock()
        comm.start_workflow_stream = AsyncMock()
        comm.serve = AsyncMock()

        await comm.serve_workflow(
            agent_id="detector",
            durable="pod-uid-a",
            handler=AsyncMock(),
            local_cluster="edge-a",
            instance_id="pod-uid-a",
            manage_stream_lifecycle=False,
        )

        comm.wait_workflow_stream.assert_awaited_once_with(
            agent_id="detector",
            local_cluster="edge-a",
            instance_id="pod-uid-a",
        )
        comm.start_workflow_stream.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
