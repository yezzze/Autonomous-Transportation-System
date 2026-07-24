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
    args = parser.parse_args()

    comm = FrameComm()
    try:
        result = await comm.send_and_wait(
            subject=args.subject,
            payload={
                "workflow_id": str(uuid.uuid4()),
                "text": "frame from external app",
            },
            frame_path=args.frame,
            content_type="application/octet-stream",
            timeout_sec=120,
        )
        print(result)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
