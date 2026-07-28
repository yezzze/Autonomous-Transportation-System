import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nats.errors import TimeoutError as NatsTimeoutError

from runtime_api.nats_comm import NatsComm


class MemoryFrameRoutingTest(unittest.TestCase):
    def test_subject_and_stream_are_instance_scoped(self):
        comm = NatsComm()

        self.assertEqual(
            comm.memory_frame_stream_name("pod-uid-a"),
            "FRAME_pod-uid-a",
        )
        self.assertEqual(
            comm.memory_frame_subject(
                target_cluster="edge-b",
                agent_id="detector",
                target_instance_id="pod-uid-b",
                local_cluster="edge-a",
            ),
            "frame.global.edge-b.agent.detector."
            "instance.pod-uid-b.infer",
        )
        self.assertEqual(
            comm.memory_frame_subscription_subjects(
                agent_id="detector",
                instance_id="pod-uid-a",
                local_cluster="edge-a",
            ),
            (
                "frame.local.edge-a.agent.detector."
                "instance.pod-uid-a.infer",
                "frame.global.edge-a.agent.detector."
                "instance.pod-uid-a.infer",
            ),
        )


class MemoryFrameApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_controller_provisions_memory_stream(self):
        comm = NatsComm()
        js = Mock()
        comm._jetstream_for_domain = AsyncMock(return_value=js)

        with patch(
            "runtime_api.nats_comm.ensure_jetstream_stream",
            new=AsyncMock(),
        ) as ensure_stream:
            result = await comm.provision_memory_frame_stream(
                target_cluster="edge-a",
                agent_id="detector",
                instance_id="pod-uid-a",
            )

        self.assertEqual(result["stream"], "FRAME_pod-uid-a")
        self.assertEqual(result["storage"], "memory")
        ensure_stream.assert_awaited_once()
        call = ensure_stream.await_args
        self.assertEqual(call.args, (js,))
        self.assertEqual(call.kwargs["storage"], "memory")
        self.assertTrue(call.kwargs["replace_subjects"])
        self.assertEqual(
            call.kwargs["subjects"],
            [
                "frame.local.edge-a.agent.detector."
                "instance.pod-uid-a.>",
                "frame.global.edge-a.agent.detector."
                "instance.pod-uid-a.>",
            ],
        )

    async def test_request_publishes_to_target_domain_and_waits_for_reply(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = Mock()
        js.publish = AsyncMock()
        comm._jetstream_for_domain = AsyncMock(return_value=js)
        comm._nc.new_inbox = Mock(return_value="_INBOX.frame")
        response = SimpleNamespace(
            data=b"result",
            headers={"X-Frame-Request-Id": "frame-1"},
        )
        subscription = Mock()
        subscription.next_msg = AsyncMock(return_value=response)
        subscription.unsubscribe = AsyncMock()
        comm._nc.subscribe = AsyncMock(return_value=subscription)
        comm._nc.flush = AsyncMock()

        result = await comm.request_memory_frame(
            target_cluster="edge-b",
            agent_id="detector",
            target_instance_id="pod-uid-b",
            payload=b"frame",
            local_cluster="edge-a",
            timeout_sec=20,
            request_id="frame-1",
        )

        self.assertEqual(result, b"result")
        comm._jetstream_for_domain.assert_awaited_once_with("edge-b")
        publish = js.publish.await_args
        self.assertEqual(
            publish.args[:2],
            (
                "frame.global.edge-b.agent.detector."
                "instance.pod-uid-b.infer",
                b"frame",
            ),
        )
        self.assertEqual(publish.kwargs["stream"], "FRAME_pod-uid-b")
        self.assertEqual(
            publish.kwargs["headers"]["X-Frame-Reply"],
            "_INBOX.frame",
        )
        self.assertNotIn("Nats-Msg-Id", publish.kwargs["headers"])
        subscription.next_msg.assert_awaited_once()
        subscription.unsubscribe.assert_awaited_once()

    async def test_request_converts_reply_timeout(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        js = Mock()
        js.publish = AsyncMock()
        comm._jetstream_for_domain = AsyncMock(return_value=js)
        comm._nc.new_inbox = Mock(return_value="_INBOX.frame")
        subscription = Mock()
        subscription.next_msg = AsyncMock(side_effect=NatsTimeoutError())
        subscription.unsubscribe = AsyncMock()
        comm._nc.subscribe = AsyncMock(return_value=subscription)
        comm._nc.flush = AsyncMock()

        with self.assertRaises(TimeoutError):
            await comm.request_memory_frame(
                target_cluster="edge-a",
                agent_id="detector",
                target_instance_id="pod-uid-a",
                payload=b"frame",
                local_cluster="edge-a",
                timeout_sec=1,
                request_id="frame-1",
            )

    async def test_server_replies_then_acks_memory_frame(self):
        comm = NatsComm()
        comm.wait_memory_frame_stream = AsyncMock()
        js = Mock()
        comm._jetstream_for_domain = AsyncMock(return_value=js)
        comm._nc.publish = AsyncMock()
        comm._nc.flush = AsyncMock()
        handled = asyncio.Event()
        stop = asyncio.Event()

        raw = Mock()
        raw.subject = (
            "frame.local.edge-a.agent.detector."
            "instance.pod-uid-a.infer"
        )
        raw.data = b"frame"
        raw.headers = {
            "X-Frame-Reply": "_INBOX.frame",
            "X-Frame-Request-Id": "frame-1",
        }
        raw.metadata = SimpleNamespace(
            stream="FRAME_pod-uid-a",
            consumer="FRAME_pod-uid-a-local",
            sequence=SimpleNamespace(stream=1),
            num_delivered=1,
        )
        raw.ack_sync = AsyncMock()
        raw.nak = AsyncMock()
        raw.in_progress = AsyncMock()

        class PullSubscription:
            def __init__(self, message=None):
                self.message = message
                self.unsubscribe = AsyncMock()

            async def fetch(self, _batch, timeout):
                del timeout
                if self.message is not None:
                    message, self.message = self.message, None
                    return [message]
                await stop.wait()
                return []

        local = PullSubscription(raw)
        global_ = PullSubscription()
        js.pull_subscribe = AsyncMock(side_effect=[local, global_])

        async def handler(message):
            self.assertEqual(message.data, b"frame")
            self.assertEqual(message.request_id, "frame-1")
            handled.set()
            return b"result"

        task = asyncio.create_task(
            comm.serve_memory_frames(
                agent_id="detector",
                instance_id="pod-uid-a",
                local_cluster="edge-a",
                handler=handler,
                poll_timeout_sec=1,
            )
        )
        await asyncio.wait_for(handled.wait(), timeout=1)
        for _ in range(100):
            if raw.ack_sync.await_count:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        stop.set()

        comm._nc.publish.assert_awaited_once_with(
            "_INBOX.frame",
            b"result",
            headers={"X-Frame-Request-Id": "frame-1"},
        )
        raw.ack_sync.assert_awaited_once()
        raw.nak.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
