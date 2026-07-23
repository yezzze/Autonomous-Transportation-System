import asyncio
import os
import re

from runtime_api import NatsComm

IN_SUBJECT = os.environ.get("IN_SUBJECT", "workflow.demo.agent.b.in")
OUT_SUBJECT = os.environ.get("OUT_SUBJECT", "workflow.demo.agent.grpc.reply.default")
C_IN_SUBJECT = os.environ.get("C_IN_SUBJECT", "workflow.demo.agent.c.in")
B_REPLY_PREFIX = os.environ.get("B_REPLY_PREFIX", "workflow.demo.agent.b.c.reply")
DURABLE = os.environ.get("DURABLE", "agent-b-consumer")
MAX_INFLIGHT = int(os.environ.get("NATS_MAX_INFLIGHT", "4"))
WORKFLOW_TIMEOUT_SEC = float(os.environ.get("WORKFLOW_TIMEOUT_SEC", "120"))
ACK_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("NATS_ACK_PROGRESS_INTERVAL_SEC", "10")
)


def log(msg: str) -> None:
    print(f"[Agent B] {msg}", flush=True)


def _safe_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", value)
    return token.strip("-") or "unknown"


async def main():
    log("worker.py starting with runtime_api.NatsComm")
    comm = NatsComm()

    async def handler(data):
        workflow_id = data.get("workflow_id")
        text = data.get("text", "")
        if not workflow_id:
            raise ValueError("missing workflow_id")

        reply_subject = f"{B_REPLY_PREFIX}.{_safe_token(workflow_id)}"
        c_payload = {
            "workflow_id": workflow_id,
            "text": text,
            "reply_subject": reply_subject,
        }
        if data.get("frame_ref"):
            c_payload["frame_ref"] = data["frame_ref"]

        log(f"forwarding workflow_id={workflow_id} to Agent C: {c_payload}")
        await comm.send(C_IN_SUBJECT, c_payload)

        replies = await comm.receive(
            subject=reply_subject,
            durable=None,
            batch=1,
            timeout_sec=WORKFLOW_TIMEOUT_SEC,
        )
        if not replies:
            raise TimeoutError(f"timeout waiting for Agent C reply: workflow_id={workflow_id}")

        c_reply = replies[0]
        await c_reply.ack()
        c_result = c_reply.payload.get("result", "")
        log(f"received Agent C reply workflow_id={workflow_id}: {c_reply.payload}")

        result = f"Agent B processed with Agent C: {c_result}"
        final_reply_subject = data.get("reply_subject") or OUT_SUBJECT
        reply = {
            "workflow_id": workflow_id,
            "result": result,
        }
        log(f"publishing final reply to {final_reply_subject}: {reply}")
        await comm.send(final_reply_subject, reply)

    try:
        log(f"subscribing to {IN_SUBJECT}, forwarding to {C_IN_SUBJECT}")
        await comm.serve(
            subject=IN_SUBJECT,
            durable=DURABLE,
            handler=handler,
            max_inflight=MAX_INFLIGHT,
            ack_progress_interval_sec=ACK_PROGRESS_INTERVAL_SEC,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
