"""
FastAPI 应用主体 — Agent Template 的核心服务

架构概览:
  外部调用方 ──A2A JSON-RPC──> / ──> a2a-python RequestHandler
                                          │
                                          ├─ Perception2IntermediateFeatureExecutor 解析标准 A2A Message
                                          │
                                          ├─ 从 message data Part 获取 NATS 输出路由
                                          │                      │
                                          ├─ agent_function() ── 从 MCP 获取点云并执行模型推理
                                          │                      │
                                          │                      ├─ encode_structured_numpy() 编码结果
                                          │                      │
                                          │                      └─ 将结果发布到 NATS 输出主题
                                          │
                                          └─ 通过 A2A Task artifact 返回执行结果和 QoS metadata

标准 A2A 入口:
  GET  /.well-known/agent-card.json
  POST /

Prometheus 入口:
  GET  /metrics/

环境变量:
  A2A_AGENT_URL             : Agent Card 中声明的服务地址，默认 http://localhost:9001
  NATS_SERVER_URL           : NATS 服务器地址，默认 nats://nats:4222
  CLUSTER_ID                : 当前 Agent 所在 NATS JetStream domain/集群标识
  AGENT_MAX_CONCURRENT_TASKS: 单实例并发执行槽数量，默认 1

开发者指南:
  1. 通过标准 A2A data Part 的 parameters 显式提供 NATS 输出路由
  2. Agent 从 MCP 读取点云并生成中间特征
  3. 推理结果通过新版 NATS workflow 路由发布给下游 Agent
"""

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from a2a.helpers import (
    get_data_parts,
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
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from runtime_api import NatsComm
from fast_api.model_runtime import model_runtime
from mcp_clients.pointcloud_mcp_clients import PointCloudMCPClient
from utils.logger_utils import get_logger
from utils.numpy_utils import decode_structured_numpy, encode_structured_numpy
from utils.prometheus_metrics import (
    AgentCallTiming,
    get_current_timing,
    observe_call,
    reset_current_timing,
    set_current_timing,
)


logger = get_logger(__name__)

# Model and MCP settings used by the perception-to-feature business logic.
FIXED_MODEL_CHECKPOINT_PATH = os.getenv(
    "MODEL_CHECKPOINT_PATH",
    "checkpoints/point_pillar_where2comm/",
)
MCP_SERVER_HOST = os.getenv("MCP_SERVER_HOST", "127.0.0.1")
MCP_SERVER_PORT = int(os.getenv("MCP_SERVER_PORT", "8123"))
RESOURCE_URI_PREFIX = os.getenv(
    "RESOURCE_URI_PREFIX",
    "perception://2021_09_03_09_32_17/302",
)

pointcloud_mcp_client = PointCloudMCPClient(
    host=MCP_SERVER_HOST,
    port=MCP_SERVER_PORT,
)
_resource_index = 0
_resource_index_lock = threading.Lock()


def _next_resource_uri() -> str:
    """Return the next point-cloud resource URI for this process."""
    global _resource_index
    with _resource_index_lock:
        current_index = _resource_index
        _resource_index += 1
    return f"{RESOURCE_URI_PREFIX}/{current_index}"

# ──────────────────────────────────────────────────────────────────────
# A2A 与 NATS 连接配置（可通过环境变量覆盖）
# ──────────────────────────────────────────────────────────────────────

# Agent Card 中暴露给调用方的访问地址。
# 部署到不同环境时通常需要覆盖为网关地址、Pod Service 地址或公网地址。
A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:9031")

# NATS 服务器地址，容器环境中通常由编排系统注入。
# 默认值 nats://nats:4222 假设 FastAPI 服务和 NATS 运行在同一容器网络内。
NATS_SERVER_URL = os.getenv("NATS_SERVER_URL", "nats://nats:4222")

CLUSTER_ID = os.getenv("CLUSTER_ID", "").strip()

logger.info("A2A Agent URL initialized as: %s", A2A_AGENT_URL)
logger.info("NATS communication initialized with server: %s", NATS_SERVER_URL)

# 全局 NATS 通信实例，由 FastAPI lifespan 在启动阶段异步创建。
_nats_comm: NatsComm | None = None

# 单实例并发执行槽。等待该信号量的时间会记录为 queue_wait_ms。
_execution_slots = asyncio.Semaphore(int(os.getenv("AGENT_MAX_CONCURRENT_TASKS", "1")))


# ──────────────────────────────────────────────────────────────────────
# NATS 数据发送辅助函数
# ──────────────────────────────────────────────────────────────────────

def _get_nats_comm() -> NatsComm:
    """返回已在应用启动阶段创建的 NATS 通信客户端。"""
    if _nats_comm is None:
        raise RuntimeError("NATS communication client is not initialized")
    return _nats_comm


def _require_local_nats_values(values: dict[str, str]) -> None:
    """校验发送 NATS 消息所需的本地身份配置。"""
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing local environment variables for NATS output: "
            f"{', '.join(missing)}"
        )


