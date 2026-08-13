"""
FastAPI 应用主体 — Agent Template 的核心服务

架构概览:
  外部调用方 ──A2A JSON-RPC──> / ──> a2a-python RequestHandler
                                          │
                                          ├─ CooperativeFeatureFusionExecutor 解析标准 A2A Message
                                          │
                                          ├─ 从 data Part parameters 获取 NATS 输入来源集群
                                          │
                                          ├─ agent_function() ── 从 NATS 拉取上游数据
                                          │                      │
                                          │                      ├─ decode_structured_numpy() 还原 numpy 数组
                                          │                      │
                                          │                      ├─ 恢复稀疏中间特征图
                                          │                      ├─ 特征融合与目标检测
                                          │                      └─ Socket.IO 推送可视化帧
                                          │
                                          └─ 通过 A2A Task artifact 返回执行结果和 QoS metadata

标准 A2A 入口:
  GET  /.well-known/agent-card.json
  POST /

Prometheus 入口:
  GET  /metrics/

环境变量:
  A2A_AGENT_URL             : Agent Card 中声明的服务地址，默认 http://localhost:9032
  NATS_SERVER_URL           : NATS 服务器地址，默认 nats://nats:4222
  CLUSTER_ID                : 当前 Agent 所在 NATS JetStream domain/集群标识
  AGENT_ID                  : 当前 Agent 逻辑标识
  AGENT_INSTANCE_ID         : 当前运行实例标识
  AGENT_MAX_CONCURRENT_TASKS: 单实例并发执行槽数量，默认 1

开发者指南:
  1. 通过标准 A2A data Part 的 parameters 显式提供 NATS 输入路由
  2. Agent 只消费 NATS 输入，不向 NATS 发布业务输出
  3. 检测结果通过 A2A artifact 返回，可视化帧通过 Socket.IO 推送
"""

import asyncio
import base64
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import socketio
import torch
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import make_asgi_app

from runtime_api import NatsComm
from utils.logger_utils import get_logger
from fast_api.model_runtime import model_runtime
from utils.numpy_utils import decode_structured_numpy
from utils.open3d_utils import render_vis
from utils.prometheus_metrics import (
    AgentCallTiming,
    get_current_timing,
    observe_call,
    reset_current_timing,
    set_current_timing,
)


logger = get_logger(__name__)
BASE_DIR = Path(__file__).resolve().parent
FIXED_MODEL_CHECKPOINT_PATH = os.getenv(
    "MODEL_CHECKPOINT_PATH",
    "checkpoints/point_pillar_where2comm/",
)
_socketio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
_templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ──────────────────────────────────────────────────────────────────────
# A2A 与 NATS 连接配置（可通过环境变量覆盖）
# ──────────────────────────────────────────────────────────────────────

# Agent Card 中暴露给调用方的访问地址。
# 部署到不同环境时通常需要覆盖为网关地址、Pod Service 地址或公网地址。
A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:9032")

# NATS 服务器地址，容器环境中通常由编排系统注入。
# 默认值 nats://nats:4222 假设 FastAPI 服务和 NATS 运行在同一容器网络内。
NATS_SERVER_URL = os.getenv("NATS_SERVER_URL", "nats://nats:4222")

CLUSTER_ID = os.getenv("CLUSTER_ID", "").strip()

AGENT_ID = os.getenv("AGENT_ID", "").strip()

AGENT_INSTANCE_ID = os.getenv("AGENT_INSTANCE_ID", "").strip()

logger.info("A2A Agent URL initialized as: %s", A2A_AGENT_URL)
logger.info("NATS communication initialized with server: %s", NATS_SERVER_URL)

# 全局 NATS 通信实例，由 FastAPI lifespan 在启动阶段异步创建。
_nats_comm: NatsComm | None = None

# 单实例并发执行槽。等待该信号量的时间会记录为 queue_wait_ms。
_execution_slots = asyncio.Semaphore(int(os.getenv("AGENT_MAX_CONCURRENT_TASKS", "1")))


# ──────────────────────────────────────────────────────────────────────
# NATS 数据接收辅助函数
# ──────────────────────────────────────────────────────────────────────

def _get_nats_comm() -> NatsComm:
    """返回已在应用启动阶段创建的 NATS 通信客户端。"""
    if _nats_comm is None:
        raise RuntimeError("NATS communication client is not initialized")
    return _nats_comm


