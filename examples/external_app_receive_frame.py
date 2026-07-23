import asyncio
import os
from pathlib import Path

from runtime_api import FrameComm


async def main():
    comm = FrameComm()

    async def handler(data):
        frame_path = Path(data["frame_path"])
        print(
            f"received workflow_id={data.get('workflow_id')} "
            f"path={frame_path} bytes={data['frame_size_bytes']} "
            f"sha256={data['frame_sha256']}"
        )
        # The temporary file remains valid until this handler returns.

    try:
        await comm.serve(
            subject=os.environ.get("IN_SUBJECT", "workflow.demo.frame.in"),
            durable=os.environ.get("DURABLE", "external-frame-consumer"),
            handler=handler,
            max_inflight=int(os.environ.get("NATS_MAX_INFLIGHT", "4")),
            download_frames=True,
            delete_remote_frame=True,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
