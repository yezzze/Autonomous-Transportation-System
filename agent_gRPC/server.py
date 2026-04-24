import asyncio
import os
import uuid
from concurrent import futures

import grpc
from runtime_api import NatsComm

import agent_pb2
import agent_pb2_grpc

REQ_SUBJECT = os.environ.get("REQ_SUBJECT", "workflow.demo.agent.b.in")
REPLY_SUBJECT_PREFIX = os.environ.get("REPLY_SUBJECT_PREFIX", "workflow.demo.agent.grpc.reply")

print("[Agent gRPC] server.py starting...", flush=True)


def log(msg: str) -> None:
    print(f"[Agent gRPC] {msg}", flush=True)


async def handle_infer(text: str) -> str:
    workflow_id = str(uuid.uuid4())
    reply_subject = f"{REPLY_SUBJECT_PREFIX}.{workflow_id}"
    comm = NatsComm()
    log(f"handle_infer called, workflow_id={workflow_id}, text={text}, reply_subject={reply_subject}")

    try:
        payload = {
            "workflow_id": workflow_id,
            "text": text,
            "reply_subject": reply_subject,
        }
        log(f"sending request to {REQ_SUBJECT}: {payload}")
        ack = await comm.send(REQ_SUBJECT, payload)
        log(f"request stored in stream={ack['stream']} seq={ack['seq']}")

        while True:
            replies = await comm.receive(
                subject=reply_subject,
                durable=None,
                batch=1,
                timeout_sec=30,
                ack=False,
            )
            if not replies:
                raise TimeoutError(f"timeout waiting for reply on subject={reply_subject}")

            reply = replies[0]
            await reply.ack()
            log(f"received reply payload: {reply.payload}")
            result = reply.payload.get("result", "")
            log(f"matched workflow_id={workflow_id}, returning result={result}")
            return result
    except Exception as e:
        log(f"handle_infer error: {e}")
        raise
    finally:
        await comm.close()


class AgentService(agent_pb2_grpc.AgentServiceServicer):
    def Infer(self, request, context):
        log(f"received gRPC request: text={request.text}")
        try:
            result = asyncio.run(handle_infer(request.text))
            log(f"gRPC request completed, result={result}")
            return agent_pb2.InferResponse(result=result)
        except Exception as e:
            log(f"gRPC Infer failed: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return agent_pb2.InferResponse(result="")


def serve():
    log("creating gRPC server")
    log(f"runtime_api=NatsComm, NATS_SERVERS={os.environ.get('NATS_SERVERS', 'nats://nats:4222')}")
    log(f"REQ_SUBJECT={REQ_SUBJECT}, REPLY_SUBJECT_PREFIX={REPLY_SUBJECT_PREFIX}")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    agent_pb2_grpc.add_AgentServiceServicer_to_server(AgentService(), server)

    listen_addr = "[::]:50051"
    server.add_insecure_port(listen_addr)
    log(f"binding gRPC server on {listen_addr}")

    server.start()
    log("gRPC server started and waiting for requests")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
