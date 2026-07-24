import asyncio
import logging
import os

from runtime_api import FrameComm

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

IN_SUBJECT = os.environ.get("IN_SUBJECT", "workflow.demo.agent.c.in")
DURABLE = os.environ.get("DURABLE", "agent-c-consumer")
MAX_INFLIGHT = int(os.environ.get("NATS_MAX_INFLIGHT", "4"))
ACK_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("NATS_ACK_PROGRESS_INTERVAL_SEC", "10")
)
DELETE_REMOTE_FRAME = (
    os.environ.get("FRAME_DELETE_AFTER_PROCESS", "true").strip().lower()
    not in {"0", "false", "no"}
)


def log(msg: str) -> None:
    print(f"[Agent C] {msg}", flush=True)


async def main():
    log("worker.py starting with runtime_api.FrameComm")
    comm = FrameComm()

    async def handler(data):
        workflow_id = data.get("workflow_id")
        text = data.get("text", "")
        reply_subject = data.get("reply_subject")
        if not workflow_id:
            raise ValueError("missing workflow_id")
        if not reply_subject:
            raise ValueError("missing reply_subject")

        frame_ref = data.get("frame_ref")
        if frame_ref:
            frame_path = data["frame_path"]
            frame_size = data["frame_size_bytes"]
            frame_sha256 = data["frame_sha256"]
            log(
                f"downloaded frame_id={frame_ref.get('frame_id')}, "
                f"bytes={frame_size}, sha256={frame_sha256}"
            )
            # Replace this demo result with model inference against frame_path.
            result = (
                f"Agent C transformed: {text.upper()} "
                f"(frame_bytes={frame_size}, sha256={frame_sha256})"
            )
        else:
            result = f"Agent C transformed: {text.upper()}"

        reply = {
            "workflow_id": workflow_id,
            "result": result,
        }
        log(f"publishing reply to {reply_subject}")
        await comm.publish_core(reply_subject, reply)

    try:
        log(f"subscribing to {IN_SUBJECT}")
        await comm.serve(
            subject=IN_SUBJECT,
            durable=DURABLE,
            handler=handler,
            max_inflight=MAX_INFLIGHT,
            ack_progress_interval_sec=ACK_PROGRESS_INTERVAL_SEC,
            download_frames=True,
            delete_remote_frame=DELETE_REMOTE_FRAME,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
