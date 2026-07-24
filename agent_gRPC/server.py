import asyncio
import logging
import os
import threading
import time
import uuid
from concurrent import futures
from concurrent.futures import TimeoutError as FutureTimeoutError

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

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

CLUSTER_ID = os.environ.get("CLUSTER_ID", "demo")
TARGET_B_CLUSTER_ID = os.environ.get("TARGET_B_CLUSTER_ID", CLUSTER_ID)
TARGET_B_AGENT_ID = os.environ.get("TARGET_B_AGENT_ID", "b")
TARGET_B_INSTANCE_ID = os.environ.get("TARGET_B_INSTANCE_ID", "")
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


async def handle_infer(comm: NatsComm, text: str, frame_ref=None) -> str:
    workflow_id = str(uuid.uuid4())
    log(f"handle_infer called, workflow_id={workflow_id}")

    try:
        payload = {
            "workflow_id": workflow_id,
            "text": text,
        }
        if frame_ref:
            payload["frame_ref"] = frame_ref
        started = time.monotonic()
        log(
            f"sending request to "
            f"{TARGET_B_CLUSTER_ID}/{TARGET_B_AGENT_ID}/{TARGET_B_INSTANCE_ID}, "
            f"workflow_id={workflow_id}"
        )
        reply = await comm.send_workflow_and_wait(
            target_cluster=TARGET_B_CLUSTER_ID,
            agent_id=TARGET_B_AGENT_ID,
            target_instance_id=TARGET_B_INSTANCE_ID,
            payload=payload,
            local_cluster=CLUSTER_ID,
            timeout_sec=WORKFLOW_TIMEOUT_SEC,
        )
        result = reply.get("result", "")
        log(
            f"matched workflow_id={workflow_id}, "
            f"elapsed_ms={(time.monotonic() - started) * 1000:.1f}"
        )
        return result
    except Exception as e:
        log(f"handle_infer error: {e}")
        raise


class NatsRuntime:
    """Own one asyncio loop and one NATS connection for the gRPC process."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.comm = None
        self._startup_error = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="agent-grpc-nats",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise RuntimeError("failed to start NATS runtime") from self._startup_error

    def _run(self) -> None:
        try:
            asyncio.set_event_loop(self.loop)
            self.comm = NatsComm()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        self.loop.run_forever()

    def infer(self, text: str, frame_ref=None, timeout_sec: float = 150) -> str:
        future = asyncio.run_coroutine_threadsafe(
            handle_infer(self.comm, text, frame_ref=frame_ref),
            self.loop,
        )
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("gRPC worker timed out waiting for NATS workflow") from exc

    def close(self) -> None:
        if self.comm is not None and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.comm.close(), self.loop)
            try:
                future.result(timeout=10)
            except Exception as exc:
                log(f"failed to close NATS runtime cleanly: {exc}")
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            log("NATS runtime thread did not stop within 10 seconds")
        else:
            self.loop.close()


class AgentService(agent_pb2_grpc.AgentServiceServicer):
    def __init__(self, nats_runtime: NatsRuntime) -> None:
        self.nats_runtime = nats_runtime

    def Infer(self, request, context):
        frame_id = request.frame.frame_id if request.HasField("frame") else ""
        log(f"received gRPC request: text={request.text}, frame_id={frame_id or '-'}")
        try:
            frame_ref = None
            if frame_id:
                stored = FRAME_STORE.get(frame_id)
                canonical_ref = FRAME_SERVICE.reference_for(stored)
                frame_ref = frame_reference_to_dict(canonical_ref)
            wait_timeout = WORKFLOW_TIMEOUT_SEC + 30
            remaining = context.time_remaining()
            if remaining is not None:
                wait_timeout = min(wait_timeout, max(0.1, remaining))
            result = self.nats_runtime.infer(
                request.text,
                frame_ref=frame_ref,
                timeout_sec=wait_timeout,
            )
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
    if not TARGET_B_INSTANCE_ID:
        raise ValueError("TARGET_B_INSTANCE_ID is required")
    log("creating gRPC server")
    log(f"runtime_api=NatsComm, NATS_SERVERS={os.environ.get('NATS_SERVERS', 'nats://nats:4222')}")
    log(
        f"TARGET_B={TARGET_B_CLUSTER_ID}/"
        f"{TARGET_B_AGENT_ID}/{TARGET_B_INSTANCE_ID}"
    )
    log(
        f"FRAME_PUBLIC_ADDR={FRAME_PUBLIC_ADDR}, FRAME_CHUNK_SIZE={FRAME_CHUNK_SIZE}, "
        f"FRAME_MAX_BYTES={FRAME_STORE.max_frame_bytes}"
    )
    nats_runtime = NatsRuntime()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=GRPC_WORKERS),
        options=[
            ("grpc.max_receive_message_length", FRAME_CHUNK_SIZE + 64 * 1024),
            ("grpc.max_send_message_length", FRAME_CHUNK_SIZE + 64 * 1024),
        ],
    )
    agent_pb2_grpc.add_AgentServiceServicer_to_server(
        AgentService(nats_runtime),
        server,
    )
    frame_transport_pb2_grpc.add_FrameTransportServicer_to_server(
        FRAME_SERVICE,
        server,
    )

    listen_addr = "[::]:50051"
    server.add_insecure_port(listen_addr)
    log(f"binding gRPC server on {listen_addr}")

    try:
        server.start()
        log("gRPC server started and waiting for requests")
        server.wait_for_termination()
    finally:
        nats_runtime.close()


if __name__ == "__main__":
    serve()
