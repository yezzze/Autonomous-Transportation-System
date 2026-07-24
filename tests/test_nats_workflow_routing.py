import unittest
from unittest.mock import AsyncMock, Mock, patch

from runtime_api.jetstream_stream import ensure_jetstream_stream
from runtime_api.nats_comm import NatsComm


class NatsWorkflowRoutingTest(unittest.TestCase):
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

    async def test_orchestrator_provisions_instance_stream(self):
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

    async def test_orchestrator_deletes_finished_instance_stream(self):
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

    async def test_agent_waits_for_orchestrator_provisioned_stream(self):
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
        comm.wait_workflow_stream = AsyncMock()
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

        comm.wait_workflow_stream.assert_awaited_once_with(
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


if __name__ == "__main__":
    unittest.main()
