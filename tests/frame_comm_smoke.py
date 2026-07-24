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
    comm = FrameComm(
        upload_target=args.frame_target,
        allowed_targets=[args.frame_target],
    )

    try:
        reply = await comm.send_workflow_and_wait(
            target_cluster=args.target_cluster,
            agent_id=args.target_agent,
            target_instance_id=args.target_instance,
            local_cluster=args.local_cluster,
            payload={
                "workflow_id": workflow_id,
                "text": "frame comm smoke",
            },
            frame_bytes=frame_bytes,
            timeout_sec=args.timeout_sec,
        )
        result = reply.get("result", "")
        if str(expected_size) not in result or expected_sha256 not in result:
            raise AssertionError(f"invalid Agent C result: {result}")
        print(f"FrameComm smoke passed: {result}")
    finally:
        await comm.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frame-target", default="agent-grpc:50051")
    parser.add_argument("--local-cluster", default="demo")
    parser.add_argument("--target-cluster", default="demo")
    parser.add_argument("--target-agent", default="c")
    parser.add_argument("--target-instance", required=True)
    parser.add_argument("--size-mib", type=int, default=12)
    parser.add_argument("--timeout-sec", type=float, default=60)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
