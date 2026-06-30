from contextlib import asynccontextmanager
import asyncio
import json
import os
import threading
import time
from typing import Any

import numpy as np
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.types.a2a_pb2 import TaskState
from fastapi import FastAPI, HTTPException
from prometheus_client import make_asgi_app

from utils.logger_utils import get_logger
from utils.numpy_utils import encode_array_to_dict, decode_array_from_dict
from utils.prometheus_metrics import (
    AgentCallTiming,
    get_current_timing,
    observe_call,
    reset_current_timing,
    set_current_timing,
)
from protocols import NatsComm
from fast_api.model_runtime import model_runtime
from mcp_clients.pointcloud_mcp_clients import PointCloudMCPClient

logger = get_logger(__name__)

# Configurable model checkpoint path for container/local deployment.
FIXED_MODEL_CHECKPOINT_PATH = os.getenv(
    "MODEL_CHECKPOINT_PATH",
    "checkpoints/point_pillar_where2comm/",
)

FRONTEND_CALLBACK_URL = os.getenv(
    "FRONTEND_CALLBACK_URL",
    "http://localhost:9002/temp/post_data",
)

MCP_SERVER_HOST = os.getenv("MCP_SERVER_HOST", "127.0.0.1")
MCP_SERVER_PORT = int(os.getenv("MCP_SERVER_PORT", "8123"))
RESOURCE_URI_PREFIX = "perception://2021_09_03_09_32_17/302"

NATS_SERVER_URL = os.getenv("NATS_SERVER_URL", "nats://nats:4222")
NATS_SUBJECT = os.getenv("NATS_SUBJECT", "workflow.demo.perception2feature.result")
A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:9031")

pointcloud_mcp_client = PointCloudMCPClient(host=MCP_SERVER_HOST, port=MCP_SERVER_PORT)

logger.info("NATS communication initialized with server: %s", NATS_SERVER_URL)
logger.info("A2A Agent URL initialized as: %s", A2A_AGENT_URL)
_nats_comm = NatsComm(servers=[NATS_SERVER_URL])
_execution_slots = asyncio.Semaphore(int(os.getenv("AGENT_MAX_CONCURRENT_TASKS", "1")))


_resource_index = 0
_resource_index_lock = threading.Lock()


def _next_resource_uri() -> str:
    global _resource_index
    with _resource_index_lock:
        current_index = _resource_index
        _resource_index += 1
    return f"{RESOURCE_URI_PREFIX}/{current_index}"


def _encode_numpy_payload(payload: Any) -> Any:
    if isinstance(payload, np.ndarray):
        return encode_array_to_dict(payload)

    if isinstance(payload, dict):
        return {key: _encode_numpy_payload(value) for key, value in payload.items()}

    if isinstance(payload, list):
        return [_encode_numpy_payload(item) for item in payload]

    return payload

# def _temp_post_data(data: dict) -> None:
#     """临时函数: 将数据发送到前端指定接口, 供调试使用."""
#     import requests

#     try:
#         response = requests.post(FRONTEND_CALLBACK_URL, json=data, timeout=5)
#         response.raise_for_status()
#         logger.info("Data posted to frontend successfully")
#     except Exception as exc:
#         logger.error(f"Failed to post data to frontend: {exc}")

async def _send_data(data: dict, nats_subject: str = NATS_SUBJECT) -> None:
    ack = await _nats_comm.send(
        subject=nats_subject,
        payload=data,
    )
    logger.info(f"Data sent to NATS subject '{nats_subject}' with ack: {ack}")


