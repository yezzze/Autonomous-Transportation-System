import sys
import os
sys.path.append("./agent_gRPC")

import grpc
import agent_pb2 as pb2
import agent_pb2_grpc as pb2_grpc


def run():
    target = os.environ.get("AGENT_GRPC_ADDR") or os.environ.get("AGENT_A_ADDR", "localhost:50051")
    channel = grpc.insecure_channel(target)
    stub = pb2_grpc.AgentServiceStub(channel)

    request = pb2.InferRequest(text="hello from python client")
    response = stub.Infer(request)

    print("调用地址:", target)
    print("返回结果:", response.result)


if __name__ == "__main__":
    run()
