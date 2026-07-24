import asyncio
import logging
import os
import time

from runtime_api import NatsComm

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

AGENT_ID = os.environ.get("AGENT_ID", "b")
AGENT_INSTANCE_ID = os.environ.get("AGENT_INSTANCE_ID", "")
CLUSTER_ID = os.environ.get("CLUSTER_ID", "demo")
TARGET_C_CLUSTER_ID = os.environ.get("TARGET_C_CLUSTER_ID", CLUSTER_ID)
TARGET_C_AGENT_ID = os.environ.get("TARGET_C_AGENT_ID", "c")
TARGET_C_INSTANCE_ID = os.environ.get("TARGET_C_INSTANCE_ID", "")
DURABLE = os.environ.get("DURABLE", "agent-b-consumer")
MAX_INFLIGHT = int(os.environ.get("NATS_MAX_INFLIGHT", "4"))
WORKFLOW_TIMEOUT_SEC = float(os.environ.get("WORKFLOW_TIMEOUT_SEC", "120"))
ACK_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("NATS_ACK_PROGRESS_INTERVAL_SEC", "10")
)


def log(msg: str) -> None:
    print(f"[Agent B] {msg}", flush=True)


async def main():
    log("worker.py starting with runtime_api.NatsComm")
    if not AGENT_INSTANCE_ID:
        raise ValueError("AGENT_INSTANCE_ID is required")
    if not TARGET_C_INSTANCE_ID:
        raise ValueError("TARGET_C_INSTANCE_ID is required")
    comm = NatsComm()

    async def handler(data):
        workflow_id = data.get("workflow_id")
        text = data.get("text", "")
        final_reply_subject = data.get("reply_subject")
        if not workflow_id:
            raise ValueError("missing workflow_id")
        if not final_reply_subject:
            raise ValueError("missing reply_subject")

        c_payload = {
            "workflow_id": workflow_id,
            "text": text,
        }
        if data.get("frame_ref"):
            c_payload["frame_ref"] = data["frame_ref"]

        started = time.monotonic()
        log(
            f"forwarding workflow_id={workflow_id} to "
            f"{TARGET_C_CLUSTER_ID}/{TARGET_C_AGENT_ID}/{TARGET_C_INSTANCE_ID}"
        )
        c_reply_payload = await comm.send_workflow_and_wait(
            target_cluster=TARGET_C_CLUSTER_ID,
            agent_id=TARGET_C_AGENT_ID,
            target_instance_id=TARGET_C_INSTANCE_ID,
            payload=c_payload,
            local_cluster=CLUSTER_ID,
            timeout_sec=WORKFLOW_TIMEOUT_SEC,
        )
        c_result = c_reply_payload.get("result", "")
        log(
            f"received Agent C reply workflow_id={workflow_id}, "
            f"elapsed_ms={(time.monotonic() - started) * 1000:.1f}"
        )

        result = f"Agent B processed with Agent C: {c_result}"
        reply = {
            "workflow_id": workflow_id,
            "result": result,
        }
        log(f"publishing final reply to {final_reply_subject}")
        await comm.publish_core(final_reply_subject, reply)

    try:
        log(
            f"subscribing as {CLUSTER_ID}/{AGENT_ID}/{AGENT_INSTANCE_ID}"
        )
        await comm.serve_workflow(
            agent_id=AGENT_ID,
            instance_id=AGENT_INSTANCE_ID,
            local_cluster=CLUSTER_ID,
            durable=DURABLE,
            handler=handler,
            max_inflight=MAX_INFLIGHT,
            ack_progress_interval_sec=ACK_PROGRESS_INTERVAL_SEC,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