def _require_output_parameters(parameters: dict) -> None:
    """校验调用方在 data Part.parameters 中显式提供的输出路由。"""
    names = ("target_cluster", "target_agent_id", "target_instance_id")
    missing = [
        name
        for name in names
        if not isinstance(parameters.get(name), str) or not parameters[name].strip()
    ]
    if missing:
        raise ValueError(
            f"Missing A2A parameters for NATS output: {', '.join(missing)}"
        )


async def _send_data_to_nats(
    data: dict,
    target_cluster: str,
    target_agent_id: str,
    target_instance_id: str,
    operation: str = "in",
) -> None:
    """
    将数据发布到 NATS JetStream 输出主题。

    参数:
      data:
        要发给下游 Agent 的结果字典。若包含 numpy.ndarray，请先调用
        encode_structured_numpy()，否则普通 JSON 传输无法保留 dtype/shape。
      target_cluster / target_agent_id / target_instance_id:
        调用方通过 data Part.parameters 显式给出的下游路由。
      operation:
        输出 subject 的操作名称，默认 in。
    """
    _require_local_nats_values({"CLUSTER_ID": CLUSTER_ID})
    output_parameters = {
        "target_cluster": target_cluster,
        "target_agent_id": target_agent_id,
        "target_instance_id": target_instance_id,
    }
    _require_output_parameters(output_parameters)

    # send() 返回的 ack 可用于确认 JetStream 已接收消息，日志中保留它方便排障。
    ack = await _get_nats_comm().send_workflow(
        target_cluster=target_cluster,
        agent_id=target_agent_id,
        target_instance_id=target_instance_id,
        payload=data,
        operation=operation,
        local_cluster=CLUSTER_ID,
    )
    logger.info("Data sent to NATS subject with ack: %s", ack)


