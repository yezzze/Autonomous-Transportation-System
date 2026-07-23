import asyncio
import os
import uuid
from concurrent import futures

import grpc
from runtime_api import NatsComm
from runtime_api import frame_transport_pb2_grpc
from runtime_api.frame_comm import frame_reference_to_dict
from runtime_api.frame_service import FrameTransportService
from runtime_api.frame_store import (
    FrameNotFound,
    FrameStore,
)

import agent_pb2
import agent_pb2_grpc

REQ_SUBJECT = os.environ.get("REQ_SUBJECT", "workflow.demo.agent.b.in")
REPLY_SUBJECT_PREFIX = os.environ.get("REPLY_SUBJECT_PREFIX", "workflow.demo.agent.grpc.reply")
FRAME_PUBLIC_ADDR = os.environ.get("FRAME_PUBLIC_ADDR", "agent-grpc:50051")
FRAME_CHUNK_SIZE = int(os.environ.get("FRAME_CHUNK_SIZE", str(1024 * 1024)))
FRAME_MAX_INFLIGHT_UPLOADS = int(os.environ.get("FRAME_MAX_INFLIGHT_UPLOADS", "4"))
WORKFLOW_TIMEOUT_SEC = float(os.environ.get("WORKFLOW_TIMEOUT_SEC", "120"))
GRPC_WORKERS = int(os.environ.get("GRPC_WORKERS", "16"))

FRAME_STORE = FrameStore.from_env()

print("[Agent gRPC] server.py starting...", flush=True)


def log(msg: str) -> None:
    print(f"[Agent gRPC] {msg}", flush=True)


FRAME_SERVICE = FrameTransportService(
    store=FRAME_STORE,
    public_addr=FRAME_PUBLIC_ADDR,
    chunk_size=FRAME_CHUNK_SIZE,
    max_inflight_uploads=FRAME_MAX_INFLIGHT_UPLOADS,
    logger=log,
)


async def handle_infer(text: str, frame_ref=None) -> str:
    workflow_id = str(uuid.uuid4())
    reply_subject = f"{REPLY_SUBJECT_PREFIX}.{workflow_id}"
    comm = NatsComm()
    log(f"handle_infer called, workflow_id={workflow_id}, reply_subject={reply_subject}")

    try:
        payload = {
            "workflow_id": workflow_id,
            "text": text,
            "reply_subject": reply_subject,
        }
        if frame_ref:
            payload["frame_ref"] = frame_ref
        log(f"sending request to {REQ_SUBJECT}: {payload}")
        ack = await comm.send(REQ_SUBJECT, payload)
        log(f"request stored in stream={ack['stream']} seq={ack['seq']}")

        while True:
            replies = await comm.receive(
                subject=reply_subject,
                durable=None,
                batch=1,
                timeout_sec=WORKFLOW_TIMEOUT_SEC,
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
        frame_id = request.frame.frame_id if request.HasField("frame") else ""
        log(f"received gRPC request: text={request.text}, frame_id={frame_id or '-'}")
        try:
            frame_ref = None
            if frame_id:
                stored = FRAME_STORE.get(frame_id)
                canonical_ref = FRAME_SERVICE.reference_for(stored)
                frame_ref = frame_reference_to_dict(canonical_ref)
            result = asyncio.run(handle_infer(request.text, frame_ref=frame_ref))
            log(f"gRPC request completed, result={result}")
            return agent_pb2.InferResponse(result=result)
        except FrameNotFound as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return agent_pb2.InferResponse(result="")
        except Exception as e:
            log(f"gRPC Infer failed: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return agent_pb2.InferResponse(result="")
        finally:
            if frame_id and not request.retain_frame:
                FRAME_STORE.delete(frame_id)


def serve():
    log("creating gRPC server")
    log(f"runtime_api=NatsComm, NATS_SERVERS={os.environ.get('NATS_SERVERS', 'nats://nats:4222')}")
    log(f"REQ_SUBJECT={REQ_SUBJECT}, REPLY_SUBJECT_PREFIX={REPLY_SUBJECT_PREFIX}")
    log(
        f"FRAME_PUBLIC_ADDR={FRAME_PUBLIC_ADDR}, FRAME_CHUNK_SIZE={FRAME_CHUNK_SIZE}, "
        f"FRAME_MAX_BYTES={FRAME_STORE.max_frame_bytes}"
    )
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=GRPC_WORKERS),
        options=[
            ("grpc.max_receive_message_length", FRAME_CHUNK_SIZE + 64 * 1024),
            ("grpc.max_send_message_length", FRAME_CHUNK_SIZE + 64 * 1024),
        ],
    )
    agent_pb2_grpc.add_AgentServiceServicer_to_server(AgentService(), server)
    frame_transport_pb2_grpc.add_FrameTransportServicer_to_server(
        FRAME_SERVICE,
        server,
    )

    listen_addr = "[::]:50051"
    server.add_insecure_port(listen_addr)
    log(f"binding gRPC server on {listen_addr}")

    server.start()
    log("gRPC server started and waiting for requests")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