def _parse_a2a_text_payload(text: str) -> tuple[str, dict]:
    if not text:
        return "", {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text, {}

    if not isinstance(payload, dict):
        return text, {}

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    task_description = (
        payload.get("task_description")
        or payload.get("description")
        or payload.get("message")
        or text
    )
    return str(task_description), metadata


def _resolve_nats_subject(metadata: dict) -> str:
    return metadata.get("nats_subject") or NATS_SUBJECT


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Load model once when app starts."""
    try:
        model_runtime.load_model(FIXED_MODEL_CHECKPOINT_PATH)
        logger.info("Model loaded from fixed path: %s", FIXED_MODEL_CHECKPOINT_PATH)
        await _nats_comm.connect()
    except FileNotFoundError as exc:
        # Keep service alive in demo mode even when fixed path is not present.
        logger.error("Fixed checkpoint missing (%s), loading built-in demo weights", exc)
    except Exception as exc:
        logger.exception("Failed to load model during startup")
        raise RuntimeError(f"Startup model loading failed: {exc}") from exc

    try:
        yield
    finally:
        await _nats_comm.close()


app = FastAPI(title="Perception to Feature Agent Demo API", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


async def _run_model_forward(nats_subject: str = NATS_SUBJECT) -> dict:
    timing = get_current_timing()
    resource_uri = _next_resource_uri()

    stage_started = time.monotonic()
    try:
        perception_info = await pointcloud_mcp_client.fetch_perception_info(resource_uri)
    finally:
        if timing:
            timing.mcp_fetch_ms = (time.monotonic() - stage_started) * 1000

    stage_started = time.monotonic()
    try:
        if not isinstance(perception_info, dict):
            raise RuntimeError("perception_info is not a dict")

        if perception_info.get("status") == "error":
            message = perception_info.get("message", "Unknown MCP error")
            raise RuntimeError(f"MCP resource read failed: {message}")

        pointcloud = perception_info.get("pcd")

        if isinstance(pointcloud, dict) and pointcloud.get("data") is not None:
            try:
                pointcloud = decode_array_from_dict(pointcloud)
            except Exception as exc:
                logger.error(f"Failed to decode point cloud data: {exc}")
                pointcloud = None
        else:
            pointcloud = None

        if not isinstance(pointcloud, np.ndarray):
            raise RuntimeError("pcd is missing or not a decoded numpy array")

        intermediate_feature = model_runtime.pointcloud_inference(pointcloud=pointcloud)
        data = {
            "status": "success",
            "resource_uri": resource_uri,
            "intermediate_feature": _encode_numpy_payload(intermediate_feature),
            "pcd": perception_info.get("pcd"),
        }
    finally:
        if timing:
            timing.execution_ms = (time.monotonic() - stage_started) * 1000

    # _temp_post_data(data)
    stage_started = time.monotonic()
    try:
        await _send_data(data, nats_subject=nats_subject)
    finally:
        if timing:
            timing.nats_output_publish_ms = (time.monotonic() - stage_started) * 1000

    return {
        "status": "success",
        "resource_uri": resource_uri,
    }


@app.get("/model/forward")
async def model_forward(nats_subject: str = NATS_SUBJECT) -> dict:
    try:
        return await _run_model_forward(nats_subject=nats_subject)
    except Exception as exc:
        logger.exception("Forward inference failed")
        raise HTTPException(status_code=500, detail=f"Forward failed: {exc}") from exc


async def agent_function(nats_subject: str = NATS_SUBJECT) -> dict:
    return await _run_model_forward(nats_subject=nats_subject)


class Perception2IntermediateFeatureExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        request_received = time.monotonic()

        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Processing request..."),
        )

        timing = AgentCallTiming(task_id=task.id)
        status = "error"
        result: dict | None = None
        error_message: str | None = None
        acquired_slot = False
        timing_token = None

        try:
            await _execution_slots.acquire()
            acquired_slot = True
            timing.queue_wait_ms = (time.monotonic() - request_received) * 1000
            timing_token = set_current_timing(timing)

            text = get_message_text(context.message)
            task_description, metadata = _parse_a2a_text_payload(text)
            nats_subject = _resolve_nats_subject(metadata)

            logger.info(
                "Processing A2A task: description=%s, nats_subject=%s",
                task_description,
                nats_subject,
            )

            result = await agent_function(nats_subject=nats_subject)
            result_status = (
                result.get("status", "error") if isinstance(result, dict) else "error"
            )
            status = (
                result_status
                if result_status in {"success", "error", "timeout", "cancelled"}
                else "error"
            )
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            error_message = str(exc)
            logger.exception("Agent execution failed")
        finally:
            timing.server_total_ms = (time.monotonic() - request_received) * 1000
            observe_call(timing, status)
            logger.info(
                "[Agent QoS] %s",
                json.dumps(
                    {**timing.to_dict(), "status": status},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            if timing_token is not None:
                reset_current_timing(timing_token)
            if acquired_slot:
                _execution_slots.release()

        qos_metadata = {"qos": timing.to_dict()}
        if error_message is not None:
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(f"Request failed: {error_message}"),
                metadata=qos_metadata,
            )
            return

        result_text = json.dumps(result or {}, ensure_ascii=False, default=_json_default)
        await updater.add_artifact(
            parts=[new_text_part(text=result_text, media_type="text/plain")],
            name="perception2intermediatefeature-result",
            metadata=qos_metadata,
        )

        if status != "success":
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("Request failed."),
                metadata=qos_metadata,
            )
            return

        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Request is completed!"),
            metadata=qos_metadata,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


def _json_default(value: Any):
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def _build_agent_card() -> AgentCard:
    skill = AgentSkill(
        id="perception_to_intermediate_feature",
        name="Perception to Intermediate Feature",
        description=(
            "Fetches point cloud perception data from MCP, runs model inference "
            "to generate intermediate features, and publishes the result to NATS."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["perception", "intermediate-feature", "nats"],
        examples=[
            "Generate intermediate feature from the next point cloud resource",
            (
                '{"task_description": "Generate intermediate feature", '
                '"metadata": {"nats_subject": "workflow.demo.perception2feature.result"}}'
            ),
        ],
    )

    return AgentCard(
        name="Perception2IntermediateFeature Agent",
        description=(
            "FastAPI agent that converts point cloud perception data into "
            "intermediate features and exposes standard A2A JSON-RPC."
        ),
        version="0.1.6",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=A2A_AGENT_URL,
            )
        ],
        skills=[skill],
    )


_agent_card = _build_agent_card()
_request_handler = DefaultRequestHandler(
    agent_executor=Perception2IntermediateFeatureExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=_agent_card,
)

add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(_agent_card),
    jsonrpc_routes=create_jsonrpc_routes(_request_handler, rpc_url="/"),
)