def _parse_a2a_payload(payload: Any) -> tuple[str, dict, dict]:
    """解析 data Part 或已经解码的 JSON text payload。"""
    if not isinstance(payload, dict):
        raise ValueError("A2A structured payload must be an object")

    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("A2A payload metadata must be an object")

    parameters = payload.get("parameters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("A2A payload parameters must be an object")

    task_description = (
        payload.get("task_description")
        or payload.get("description")
        or payload.get("message")
        or ""
    )
    return str(task_description), metadata, parameters


def _parse_a2a_text_payload(text: str) -> tuple[str, dict, dict]:
    """
    解析标准 A2A text message。

    支持普通文本，或形如:
    {"task_description": "...", "parameters": {"target_cluster": "edge-a", ...}}
    的 JSON 文本。

    返回:
      (task_description, metadata, parameters)

    设计目的:
      A2A 的 text/plain 输入足够通用，但模板还需要携带工作流运行时配置。
      因此这里约定: 普通文本作为任务描述；JSON 文本使用与 data Part 相同的结构。
    """
    if not text:
        # 空消息仍然返回稳定的 tuple，避免调用方额外处理 None。
        return "", {}, {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # 不是 JSON 时，把整段文本当作任务描述；metadata 为空，后续使用环境变量默认值。
        return text, {}, {}

    if not isinstance(payload, dict):
        # JSON 数组、字符串、数字等都不符合模板约定，同样退回普通文本模式。
        return text, {}, {}

    task_description, metadata, parameters = _parse_a2a_payload(payload)
    return task_description or text, metadata, parameters


def _parse_a2a_message_payload(message) -> tuple[str, dict, dict]:
    """优先解析标准 data Part，并兼容旧 text Part 调用方。"""
    data_parts = get_data_parts(message.parts)
    if data_parts:
        return _parse_a2a_payload(data_parts[0])
    return _parse_a2a_text_payload(get_message_text(message))


# ──────────────────────────────────────────────────────────────────────
# 应用生命周期管理
# ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    """
    FastAPI 生命周期上下文管理器。

    启动阶段 (yield 前):
      - 加载模型 / 初始化资源
      - 当前为占位代码，替换为你的实际初始化逻辑

    关闭阶段 (finally):
      - 释放 NATS 连接

    为什么放在 lifespan:
      FastAPI 会在应用启动时进入 yield 之前的代码，在应用关闭时执行 finally。
      适合放置模型加载、数据库连接池、NATS 连接等进程级资源的准备和清理。
    """
    global _nats_comm

    try:
        # create() 会建立连接并等待默认 JetStream Stream 就绪。
        _nats_comm = await NatsComm.create(servers=[NATS_SERVER_URL])

        model_runtime.load_model(FIXED_MODEL_CHECKPOINT_PATH)
        logger.info("Model loaded from fixed path: %s", FIXED_MODEL_CHECKPOINT_PATH)
    except Exception as exc:
        logger.exception("Failed to initialize application resources")
        if _nats_comm is not None:
            await _nats_comm.close()
            _nats_comm = None
        raise RuntimeError(f"Application startup failed: {exc}") from exc

    try:
        yield
    finally:
        # 关闭阶段统一释放应用级 NATS 长连接。
        if _nats_comm is not None:
            await _nats_comm.close()
            _nats_comm = None


# ──────────────────────────────────────────────────────────────────────
# 核心 Agent 业务逻辑
# ──────────────────────────────────────────────────────────────────────

async def agent_function(
    task_description: str = "",
    parameters: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Agent 核心业务处理函数。

    数据流:
      1. 从 MCP 获取点云数据
      2. 执行模型推理
      3. 将结果中的 numpy 数组编码为 base64 字典
      4. 将编码后的结果发布到 NATS 输出主题

    参数:
      task_description / parameters / metadata:
        调用方通过标准 A2A data Part 传入的任务信息，可在业务逻辑中直接使用。

    返回:
      A2A artifact 中展示的轻量执行结果。真正传给下游 Agent 的业务数据
      已通过 _send_data_to_nats() 发布到 NATS。
    """
    parameters = parameters or {}
    metadata = metadata or {}
    _require_output_parameters(parameters)
    _require_local_nats_values({"CLUSTER_ID": CLUSTER_ID})
    timing = get_current_timing()

    resource_uri = _next_resource_uri()

    # 1. 从 MCP 获取点云并执行点云到中间特征的模型推理。
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

        pointcloud = decode_structured_numpy(perception_info.get("pcd"))
        if not isinstance(pointcloud, np.ndarray):
            raise RuntimeError("pcd is missing or not a decoded numpy array")

        intermediate_feature = model_runtime.pointcloud_inference(
            pointcloud=pointcloud,
        )
    finally:
        if timing:
            timing.execution_ms = (time.monotonic() - stage_started) * 1000

    # 2. 组织下游 payload，并递归编码模型输出中的 numpy 数组。
    result = {
        "status": "success",
        "resource_uri": resource_uri,
        "intermediate_feature": encode_structured_numpy(intermediate_feature),
        "pcd": perception_info.get("pcd"),
    }

    # 3. 发布给下游 Agent。A2A 响应只返回简短状态，业务数据走 NATS。
    stage_started = time.monotonic()

    try:
        await _send_data_to_nats(
            result,
            target_cluster=parameters["target_cluster"].strip(),
            target_agent_id=parameters["target_agent_id"].strip(),
            target_instance_id=parameters["target_instance_id"].strip(),
            operation=parameters.get("operation", "in"),
        )
    finally:
        if timing:
            timing.nats_output_publish_ms = (
                time.monotonic() - stage_started
            ) * 1000

    return {
        "status": "success",
        "resource_uri": resource_uri,
    }


# ──────────────────────────────────────────────────────────────────────
# a2a-python 执行器与 FastAPI 应用
# ──────────────────────────────────────────────────────────────────────

class Perception2IntermediateFeatureExecutor(AgentExecutor):
    """
    a2a-python 标准 AgentExecutor，负责桥接 A2A 请求与模板业务逻辑。

    execute() 的职责:
      1. 获取或创建 A2A Task
      2. 将任务状态标记为 WORKING
      3. 从 A2A message data Part 中解析任务描述、parameters 和 metadata
      4. 调用 agent_function() 执行业务逻辑
      5. 写入 artifact，并把任务标记为 COMPLETED 或 FAILED

    这个类是 A2A 层和业务层的边界:
      - A2A 协议相关的 task/status/artifact 处理放在这里
      - 具体业务和 NATS 数据处理放在 agent_function()
    """

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 如果 a2a-python 已经为当前请求创建了 task，则继续使用它；
        # 否则根据用户消息新建 task，并先发送给事件队列，让调用方能看到任务已创建。
        request_received = time.monotonic()

        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        # TaskUpdater 是 a2a-python 提供的状态更新工具。
        # 后续所有 task 状态变更和 artifact 添加都通过它进入 event_queue。
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )

        # 先把任务状态置为 WORKING，避免长时间处理时调用方误以为请求没有开始。
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

            # 标准路径使用 data Part；迁移期仍兼容旧 text Part 调用方。
            task_description, metadata, parameters = _parse_a2a_message_payload(
                context.message
            )

            logger.info(
                "Processing A2A task: description=%s, parameters=%s, metadata=%s",
                task_description,
                parameters,
                metadata,
            )

            # 将协议层解析出的 NATS 配置交给业务函数。
            # execute() 不直接处理业务 payload，保持 A2A 桥接层职责单一。
            result = await agent_function(
                task_description=task_description,
                parameters=parameters,
                metadata=metadata,
            )
            result_status = result.get("status", "error") if isinstance(result, dict) else "error"
            status = result_status if result_status in {"success", "error", "timeout", "cancelled"} else "error"
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

        # artifact 是 A2A task 的标准结果载体。
        # ensure_ascii=False 保留中文等非 ASCII 字符，便于调试和前端展示。
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
    """json.dumps(default=...) 的兜底序列化函数。"""
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def _build_agent_card() -> AgentCard:
    """
    构建 A2A Agent Card。

    Agent Card 是 A2A 服务的能力说明，调用方会通过
    /.well-known/agent-card.json 读取它来了解:
      - Agent 名称和版本
      - 支持的输入/输出模式
      - JSON-RPC 调用地址
      - 可调用技能及示例

    修改模板用途时，通常需要同步更新 skill 的 id/name/description/tags/examples。
    """
    # Skill 描述单个可调用能力。
    # 当前 Agent 只暴露一个技能: 从 MCP 获取点云、执行推理并发布到 NATS。
    skill = AgentSkill(
        id="perception_to_intermediate_feature",
        name="Perception to Intermediate Feature",
        description=(
            "Fetches point cloud perception data from MCP, runs model inference "
            "to generate intermediate features, and publishes the result to NATS."
        ),
        input_modes=["application/json", "text/plain"],
        output_modes=["text/plain"],
        tags=["perception", "intermediate-feature", "nats"],
        examples=[
            "Generate intermediate feature from the next point cloud resource",
            (
                '{"task_description": "Generate intermediate feature", '
                '"parameters": {"target_cluster": "edge-b", '
                '"target_agent_id": "downstream-agent", '
                '"target_instance_id": "downstream-instance"}, '
                '"metadata": {}}'
            ),
        ],
    )

    # AgentCard 描述整个 Agent。
    # supported_interfaces 中的 url 应与部署后的实际 A2A JSON-RPC 地址一致。
    return AgentCard(
        name="Perception2IntermediateFeature Agent",
        description=(
            "FastAPI agent that converts point cloud perception data into "
            "intermediate features and exposes standard A2A JSON-RPC."
        ),
        version="0.2.0",
        default_input_modes=["application/json", "text/plain"],
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


# FastAPI 应用实例。
# lifespan 负责启动/关闭阶段的资源管理；A2A 路由随后被挂载到该应用上。
app = FastAPI(title="Perception to Feature Agent Demo API", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())

# 构建一次 Agent Card，并复用于 agent-card 路由和 JSON-RPC handler。
_agent_card = _build_agent_card()

# DefaultRequestHandler 是 a2a-python 的默认 JSON-RPC 请求处理器。
# - agent_executor: 真正执行任务的对象
# - task_store: 示例中使用内存存储，服务重启后任务状态不会保留
# - agent_card: 用于校验/描述当前 Agent 能力
_request_handler = DefaultRequestHandler(
    agent_executor=Perception2IntermediateFeatureExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=_agent_card,
)

# 将标准 A2A 端点注册到 FastAPI:
# - GET  /.well-known/agent-card.json  返回 Agent Card
# - POST /                             接收 A2A JSON-RPC 请求
add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(_agent_card),
    jsonrpc_routes=create_jsonrpc_routes(_request_handler, rpc_url="/"),
)
