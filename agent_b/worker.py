import asyncio
import os
import re

from runtime_api import NatsComm

IN_SUBJECT = os.environ.get("IN_SUBJECT", "workflow.demo.agent.b.in")
OUT_SUBJECT = os.environ.get("OUT_SUBJECT", "workflow.demo.agent.a.reply")
C_IN_SUBJECT = os.environ.get("C_IN_SUBJECT", "workflow.demo.agent.c.in")
B_REPLY_PREFIX = os.environ.get("B_REPLY_PREFIX", "workflow.demo.agent.b.c.reply")
DURABLE = os.environ.get("DURABLE", "agent-b-consumer")


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

        log(f"forwarding workflow_id={workflow_id} to Agent C: {c_payload}")
        await comm.send(C_IN_SUBJECT, c_payload)

        replies = await comm.receive(
            subject=reply_subject,
            durable=None,
            batch=1,
            timeout_sec=30,
        )
        if not replies:
            raise TimeoutError(f"timeout waiting for Agent C reply: workflow_id={workflow_id}")

        c_reply = replies[0]
        await c_reply.ack()
        c_result = c_reply.payload.get("result", "")
        log(f"received Agent C reply workflow_id={workflow_id}: {c_reply.payload}")

        result = f"Agent B processed with Agent C: {c_result}"
        reply = {
            "workflow_id": workflow_id,
            "result": result,
        }
        log(f"publishing final reply to Agent A: {reply}")
        await comm.send(OUT_SUBJECT, reply)

    try:
        log(f"subscribing to {IN_SUBJECT}, forwarding to {C_IN_SUBJECT}")
        await comm.serve(
            subject=IN_SUBJECT,
            durable=DURABLE,
            handler=handler,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
