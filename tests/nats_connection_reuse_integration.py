import asyncio
import os

from runtime_api import NatsComm


REQUEST_COUNT = int(os.environ.get("NATS_REUSE_TEST_REQUESTS", "200"))
SUBJECT = "workflow.reuse.integration.in"
DURABLE = "reuse-integration-worker"


async def wait_for_worker(worker: NatsComm) -> None:
    for _ in range(100):
        if worker._pull_subscriptions:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("worker pull subscription was not created")


async def main() -> None:
    worker = NatsComm()
    requester = NatsComm()

    async def handler(payload):
        await worker.publish_core(
            payload["reply_subject"],
            {
                "workflow_id": payload["workflow_id"],
                "result": "ok",
            },
        )

    serve_task = asyncio.create_task(
        worker.serve(
            subject=SUBJECT,
            durable=DURABLE,
            handler=handler,
            max_inflight=1,
            poll_timeout_sec=0.2,
        )
    )

    try:
        await wait_for_worker(worker)
        await requester.connect()
        requester_subscription_baseline = len(requester._nc._subs)
        worker_subscription_baseline = len(worker._nc._subs)

        for index in range(REQUEST_COUNT):
            reply = await requester.send_and_wait(
                subject=SUBJECT,
                payload={"workflow_id": f"reuse-{index}"},
                timeout_sec=5,
            )
            if reply.get("result") != "ok":
                raise AssertionError(f"unexpected reply at index={index}: {reply}")
            if len(requester._nc._subs) != requester_subscription_baseline:
                raise AssertionError(
                    "requester subscription count grew: "
                    f"baseline={requester_subscription_baseline}, "
                    f"current={len(requester._nc._subs)}, index={index}"
                )

        if len(worker._nc._subs) != worker_subscription_baseline:
            raise AssertionError(
                "worker subscription count grew: "
                f"baseline={worker_subscription_baseline}, "
                f"current={len(worker._nc._subs)}"
            )
        if len(worker._pull_subscriptions) != 1:
            raise AssertionError(
                f"expected one cached pull subscription, "
                f"got {len(worker._pull_subscriptions)}"
            )

        stream_info = await requester._js.stream_info(requester.stream)
        consumers = await requester._js.consumers_info(requester.stream)
        consumer_names = sorted(info.name for info in consumers)
        if consumer_names != [DURABLE]:
            raise AssertionError(f"unexpected consumers: {consumer_names}")
        if stream_info.state.messages != REQUEST_COUNT:
            raise AssertionError(
                "reply inbox messages were unexpectedly persisted: "
                f"expected={REQUEST_COUNT}, actual={stream_info.state.messages}"
            )

        print(
            "PASS "
            f"requests={REQUEST_COUNT} "
            f"requester_subscriptions={len(requester._nc._subs)} "
            f"worker_subscriptions={len(worker._nc._subs)} "
            f"consumers={consumer_names} "
            f"stream_messages={stream_info.state.messages}",
            flush=True,
        )
    finally:
        serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)
        await requester.close()
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
