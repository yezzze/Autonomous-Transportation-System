import json
import unittest
from unittest.mock import AsyncMock, Mock

from nats.errors import TimeoutError as NatsTimeoutError

from runtime_api.frame_comm import FrameComm
from runtime_api.nats_comm import NatsComm


class NatsCommLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_durable_pull_subscription_is_reused(self):
        comm = NatsComm()
        subscription = object()
        comm._js = AsyncMock()
        comm._js.pull_subscribe = AsyncMock(return_value=subscription)

        first = await comm._get_pull_subscription("workflow.tasks", "worker-1")
        second = await comm._get_pull_subscription("workflow.tasks", "worker-1")

        self.assertIs(first, subscription)
        self.assertIs(second, subscription)
        comm._js.pull_subscribe.assert_awaited_once_with(
            "workflow.tasks",
            durable="worker-1",
        )

    async def test_ephemeral_pull_subscription_is_not_cached(self):
        comm = NatsComm()
        first = object()
        second = object()
        comm._js = AsyncMock()
        comm._js.pull_subscribe = AsyncMock(side_effect=[first, second])

        self.assertIs(
            await comm._get_pull_subscription("workflow.reply.1", None),
            first,
        )
        self.assertIs(
            await comm._get_pull_subscription("workflow.reply.1", None),
            second,
        )
        self.assertEqual(comm._js.pull_subscribe.await_count, 2)

    async def test_ephemeral_pull_subscription_is_unsubscribed_on_timeout(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        subscription = AsyncMock()
        subscription.fetch.side_effect = NatsTimeoutError()
        comm._get_pull_subscription = AsyncMock(return_value=subscription)

        messages = await comm.receive(
            "workflow.reply.1",
            durable=None,
            timeout_sec=0.1,
        )

        self.assertEqual(messages, [])
        subscription.unsubscribe.assert_awaited_once()

    async def test_send_and_wait_uses_inbox_and_unsubscribes(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        comm.send = AsyncMock(return_value={"stream": "WORKFLOW", "seq": 7})
        comm._nc.new_inbox = Mock(return_value="_INBOX.test")
        subscription = AsyncMock()
        subscription.next_msg.return_value.data = json.dumps(
            {"result": "ok"}
        ).encode()
        comm._nc.subscribe = AsyncMock(return_value=subscription)
        comm._nc.flush = AsyncMock()

        reply = await comm.send_and_wait(
            "workflow.tasks",
            {"workflow_id": "wf-1"},
            timeout_sec=1,
        )

        self.assertEqual(reply, {"result": "ok"})
        comm._nc.subscribe.assert_awaited_once_with("_INBOX.test", max_msgs=1)
        comm.send.assert_awaited_once_with(
            "workflow.tasks",
            {
                "workflow_id": "wf-1",
                "reply_subject": "_INBOX.test",
            },
        )
        subscription.unsubscribe.assert_awaited_once()

    async def test_send_and_wait_unsubscribes_on_timeout(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        comm.send = AsyncMock(return_value={"stream": "WORKFLOW", "seq": 8})
        comm._nc.new_inbox = Mock(return_value="_INBOX.timeout")
        subscription = AsyncMock()
        subscription.next_msg.side_effect = NatsTimeoutError()
        comm._nc.subscribe = AsyncMock(return_value=subscription)
        comm._nc.flush = AsyncMock()

        with self.assertRaises(TimeoutError):
            await comm.send_and_wait(
                "workflow.tasks",
                {"workflow_id": "wf-2"},
                timeout_sec=0.1,
            )

        subscription.unsubscribe.assert_awaited_once()


class FrameCommLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_and_wait_uploads_frame_and_reuses_transport(self):
        nats = AsyncMock()
        nats.send_and_wait.return_value = {"result": "ok"}
        transport = Mock()
        transport.upload_bytes.return_value = {
            "transport": "grpc",
            "target": "frame-host:50051",
            "frame_id": "frame-1",
            "size_bytes": 3,
            "sha256": "a" * 64,
            "content_type": "application/octet-stream",
            "chunk_size": 1024,
        }
        comm = FrameComm(nats=nats, transport=transport)

        reply = await comm.send_and_wait(
            "workflow.tasks",
            {"workflow_id": "wf-frame"},
            frame_bytes=b"abc",
            timeout_sec=10,
        )

        self.assertEqual(reply, {"result": "ok"})
        transport.upload_bytes.assert_called_once_with(
            b"abc",
            "application/octet-stream",
        )
        nats.send_and_wait.assert_awaited_once_with(
            subject="workflow.tasks",
            payload={
                "workflow_id": "wf-frame",
                "frame_ref": transport.upload_bytes.return_value,
            },
            reply_subject=None,
            timeout_sec=10,
        )


if __name__ == "__main__":
    unittest.main()
