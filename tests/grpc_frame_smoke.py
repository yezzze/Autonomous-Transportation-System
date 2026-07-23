import argparse
import hashlib
import uuid

import grpc

import agent_pb2
import agent_pb2_grpc
from runtime_api import frame_transport_pb2, frame_transport_pb2_grpc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="localhost:50051")
    parser.add_argument("--size-mib", type=int, default=12)
    parser.add_argument("--infer", action="store_true")
    args = parser.parse_args()

    chunk_size = 1024 * 1024
    chunk_count = args.size_mib
    data = b"x" * chunk_size
    digest = hashlib.sha256()
    for _ in range(chunk_count):
        digest.update(data)
    expected_sha256 = digest.hexdigest()
    expected_size = chunk_count * chunk_size
    frame_id = f"smoke-{uuid.uuid4().hex}"

    def chunks():
        for chunk_index in range(chunk_count):
            yield frame_transport_pb2.FrameChunk(
                frame_id=frame_id,
                chunk_index=chunk_index,
                data=data,
                content_type="application/octet-stream" if chunk_index == 0 else "",
                total_size=expected_size if chunk_index == 0 else 0,
                sha256=expected_sha256 if chunk_index == 0 else "",
            )

    options = [
        ("grpc.max_send_message_length", chunk_size + 64 * 1024),
        ("grpc.max_receive_message_length", chunk_size + 64 * 1024),
    ]
    with grpc.insecure_channel(args.target, options=options) as channel:
        stub = agent_pb2_grpc.AgentServiceStub(channel)
        frame_stub = frame_transport_pb2_grpc.FrameTransportStub(channel)
        frame_ref = frame_stub.UploadFrame(
            chunks(),
            timeout=30,
            wait_for_ready=True,
        )

        if args.infer:
            response = stub.Infer(
                agent_pb2.InferRequest(text="frame smoke", frame=frame_ref),
                timeout=180,
                wait_for_ready=True,
            )
            if str(expected_size) not in response.result:
                raise AssertionError(f"inference result did not confirm frame: {response.result}")
            if expected_sha256 not in response.result:
                raise AssertionError(f"inference result did not confirm sha256: {response.result}")
            print(f"grpc+nats frame e2e passed: {response.result}")
            return

        received_size = 0
        received_digest = hashlib.sha256()
        for expected_index, chunk in enumerate(
            frame_stub.DownloadFrame(frame_ref, timeout=30, wait_for_ready=True)
        ):
            if chunk.chunk_index != expected_index:
                raise AssertionError("download chunk order mismatch")
            received_size += len(chunk.data)
            received_digest.update(chunk.data)

        if received_size != expected_size:
            raise AssertionError(
                f"size mismatch: expected={expected_size}, actual={received_size}"
            )
        if received_digest.hexdigest() != expected_sha256:
            raise AssertionError("sha256 mismatch")
        if not frame_stub.DeleteFrame(frame_ref, timeout=5).deleted:
            raise AssertionError("frame was not deleted")

    print(
        f"grpc frame smoke passed: bytes={received_size}, "
        f"chunks={chunk_count}, sha256={expected_sha256}"
    )


if __name__ == "__main__":
    main()
