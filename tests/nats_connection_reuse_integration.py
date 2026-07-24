import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_api import NatsComm


REQUEST_COUNT = int(os.environ.get("NATS_REUSE_TEST_REQUESTS", "200"))
CLUSTER_ID = os.environ.get("CLUSTER_ID", "hub")
AGENT_ID = "reuse"
INSTANCE_ID = "reuse-integration-pod"
DURABLE = "reuse-integration-worker"


async def wait_for_worker(worker: NatsComm) -> None:
    for _ in range(100):
        if len(worker._pull_subscriptions) == 2:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("worker pull subscription was not created")


async def wait_for_stream_empty(js, stream: str):
    for _ in range(100):
        info = await js.stream_info(stream)
        if info.state.messages == 0:
            return info
        await asyncio.sleep(0.01)
    raise TimeoutError(f"stream did not drain: {stream}")


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

    await requester.provision_workflow_stream(
        target_cluster=CLUSTER_ID,
        agent_id=AGENT_ID,
        instance_id=INSTANCE_ID,
    )
    serve_task = asyncio.create_task(
        worker.serve_workflow(
            agent_id=AGENT_ID,
            instance_id=INSTANCE_ID,
            local_cluster=CLUSTER_ID,
            durable=DURABLE,
            handler=handler,
            max_inflight=1,
            poll_timeout_sec=0.2,
        )
    )

    try:
        await wait_for_worker(worker)
        requester_subscription_baseline = len(requester._nc._subs)
        worker_subscription_baseline = len(worker._nc._subs)

        for index in range(REQUEST_COUNT):
            reply = await requester.send_workflow_and_wait(
                target_cluster=CLUSTER_ID,
                agent_id=AGENT_ID,
                target_instance_id=INSTANCE_ID,
                local_cluster=CLUSTER_ID,
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
        if len(worker._pull_subscriptions) != 2:
            raise AssertionError(
                f"expected two cached pull subscriptions, "
                f"got {len(worker._pull_subscriptions)}"
            )

        js = requester._nc.jetstream(domain=CLUSTER_ID)
        stream = requester.workflow_stream_name(INSTANCE_ID)
        stream_info = await wait_for_stream_empty(js, stream)
        consumers = await js.consumers_info(stream)
        consumer_names = sorted(info.name for info in consumers)
        expected_consumers = [
            f"{DURABLE}-global",
            f"{DURABLE}-local",
        ]
        if consumer_names != expected_consumers:
            raise AssertionError(f"unexpected consumers: {consumer_names}")
        if stream_info.state.messages != 0:
            raise AssertionError(
                "ACKed work-queue messages were not removed: "
                f"actual={stream_info.state.messages}"
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
        await requester.delete_workflow_stream(
            target_cluster=CLUSTER_ID,
            instance_id=INSTANCE_ID,
        )
        await requester.close()
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
