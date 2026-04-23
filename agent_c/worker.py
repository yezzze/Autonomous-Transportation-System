import asyncio
import os

from runtime_api import NatsComm

IN_SUBJECT = os.environ.get("IN_SUBJECT", "workflow.demo.agent.c.in")
DURABLE = os.environ.get("DURABLE", "agent-c-consumer")


def log(msg: str) -> None:
    print(f"[Agent C] {msg}", flush=True)


async def main():
    log("worker.py starting with runtime_api.NatsComm")
    comm = NatsComm()

    async def handler(data):
        workflow_id = data.get("workflow_id")
        text = data.get("text", "")
        reply_subject = data.get("reply_subject")
        if not workflow_id:
            raise ValueError("missing workflow_id")
        if not reply_subject:
            raise ValueError("missing reply_subject")

        result = f"Agent C transformed: {text.upper()}"
        reply = {
            "workflow_id": workflow_id,
            "result": result,
        }
        log(f"publishing reply to {reply_subject}: {reply}")
        await comm.send(reply_subject, reply)

    try:
        log(f"subscribing to {IN_SUBJECT}")
        await comm.serve(
            subject=IN_SUBJECT,
            durable=DURABLE,
            handler=handler,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
