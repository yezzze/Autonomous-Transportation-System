import argparse
import asyncio
import hashlib
import uuid

from runtime_api import FrameComm


async def run(args):
    frame_bytes = b"x" * (args.size_mib * 1024 * 1024)
    expected_sha256 = hashlib.sha256(frame_bytes).hexdigest()
    expected_size = len(frame_bytes)
    workflow_id = str(uuid.uuid4())
    reply_subject = f"workflow.demo.frame.comm.reply.{workflow_id}"
    comm = FrameComm(
        upload_target=args.frame_target,
        allowed_targets=[args.frame_target],
    )

    try:
        ack = await comm.send(
            subject=args.subject,
            payload={
                "workflow_id": workflow_id,
                "text": "frame comm smoke",
                "reply_subject": reply_subject,
            },
            frame_bytes=frame_bytes,
        )
        if not ack.get("frame_ref"):
            raise AssertionError("FrameComm.send did not return frame_ref")

        replies = await comm.receive(
            subject=reply_subject,
            durable=None,
            batch=1,
            timeout_sec=args.timeout_sec,
        )
        if not replies:
            raise AssertionError("timeout waiting for Agent C reply")
        reply = replies[0]
        await reply.ack()
        result = reply.payload.get("result", "")
        if str(expected_size) not in result or expected_sha256 not in result:
            raise AssertionError(f"invalid Agent C result: {result}")
        print(f"FrameComm smoke passed: {result}")
    finally:
        await comm.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frame-target", default="agent-grpc:50051")
    parser.add_argument("--subject", default="workflow.demo.agent.c.in")
    parser.add_argument("--size-mib", type=int, default=12)
    parser.add_argument("--timeout-sec", type=float, default=60)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