def _require_local_nats_values(values: dict[str, str]) -> None:
    """校验 NATS 输入所需的本地实例身份。"""
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing local environment variables for NATS input: "
            f"{', '.join(missing)}"
        )


def _require_source_cluster(parameters: dict) -> None:
    """校验调用方显式提供的 NATS 来源集群。"""
    if not isinstance(parameters.get("source_cluster"), str) or not parameters["source_cluster"].strip():
        raise ValueError("Missing A2A parameters for NATS input: source_cluster")


async def _receive_data_from_nats(
    source_cluster: str,
    operation: str = "in",
) -> dict:
    """
    从 NATS JetStream 拉取消息。

    参数:
      source_cluster:
        消息来源集群，用于选择当前实例的 local/global 输入 subject。
      operation:
        输入 subject 的操作名称，默认 in。

    返回:
      已确认消息的 payload。

    工作流程:
      1. 通过 pull_subscribe 订阅指定主题和持久化消费者
      2. 批量拉取消息（默认 1 条，超时 5 秒）
      3. 收到消息后立即确认并返回 payload
      4. 未取到消息或发生异常时记录日志，并通过 HTTPException 向上层暴露错误

    维护提示:
      当前模板一次只拉取 1 条消息。如果业务需要批处理，可以调整 batch，
      同时将返回值从单条 payload 改为列表，并同步修改 agent_function()。
    """
    _require_local_nats_values(
        {
            "CLUSTER_ID": CLUSTER_ID,
            "AGENT_ID": AGENT_ID,
            "AGENT_INSTANCE_ID": AGENT_INSTANCE_ID,
        },
    )
    _require_source_cluster({"source_cluster": source_cluster})
    subject = ""
    try:
        # NatsComm.receive() 是项目封装的 pull 模式消费接口。
        # batch=1 表示本次请求只消费一条上游结果；timeout_sec=5 避免请求无限等待。
        scope = "local" if source_cluster == CLUSTER_ID else "global"

        local_subject, global_subject = (
            _get_nats_comm().workflow_subscription_subjects(
                agent_id=AGENT_ID,
                operation=operation,
                local_cluster=CLUSTER_ID,
                instance_id=AGENT_INSTANCE_ID,
            )
        )
        subject = local_subject if scope == "local" else global_subject
        durable = subject.replace(".", "-")

        messages = await _get_nats_comm().receive(
            subject=subject,
            durable=durable,
            batch=1,
            timeout_sec=5,
        )
        for message in messages:
            logger.info("Received message on subject '%s'", subject)
            await message.ack()
            return message.payload
        raise HTTPException(
            status_code=504,
            detail=f"No messages received on subject '{subject}' within timeout",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error receiving message from NATS subject '%s'", subject)
        raise HTTPException(status_code=500, detail=f"Failed to receive message: {exc}") from exc


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
    {"task_description": "...", "parameters": {"source_cluster": "edge-a"}}
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
      - 创建新版 NATS JetStream 通信客户端
      - 加载特征融合检测模型

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

def _convert_numpy_to_tensor(payload: Any, device: str) -> Any:
    """递归将 numpy.ndarray 转换为模型设备上的 torch.Tensor。"""
    if isinstance(payload, dict):
        return {key: _convert_numpy_to_tensor(value, device) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_convert_numpy_to_tensor(value, device) for value in payload]
    if isinstance(payload, tuple):
        return tuple(_convert_numpy_to_tensor(value, device) for value in payload)
    if isinstance(payload, np.ndarray):
        return torch.from_numpy(payload.copy()).to(device)
    return payload


def _restore_original_feature_from_dict(
    feature_dict: dict,
    target_hw: tuple[int, int] = (96, 352),
) -> torch.Tensor:
    """根据稀疏 feature/mask 恢复完整中间特征图。"""
    if not isinstance(feature_dict, dict):
        raise TypeError("intermediate_feature must be a dict")
    if "feature" not in feature_dict or "mask" not in feature_dict:
        raise KeyError("intermediate_feature must contain 'feature' and 'mask'")

    converted = _convert_numpy_to_tensor(feature_dict, model_runtime.device)
    masked_feature = converted["feature"]
    mask = converted["mask"]
    if not isinstance(masked_feature, torch.Tensor) or not isinstance(mask, torch.Tensor):
        raise TypeError("'feature' and 'mask' must be numpy arrays")

    target_h, target_w = target_hw
    if mask.shape[-2:] != target_hw:
        mask = torch.nn.functional.interpolate(
            mask.float(), size=target_hw, mode="bilinear", align_corners=False
        )
    nonzero = torch.nonzero(mask.squeeze(0).squeeze(0) != 0, as_tuple=False)
    channels = masked_feature.shape[0]
    restored = torch.zeros(
        (1, channels, target_h, target_w),
        dtype=masked_feature.dtype,
        device=masked_feature.device,
    )
    if nonzero.numel() == 0:
        return restored
    if masked_feature.ndim != 2 or masked_feature.shape[1] != nonzero.shape[0]:
        raise ValueError(
            "Feature count mismatch: "
            f"{masked_feature.shape[-1]} vs {nonzero.shape[0]}"
        )
    # 分通道赋值可避免 PyTorch 高级索引改变轴顺序。
    for channel in range(channels):
        restored[0, channel, nonzero[:, 0], nonzero[:, 1]] = masked_feature[channel]
    return restored


def _encode_image_to_base64(image: np.ndarray | None) -> str:
    if image is None:
        return ""
    ok, buffer = cv2.imencode(".jpg", image)
    return base64.b64encode(buffer).decode("utf-8") if ok else ""


def _feature_image(feature: np.ndarray | None) -> np.ndarray | None:
    """将多通道特征图转换为旧版 UI 使用的伪彩色图。"""
    if feature is None or feature.size == 0:
        return None
    summed = feature.copy().squeeze().sum(0)
    minimum, maximum = float(np.min(summed)), float(np.max(summed))
    normalized = np.zeros_like(summed, dtype=np.float32)
    if maximum != minimum:
        normalized = (summed - minimum) / (maximum - minimum)
    green = np.array([0, 255, 0], dtype=np.float32)
    blue = np.array([255, 0, 0], dtype=np.float32)
    colors = (1 - normalized[..., None]) * green + normalized[..., None] * blue
    image = np.clip(colors, 0, 255).astype(np.uint8)
    return cv2.resize(
        image,
        (image.shape[1] * 5, image.shape[0] * 5),
        interpolation=cv2.INTER_NEAREST,
    )


async def _update_images(
    pcd_image: np.ndarray | None,
    ego_feature: np.ndarray | None,
    fused_feature: np.ndarray | None,
) -> None:
    """编码检测与特征图，并通过 Socket.IO 推送到旧版 Web UI。"""
    payload: dict[str, str] = {}
    if pcd_image is not None and pcd_image.size:
        pcd_bgr = cv2.cvtColor((pcd_image.copy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        payload["pcd_img"] = _encode_image_to_base64(pcd_bgr)
    ego_image = _feature_image(ego_feature)
    if ego_image is not None:
        payload["ego_feature_img"] = _encode_image_to_base64(ego_image)
    fused_image = _feature_image(fused_feature)
    if fused_image is not None:
        payload["fused_feature_img"] = _encode_image_to_base64(fused_image)
    if payload:
        await _socketio.emit("update_frames", payload)

async def agent_function(
    task_description: str = "",
    parameters: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Agent 核心业务处理函数。

    数据流:
      1. 从 NATS 输入主题拉取上游 Agent 的数据
      2. 将 numpy 数组从 base64 编码还原为 ndarray
      3. 恢复稀疏中间特征并执行融合检测
      4. 渲染检测结果并推送到 Web UI

    参数:
      task_description / parameters / metadata:
        调用方通过标准 A2A data Part 传入的任务信息，可在业务逻辑中直接使用。

    返回:
      A2A artifact 中展示的检测执行结果。
    """
    parameters = parameters or {}
    metadata = metadata or {}
    _require_source_cluster(parameters)
    _require_local_nats_values(
        {
            "CLUSTER_ID": CLUSTER_ID,
            "AGENT_ID": AGENT_ID,
            "AGENT_INSTANCE_ID": AGENT_INSTANCE_ID,
        }
    )
    timing = get_current_timing()

    stage_started = time.monotonic()
    # 1. 输入型 Agent 从调用方 parameters 指定来源的实例级 subject 读取数据。
    try:
        data = await _receive_data_from_nats(
            source_cluster=parameters["source_cluster"].strip(),
            operation=parameters.get("operation", "in"),
        )
    finally:
        if timing:
            timing.nats_input_wait_ms = (time.monotonic() - stage_started) * 1000

    # 2. 还原 numpy 结构。
    # NATS payload 通常是 JSON 友好的 dict，无法直接承载 ndarray；
    # decode_structured_numpy() 会递归识别项目约定的 numpy 编码结构并恢复为 ndarray。
    decoded_data = decode_structured_numpy(data)

    stage_started = time.monotonic()
    try:
        if not isinstance(decoded_data, dict):
            raise HTTPException(status_code=400, detail="NATS payload must be an object")
        masked_feature = decoded_data.get("intermediate_feature")
        if masked_feature is None:
            raise HTTPException(status_code=400, detail="intermediate_feature is missing in the payload")
        try:
            restored_feature = _restore_original_feature_from_dict(masked_feature)
        except (TypeError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Failed to restore feature map: {exc}") from exc

        ego_box, fused_box, ego_feature, fused_feature = (
            model_runtime.process_intermediate_features([restored_feature])
        )
        pcd = decoded_data.get("pcd")
        if not isinstance(pcd, np.ndarray):
            raise HTTPException(status_code=400, detail="pcd is missing in the payload")
        pcd_image = render_vis(None, pcd, ego_box, fused_box)
        await _update_images(pcd_image, ego_feature, fused_feature)
    finally:
        if timing:
            timing.execution_ms = (time.monotonic() - stage_started) * 1000

    return {
        "status": "success",
        "pred_box": ego_box.tolist() if ego_box is not None else None,
    }


# ──────────────────────────────────────────────────────────────────────
# a2a-python 执行器与 FastAPI 应用
# ──────────────────────────────────────────────────────────────────────

class CooperativeFeatureFusionExecutor(AgentExecutor):
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
            error_message = exc.detail if isinstance(exc, HTTPException) else str(exc)
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
            name="cooperative-feature-fusion-detection-viz-result",
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
    # 当前 Agent 只消费 NATS 中间特征并将可视化结果推送到 Web UI。
    skill = AgentSkill(
        id="cooperative_feature_fusion_detection_viz",
        name="Cooperative Feature Fusion Detection Visualization",
        description=(
            "Consumes intermediate feature data from NATS, runs cooperative "
            "feature fusion detection, and pushes visualization frames to the web UI."
        ),
        input_modes=["application/json", "text/plain"],
        output_modes=["text/plain"],
        tags=["cooperative-feature-fusion", "detection", "visualization", "nats"],
        examples=[
            "Run cooperative feature fusion detection visualization",
            (
                '{"task_description": "Run visualization", '
                '"parameters": {"source_cluster": "edge-a", "operation": "in"}, '
                '"metadata": {}}'
            ),
        ],
    )

    # AgentCard 描述整个 Agent。
    # supported_interfaces 中的 url 应与部署后的实际 A2A JSON-RPC 地址一致。
    return AgentCard(
        name="CooperativeFeatureFusionDetectionViz Agent",
        description=(
            "FastAPI visualization agent that consumes intermediate features "
            "from NATS and exposes standard A2A JSON-RPC."
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
fastapi_app = FastAPI(
    title="Cooperative Feature Fusion Detection Viz Agent API",
    lifespan=lifespan,
)
fastapi_app.mount("/metrics", make_asgi_app())
fastapi_app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)


@fastapi_app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return _templates.TemplateResponse(request, "home.html")


@fastapi_app.get("/get_id")
async def get_id():
    return {"id": "TEST"}


@_socketio.on("connect")
async def handle_connect(sid, environ, auth=None):
    logger.info("Client connected: %s", sid)

# 构建一次 Agent Card，并复用于 agent-card 路由和 JSON-RPC handler。
_agent_card = _build_agent_card()

# DefaultRequestHandler 是 a2a-python 的默认 JSON-RPC 请求处理器。
# - agent_executor: 真正执行任务的对象
# - task_store: 示例中使用内存存储，服务重启后任务状态不会保留
# - agent_card: 用于校验/描述当前 Agent 能力
_request_handler = DefaultRequestHandler(
    agent_executor=CooperativeFeatureFusionExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=_agent_card,
)

# 将标准 A2A 端点注册到 FastAPI:
# - GET  /.well-known/agent-card.json  返回 Agent Card
# - POST /                             接收 A2A JSON-RPC 请求
add_a2a_routes_to_fastapi(
    fastapi_app,
    agent_card_routes=create_agent_card_routes(_agent_card),
    jsonrpc_routes=create_jsonrpc_routes(_request_handler, rpc_url="/"),
)

# Socket.IO is the outer ASGI application; unmatched requests are delegated to FastAPI.
app = socketio.ASGIApp(_socketio, other_asgi_app=fastapi_app)
