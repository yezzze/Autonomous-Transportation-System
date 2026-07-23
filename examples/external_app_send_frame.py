import argparse
import asyncio
import uuid
from pathlib import Path

from runtime_api import FrameComm


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frame", type=Path)
    parser.add_argument(
        "--subject",
        default="workflow.demo.agent.c.in",
    )
    parser.add_argument("--reply-subject", default="workflow.demo.frame.reply")
    args = parser.parse_args()

    comm = FrameComm()
    try:
        result = await comm.send(
            subject=args.subject,
            payload={
                "workflow_id": str(uuid.uuid4()),
                "text": "frame from external app",
                "reply_subject": args.reply_subject,
            },
            frame_path=args.frame,
            content_type="application/octet-stream",
        )
        print(result)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
