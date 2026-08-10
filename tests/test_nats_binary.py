import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import DeliverPolicy
from nats.js.errors import BadRequestError

from runtime_api.nats_comm import NatsComm


class NatsBinaryRoutingTest(unittest.TestCase):
    def test_frame_subject_uses_local_scope_for_same_cluster(self):
        comm = NatsComm()

        subject = comm.frame_subject(
            target_cluster="edge-a",
            agent_id="detector",
            local_cluster="edge-a",
        )

        self.assertEqual(subject, "frame.local.edge-a.detector.infer")

    def test_frame_subject_uses_global_scope_for_other_cluster(self):
        comm = NatsComm()

        subject = comm.frame_subject(
            target_cluster="edge-b",
            agent_id="detector",
            operation="segment",
            local_cluster="edge-a",
        )

        self.assertEqual(subject, "frame.global.edge-b.detector.segment")

    def test_frame_subscription_subjects_include_both_routes(self):
        comm = NatsComm()

        subjects = comm.frame_subscription_subjects(
            agent_id="detector",
            local_cluster="edge-a",
        )

        self.assertEqual(
            subjects,
            (
                "frame.local.edge-a.detector.infer",
                "frame.global.edge-a.detector.infer",
            ),
        )

    def test_frame_subject_rejects_invalid_token(self):
        comm = NatsComm()

        with self.assertRaises(ValueError):
            comm.frame_subject(
                target_cluster="edge.a",
                agent_id="detector",
                local_cluster="edge-a",
            )


class NatsBinaryApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_bytes_uses_core_nats(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        response = Mock()
        response.data = b"result"
        comm._nc.request = AsyncMock(return_value=response)

        result = await comm.request_bytes(
            "frame.local.edge-a.detector.infer",
            bytearray(b"frame"),
            timeout_sec=12,
        )

        self.assertEqual(result, b"result")
        comm.connect.assert_awaited_once_with(ensure_stream=False)
        comm._nc.request.assert_awaited_once_with(
            "frame.local.edge-a.detector.infer",
            b"frame",
            timeout=12,
        )

    async def test_request_bytes_converts_nats_timeout(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        comm._nc.request = AsyncMock(side_effect=NatsTimeoutError())

        with self.assertRaises(TimeoutError):
            await comm.request_bytes(
                "frame.local.edge-a.detector.infer",
                b"frame",
                timeout_sec=1,
            )

    async def test_publish_bytes_flushes_connection(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        comm._nc.publish = AsyncMock()
        comm._nc.flush = AsyncMock()

        await comm.publish_bytes(
            "_INBOX.reply",
            memoryview(b"result"),
            timeout_sec=3,
        )

        comm.connect.assert_awaited_once_with(ensure_stream=False)
        comm._nc.publish.assert_awaited_once_with("_INBOX.reply", b"result")
        comm._nc.flush.assert_awaited_once_with(timeout=3)

    async def test_binary_payload_limit_is_enforced(self):
        comm = NatsComm(max_binary_payload_bytes=4)
        comm.connect = AsyncMock()
        comm._nc.request = AsyncMock()

        with self.assertRaises(ValueError):
            await comm.request_bytes("frame.local.a.b.infer", b"12345")

        comm._nc.request.assert_not_called()

    async def test_connect_uses_large_pending_buffer(self):
        comm = NatsComm()
        client = Mock()
        client.is_connected = False
        client.connect = AsyncMock()
        comm._nc = client

        await comm.connect(ensure_stream=False)

        self.assertEqual(
            client.connect.await_args.kwargs["pending_size"],
            128 * 1024 * 1024,
        )
        await comm.close()

    async def test_subscribe_bytes_is_cached_and_auto_replies(self):
        comm = NatsComm()
        comm.connect = AsyncMock()
        subscription = object()
        comm._nc.subscribe = AsyncMock(return_value=subscription)
        comm._nc.flush = AsyncMock()
        handler = AsyncMock(return_value=b"response")

        first = await comm.subscribe_bytes(
            "frame.local.edge-a.detector.infer",
            handler=handler,
            queue="detector",
        )
        second = await comm.subscribe_bytes(
            "frame.local.edge-a.detector.infer",
            handler=handler,
            queue="detector",
        )

        self.assertIs(first, subscription)
        self.assertIs(second, subscription)
        comm._nc.subscribe.assert_awaited_once()
        callback = comm._nc.subscribe.await_args.kwargs["cb"]
        raw_message = Mock()
        raw_message.subject = "frame.local.edge-a.detector.infer"
        raw_message.data = b"request"
        raw_message.reply = "_INBOX.reply"
        raw_message.headers = None
        raw_message.respond = AsyncMock()

        await callback(raw_message)

        binary_message = handler.await_args.args[0]
        self.assertEqual(binary_message.data, b"request")
        self.assertEqual(binary_message.reply_subject, "_INBOX.reply")
        raw_message.respond.assert_awaited_once_with(b"response")

    async def test_subscribe_frame_bytes_registers_local_and_global(self):
        comm = NatsComm()
        comm.subscribe_bytes = AsyncMock(side_effect=["local-sub", "global-sub"])
        handler = AsyncMock()

        subscriptions = await comm.subscribe_frame_bytes(
            agent_id="detector",
            handler=handler,
            local_cluster="edge-a",
            queue="detector",
        )

        self.assertEqual(subscriptions, ("local-sub", "global-sub"))
        self.assertEqual(comm.subscribe_bytes.await_count, 2)
        self.assertEqual(
            comm.subscribe_bytes.await_args_list[0].args[0],
            "frame.local.edge-a.detector.infer",
        )
        self.assertEqual(
            comm.subscribe_bytes.await_args_list[1].args[0],
            "frame.global.edge-a.detector.infer",
        )

    async def test_subscribe_frame_bytes_rejects_invalid_concurrency(self):
        comm = NatsComm()

        with self.assertRaises(ValueError):
            await comm.subscribe_frame_bytes(
                agent_id="detector",
                handler=AsyncMock(),
                local_cluster="edge-a",
                max_inflight=0,
            )

    async def test_request_frame_bytes_routes_before_request(self):
        comm = NatsComm()
        comm.request_bytes = AsyncMock(return_value=b"ok")

        result = await comm.request_frame_bytes(
            target_cluster="edge-b",
            agent_id="detector",
            payload=b"frame",
            local_cluster="edge-a",
            timeout_sec=20,
        )

        self.assertEqual(result, b"ok")
        comm.request_bytes.assert_awaited_once_with(
            "frame.global.edge-b.detector.infer",
            b"frame",
            timeout_sec=20,
        )


class NatsJetStreamBinaryApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def raw_message(data=b"payload"):
        raw = Mock()
        raw.subject = "state.detector"
        raw.data = data
        raw.headers = {"Content-Type": "application/octet-stream"}
        raw.metadata = SimpleNamespace(
            stream="STATE",
            consumer="reader",
            sequence=SimpleNamespace(stream=9, consumer=1),
        )
        raw.ack = AsyncMock()
        raw.nak = AsyncMock()
        raw.in_progress = AsyncMock()
        raw.term = AsyncMock()
        return raw

    async def test_send_bytes_publishes_raw_payload_to_jetstream(self):
        comm = NatsComm()
        js = AsyncMock()
        js.publish.return_value = SimpleNamespace(stream="STATE", seq=9)
        comm._jetstream_for_subject = AsyncMock(return_value=(js, "legacy"))

        result = await comm.send_bytes(
            "state.detector",
            memoryview(b"\x00\x01frame"),
            timeout_sec=7,
        )

        self.assertEqual(result, {
            "subject": "state.detector",
            "stream": "STATE",
            "seq": 9,
        })
        js.publish.assert_awaited_once_with(
            "state.detector",
            b"\x00\x01frame",
            timeout=7,
        )

    async def test_send_bytes_enforces_binary_limit(self):
        comm = NatsComm(max_binary_payload_bytes=4)
        comm._jetstream_for_subject = AsyncMock()

        with self.assertRaises(ValueError):
            await comm.send_bytes("state.detector", b"12345")

        comm._jetstream_for_subject.assert_not_awaited()

    async def test_receive_bytes_preserves_raw_data_and_ack_handle(self):
        comm = NatsComm()
        raw = self.raw_message(b"\x00binary")
        comm._fetch_raw_messages = AsyncMock(return_value=[raw])

        messages = await comm.receive_bytes(
            "state.detector",
            durable="reader",
            ack=True,
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].data, b"\x00binary")
        self.assertEqual(messages[0].stream_seq, 9)
        self.assertEqual(
            messages[0].headers["Content-Type"],
            "application/octet-stream",
        )
        self.assertTrue(comm._fetch_raw_messages.await_args.kwargs["auto_ack"])

    async def test_receive_latest_bytes_uses_new_deliver_last_consumer(self):
        comm = NatsComm()
        comm._jetstream_for_subject = AsyncMock()
        js = AsyncMock()
        comm._jetstream_for_subject.return_value = (js, "legacy")
        raw = self.raw_message(b"newest")
        first_sub = AsyncMock()
        first_sub.fetch.return_value = [raw]
        second_sub = AsyncMock()
        second_sub.fetch.return_value = [raw]
        events = []
        raw.ack.side_effect = lambda: events.append("ack")
        first_sub.unsubscribe.side_effect = lambda: events.append("unsubscribe-1")
        second_sub.unsubscribe.side_effect = lambda: events.append("unsubscribe-2")
        js.pull_subscribe.side_effect = [first_sub, second_sub]

        first = await comm.receive_latest_bytes("state.detector", ack=True)
        second = await comm.receive_latest_bytes("state.detector", ack=True)

        self.assertEqual(first.data, b"newest")
        self.assertEqual(second.data, b"newest")
        self.assertEqual(js.pull_subscribe.await_count, 2)
        for call in js.pull_subscribe.await_args_list:
            self.assertIsNone(call.kwargs["durable"])
            self.assertEqual(
                call.kwargs["config"].deliver_policy,
                DeliverPolicy.LAST,
            )
        first_sub.unsubscribe.assert_awaited_once()
        second_sub.unsubscribe.assert_awaited_once()
        self.assertEqual(raw.ack.await_count, 2)
        self.assertEqual(
            events,
            ["ack", "unsubscribe-1", "ack", "unsubscribe-2"],
        )

    async def test_receive_latest_decodes_json(self):
        comm = NatsComm()
        raw = self.raw_message(b'{"version":3}')
        comm._fetch_raw_messages = AsyncMock(return_value=[raw])

        message = await comm.receive_latest("state.detector", ack=True)

        self.assertEqual(message.payload, {"version": 3})
        self.assertEqual(message.stream_seq, 9)
        fetch = comm._fetch_raw_messages.await_args
        self.assertEqual(fetch.kwargs["deliver_policy"], DeliverPolicy.LAST)
        self.assertTrue(fetch.kwargs["auto_ack"])

    async def test_receive_latest_rejects_workqueue_with_clear_error(self):
        comm = NatsComm()
        js = AsyncMock()
        js.pull_subscribe.side_effect = BadRequestError(
            code=400,
            err_code=10101,
            description="consumer must be deliver all on workqueue stream",
        )

        with self.assertRaisesRegex(ValueError, "WorkQueue stream"):
            await comm._get_pull_subscription(
                "state.detector",
                durable=None,
                js=js,
                deliver_policy=DeliverPolicy.LAST,
            )


if __name__ == "__main__":
    unittest.main()
