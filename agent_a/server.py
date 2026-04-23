import asyncio
import json
import os
import uuid
from concurrent import futures

import grpc
from nats.aio.client import Client as NATS

import agent_pb2
import agent_pb2_grpc

NATS_SERVERS = [s.strip() for s in os.environ.get("NATS_SERVERS", "nats://nats:4222").split(",") if s.strip()]
REQ_SUBJECT = os.environ.get("REQ_SUBJECT", "workflow.demo.agent.b.in")
REPLY_SUBJECT = os.environ.get("REPLY_SUBJECT", "workflow.demo.agent.a.reply")

print("[Agent A] server.py starting...", flush=True)


def log(msg: str) -> None:
    print(f"[Agent A] {msg}", flush=True)


async def handle_infer(text: str) -> str:
    workflow_id = str(uuid.uuid4())
    log(f"handle_infer called, workflow_id={workflow_id}, text={text}")

    nc = NATS()

    try:
        log(f"connecting to NATS: {NATS_SERVERS}")
        await nc.connect(
            servers=NATS_SERVERS,
            connect_timeout=5,
            reconnect_time_wait=2,
            max_reconnect_attempts=10,
        )
        log("connected to NATS")

        js = nc.jetstream()

        try:
            await js.add_stream(name="WORKFLOW", subjects=["workflow.demo.agent.>"])
            log("JetStream stream WORKFLOW created")
        except Exception as e:
            log(f"JetStream stream WORKFLOW already exists or create skipped: {e}")

        sub = await js.subscribe(
            REPLY_SUBJECT,
            durable="agent-a-reply",
            manual_ack=True,
        )
        log(f"subscribed to reply subject: {REPLY_SUBJECT}")

        payload = {
            "workflow_id": workflow_id,
            "text": text,
        }

        log(f"publishing request to {REQ_SUBJECT}: {payload}")
        await js.publish(REQ_SUBJECT, json.dumps(payload).encode())

        while True:
            msg = await sub.next_msg(timeout=30)
            data = json.loads(msg.data.decode())
            await msg.ack()
            log(f"received reply message: {data}")

            if data.get("workflow_id") == workflow_id:
                result = data.get("result", "")
                log(f"matched workflow_id={workflow_id}, returning result={result}")
                await nc.drain()
                return result

    except Exception as e:
        log(f"handle_infer error: {e}")
        try:
            await nc.drain()
        except Exception:
            pass
        raise


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
