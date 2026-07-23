import argparse
import os
import sys
from pathlib import Path

sys.path.append("./agent_gRPC")

import grpc
import agent_pb2 as pb2
import agent_pb2_grpc as pb2_grpc
from runtime_api.frame_comm import (
    FrameTransportClient,
    frame_reference_from_dict,
)


CHUNK_SIZE = int(os.environ.get("FRAME_CHUNK_SIZE", str(1024 * 1024)))
INFER_TIMEOUT_SEC = float(os.environ.get("INFER_TIMEOUT_SEC", "180"))


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="hello from python client")
    parser.add_argument("--frame", type=Path)
    parser.add_argument("--retain-frame", action="store_true")
    args = parser.parse_args()

    target = os.environ.get("AGENT_GRPC_ADDR") or os.environ.get("AGENT_A_ADDR", "localhost:50051")
    options = [
        ("grpc.max_send_message_length", CHUNK_SIZE + 64 * 1024),
        ("grpc.max_receive_message_length", CHUNK_SIZE + 64 * 1024),
    ]
    frame_transport = FrameTransportClient(
        upload_target=target,
        allowed_targets=[target],
    )
    channel = None
    try:
        channel = grpc.insecure_channel(target, options=options)
        stub = pb2_grpc.AgentServiceStub(channel)
        request = pb2.InferRequest(text=args.text, retain_frame=args.retain_frame)

        if args.frame:
            frame_ref = frame_transport.upload_file(args.frame.resolve())
            request.frame.CopyFrom(frame_reference_from_dict(frame_ref))
            print(
                f"上传帧: id={frame_ref['frame_id']}, "
                f"bytes={frame_ref['size_bytes']}, sha256={frame_ref['sha256']}"
            )

        response = stub.Infer(
            request,
            timeout=INFER_TIMEOUT_SEC,
            wait_for_ready=True,
        )
        print("调用地址:", target)
        print("返回结果:", response.result)
    finally:
        frame_transport.close()
        if channel:
            channel.close()


if __name__ == "__main__":
    run()
