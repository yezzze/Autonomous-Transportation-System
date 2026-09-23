"""
FastAPI 应用主体 — Agent Template 的核心服务

架构概览:
  外部调用方 ──A2A JSON-RPC──> / ──> a2a-python RequestHandler
                                          │
                                          ├─ AgentTemplateExecutor 解析标准 A2A Message
                                          │
                                          ├─ 从 message data Part 或环境变量获取 NATS 主题
                                          │
                                          ├─ agent_function() ── 从 NATS 拉取上游数据
                                          │                      │
                                          │                      ├─ decode_structured_numpy() 还原 numpy 数组
                                          │                      │
                                          │                      ├─ [此处编写你的业务逻辑]
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
  AGENT_ID                  : 当前 Agent 逻辑标识
  AGENT_INSTANCE_ID         : 当前运行实例标识
  AGENT_MAX_CONCURRENT_TASKS: 单实例并发执行槽数量，默认 1

开发者指南:
  1. 在 agent_function() 中的 "模拟处理时间" 位置替换为你的业务逻辑
  2. 在 lifespan() 中的 "模型加载" 位置替换为你的模型初始化代码
  3. 通过标准 A2A data Part 的 parameters 显式提供 NATS 输入和输出路由
"""

import asyncio
import base64
import json
import os
import random
import shutil
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

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
from fastapi import FastAPI, HTTPException
from prometheus_client import make_asgi_app

from runtime_api import NatsComm
from utils.logger_utils import get_logger
from utils.numpy_utils import decode_structured_numpy, encode_structured_numpy
from utils.prometheus_metrics import (
    AgentCallTiming,
    get_current_timing,
    observe_call,
    observe_performance_metrics,
    reset_current_timing,
    set_current_timing,
)


logger = get_logger(__name__)

from common.config import load_config
from vehicle.accident_detector import build_accident_detector

RUNTIME_CONFIG_PATH = os.getenv(
    "AGENT_RUNTIME_CONFIG",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.json",
    ),
)

# 业务运行时：(config, detector)，由 lifespan 初始化。
_runtime = None

# video_auto 模式在进程生命周期内维护当前视频下标。Agent 默认单并发执行，
# 因而每次调用会稳定地取得排序后的下一个视频；进程重启后从头开始。
_video_auto_index = 0


def _manifest_video_entries(
    manifest_path: Path,
    video_list_path: Path,
) -> list[tuple[str, Path]]:
    """按 VIDEO_LIST 顺序，将 manifest 相对路径拼到 CONTAINER_DATA_DIR。"""
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 VIDEO_MANIFEST={manifest_path}: {exc}") from exc

    container_data_dir = Path(os.getenv("CONTAINER_DATA_DIR", "/app/data"))
    manifest_index: dict[str, Path] = {}
    for item in manifest_data if isinstance(manifest_data, list) else []:
        video_id = str(item.get("id", "")).strip()
        source = str(item.get("video", "")).replace("\\", "/").strip()
        if not video_id or not source:
            continue
        relative = Path(source)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(
                f"VIDEO_MANIFEST 中 {video_id} 的 video 必须是相对 "
                f"CONTAINER_DATA_DIR 的安全路径，实际为：{source}"
            )
        manifest_index[video_id] = container_data_dir / relative

    try:
        lines = video_list_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"无法读取 VIDEO_LIST={video_list_path}: {exc}") from exc

    video_ids: list[str] = []
    for raw_line in lines:
        video_id = raw_line.strip()
        if not video_id or video_id.startswith("#"):
            continue
        if video_id.startswith(("LABEL_AGREE_EVENT ", "LABEL_AGREE_FULL ")):
            parts = video_id.split()
            video_id = parts[1] if len(parts) > 1 else ""
        if video_id and video_id not in video_ids:
            video_ids.append(video_id)

    entries: list[tuple[str, Path]] = []
    for video_id in video_ids:
        video_path = manifest_index.get(video_id)
        if video_path is None:
            raise FileNotFoundError(
                f"VIDEO_MANIFEST={manifest_path} 中找不到 VIDEO_LIST 样本 {video_id}"
            )
        entries.append((video_id, video_path))

    return entries

# ──────────────────────────────────────────────────────────────────────
# A2A 与 NATS 连接配置（可通过环境变量覆盖）
# ──────────────────────────────────────────────────────────────────────

# Agent Card 中暴露给调用方的访问地址。
# 部署到不同环境时通常需要覆盖为网关地址、Pod Service 地址或公网地址。
A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:9001")

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
# NATS 数据收发辅助函数
# ──────────────────────────────────────────────────────────────────────

def _get_nats_comm() -> NatsComm:
    """返回已在应用启动阶段创建的 NATS 通信客户端。"""
    if _nats_comm is None:
        raise RuntimeError("NATS communication client is not initialized")
    return _nats_comm


def _require_local_nats_values(flow: str, values: dict[str, str]) -> None:
    """按输入/输出流程分别校验本地 NATS 身份配置。"""
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing local environment variables for NATS {flow}: "
            f"{', '.join(missing)}"
        )


def _require_call_parameters(
    flow: str,
    parameters: dict,
    names: tuple[str, ...],
) -> None:
    """校验调用方在 data Part.parameters 中显式提供的路由参数。"""
    missing = [
        name
        for name in names
        if not isinstance(parameters.get(name), str) or not parameters[name].strip()
    ]
    if missing:
        raise ValueError(
            f"Missing A2A parameters for NATS {flow}: {', '.join(missing)}"
        )


def _requested_nats_flows(parameters: dict) -> tuple[bool, bool]:
    """根据显式路由字段判断本次调用需要 NATS 输入、输出或两者。"""
    input_requested = "source_cluster" in parameters
    output_names = ("target_cluster", "target_agent_id", "target_instance_id")
    output_requested = any(name in parameters for name in output_names)
    if not input_requested and not output_requested:
        raise ValueError(
            "A2A parameters must define NATS input (source_cluster), "
            "NATS output (target_cluster/target_agent_id/target_instance_id), "
            "or both"
        )
    return input_requested, output_requested


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
        "input",
        {
            "CLUSTER_ID": CLUSTER_ID,
            "AGENT_ID": AGENT_ID,
            "AGENT_INSTANCE_ID": AGENT_INSTANCE_ID,
        },
    )
    _require_call_parameters(
        "input",
        {"source_cluster": source_cluster},
        ("source_cluster",),
    )
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
    _require_local_nats_values("output", {"CLUSTER_ID": CLUSTER_ID})
    output_parameters = {
        "target_cluster": target_cluster,
        "target_agent_id": target_agent_id,
        "target_instance_id": target_instance_id,
    }
    _require_call_parameters(
        "output",
        output_parameters,
        ("target_cluster", "target_agent_id", "target_instance_id"),
    )

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
      - 加载模型 / 初始化资源
      - 当前为占位代码，替换为你的实际初始化逻辑

    关闭阶段 (finally):
      - 释放 NATS 连接

    为什么放在 lifespan:
      FastAPI 会在应用启动时进入 yield 之前的代码，在应用关闭时执行 finally。
      适合放置模型加载、数据库连接池、NATS 连接等进程级资源的准备和清理。
    """
    global _nats_comm, _runtime

    try:
        # create() 会建立连接并等待默认 JetStream Stream 就绪。
        _nats_comm = await NatsComm.create(servers=[NATS_SERVER_URL])

        # 模型加载：车端 MobileNet 事故初筛检测器。
        config = load_config(RUNTIME_CONFIG_PATH, role="vehicle")
        detector = build_accident_detector(config)
        _runtime = (config, detector)
        logger.info(
            "Vehicle accident detector loaded (mock=%s)",
            config["runtime"]["mock"],
        )
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
      1. 从 NATS 输入主题拉取上游 Agent 的数据
      2. 将 numpy 数组从 base64 编码还原为 ndarray
      3. 执行实际业务处理（模型推理、数据变换等）
      4. 将结果中的 numpy 数组编码为 base64 字典
      5. 将编码后的结果发布到 NATS 输出主题

    参数:
      task_description / parameters / metadata:
        调用方通过标准 A2A data Part 传入的任务信息，可在业务逻辑中直接使用。

    返回:
      A2A artifact 中展示的轻量执行结果。真正传给下游 Agent 的业务数据
      已通过 _send_data_to_nats() 发布到 NATS。
    """
    global _video_auto_index

    parameters = parameters or {}
    metadata = metadata or {}
    input_requested, output_requested = _requested_nats_flows(parameters)
    if input_requested:
        _require_call_parameters(
            "input",
            parameters,
            ("source_cluster",),
        )
        _require_local_nats_values(
            "input",
            {
                "CLUSTER_ID": CLUSTER_ID,
                "AGENT_ID": AGENT_ID,
                "AGENT_INSTANCE_ID": AGENT_INSTANCE_ID,
            },
        )
    if output_requested:
        _require_call_parameters(
            "output",
            parameters,
            ("target_cluster", "target_agent_id", "target_instance_id"),
        )
        _require_local_nats_values("output", {"CLUSTER_ID": CLUSTER_ID})
    timing = get_current_timing()

    stage_started = time.monotonic()
    # 1. 输入型 Agent 从调用方 parameters 指定来源的实例级 subject 读取数据。
    if input_requested:
        try:
            data = await _receive_data_from_nats(
                source_cluster=parameters["source_cluster"].strip(),
                operation=parameters.get("operation", "in"),
            )
        finally:
            if timing:
                timing.nats_input_wait_ms = (
                    time.monotonic() - stage_started
                ) * 1000
    else:
        # 仅输出型 Agent 的业务逻辑应在开发者替换区自行生成数据。
        data = {}

    # 2. 还原 numpy 结构。
    # NATS payload 通常是 JSON 友好的 dict，无法直接承载 ndarray；
    # decode_structured_numpy() 会递归识别项目约定的 numpy 编码结构并恢复为 ndarray。
    decode_data = decode_structured_numpy(data)

    # TODO: 在此处编写你的业务逻辑
    # ─── 开发者替换区: 在此处添加你的业务逻辑 ───
    # 车端是链路起点：无上游 NATS 数据，输入视频由 A2A parameters/metadata 提供。
    stage_started = time.monotonic()
    temp_input = None
    try:
        config, detector = _runtime
        video_path = parameters.get("video_path") or metadata.get("video_path")
        video_base64 = parameters.get("video_base64") or metadata.get("video_base64")

        # 显式视频输入拥有最高优先级。只有二者都未提供时，才检查
        # parameters.video_auto；若该参数缺失，则回退到环境变量 VIDEO_PATH。
        video_auto = parameters.get("video_auto") or os.getenv("VIDEO_PATH", "false").strip().lower()

        if isinstance(video_auto, str):
            video_auto = video_auto.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            video_auto = bool(video_auto)

        auto_selected = False
        if video_path:
            input_path = Path(str(video_path))
            if not input_path.is_file():
                raise FileNotFoundError(f"video_path 不存在或不是文件：{input_path}")
        elif video_base64:
            work_dir = Path(config["paths"]["work_dir"])
            work_dir.mkdir(parents=True, exist_ok=True)
            temp_input = work_dir / "a2a_input.mp4"
            temp_input.write_bytes(base64.b64decode(video_base64))
            input_path = temp_input
        elif video_auto:
            manifest_env = os.getenv("VIDEO_MANIFEST", "").strip()
            video_list_env = os.getenv("VIDEO_LIST", "").strip()
            if manifest_env and video_list_env:
                video_entries = _manifest_video_entries(
                    Path(manifest_env),
                    Path(video_list_env),
                )
                auto_source = f"VIDEO_LIST={video_list_env}"
            else:
                dataset_dir = Path(os.getenv("CONTAINER_DATA_DIR", "/app/data"))
                video_entries = (
                    [
                        (
                            "_".join(
                                path.relative_to(dataset_dir).with_suffix("").parts
                            ),
                            path,
                        )
                        for path in sorted(
                            path
                            for path in dataset_dir.rglob("*")
                            if path.is_file() and path.suffix.lower() == ".mp4"
                        )
                    ]
                    if dataset_dir.is_dir()
                    else []
                )
                auto_source = os.getenv("CONTAINER_DATA_DIR", "/app/data")

            # 前 N 次调用分别执行一个视频；第 N+1 次才通知工作流终止。
            if _video_auto_index >= len(video_entries):
                return {
                    "status": "success",
                    "workflow_control": {
                        "terminate": True,
                        "reason": (
                            f"video_auto 已执行完 {auto_source} 中的全部 "
                            f"{len(video_entries)} 个 MP4 视频"
                        ),
                    },
                }
            auto_sample_id, input_path = video_entries[_video_auto_index]
            auto_selected = True
        else:
            raise ValueError(
                "缺少参数 video_path 或 video_base64，且 video_auto 未启用"
            )

        if auto_selected:
            sample_id = auto_sample_id
        else:
            sample_id = str(
                parameters.get("sample_id")
                or metadata.get("sample_id")
                or "a2a_task"
            )
        task_type = str(
            parameters.get("task_type") or os.getenv("TASK_TYPE", "ours")
        ).strip().lower()
        if task_type == "baseline":
            # 传统链路：端不处理，完整视频原样转发给边端
            processed = {
                "sample_id": sample_id,
                "video_base64": base64.b64encode(input_path.read_bytes()).decode(
                    "ascii"
                ),
                "mode": "traditional",
            }
            result = {
                "status": "success",
                "processed_data": encode_structured_numpy(processed),
            }
            observe_performance_metrics(
                {"video_bytes": float(input_path.stat().st_size)}
            )
        else:
            # 被测链路：MobileNet 初筛并导出采样帧
            output_dir = Path(config["paths"]["work_dir"]) / "vehicle_output"
            output_dir.mkdir(parents=True, exist_ok=True)
            sample = {"id": sample_id}
            detection = detector.detect(input_path, output_dir, sample=sample)

            frames = []
            for path_value in detection.get("clip_paths", []):
                path = Path(str(path_value))
                if path.exists() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    frames.append(
                        {
                            "filename": path.name,
                            "data_base64": base64.b64encode(path.read_bytes()).decode(
                                "ascii"
                            ),
                        }
                    )

            processed = {
                "sample_id": sample["id"],
                "event_detected": bool(detection.get("event_detected")),
                "segments": detection.get("segments", []),
                "detector_metadata": detection.get("detector_metadata", {}),
                "timings_ms": detection.get("timings_ms", {}),
                "frames_base64": frames,
                "num_frames": len(frames),
            }
            frame_wire_bytes = sum(
                len(str(item.get("data_base64") or "")) * 3 // 4
                for item in frames
                if isinstance(item, dict)
            )
            result = {
                "status": "success",
                "processed_data": encode_structured_numpy(processed),
            }
            detection_ms = float(
                detection.get("timings_ms", {}).get("vehicle_accident_detection", 0.0)
            )
            observe_performance_metrics(
                {
                    "vehicle_detection_ms": detection_ms,
                    "frame_count": float(len(frames)),
                    "frame_bytes": float(frame_wire_bytes),
                }
            )

    finally:
        if temp_input is not None:
            temp_input.unlink(missing_ok=True)
        if timing:
            timing.execution_ms = (time.monotonic() - stage_started) * 1000
    # ─── 开发者替换区结束 ───

    # 4. 发布给下游 Agent。A2A 响应只返回简短状态，业务数据走 NATS。
    stage_started = time.monotonic()

    if output_requested:
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

    # 仅在处理及下游发布均成功后推进游标；发生异常时，下次调用会重试。
    if auto_selected:
        _video_auto_index += 1

    return {
        "status": "success",
    }


# ──────────────────────────────────────────────────────────────────────
# a2a-python 执行器与 FastAPI 应用
# ──────────────────────────────────────────────────────────────────────

class AgentTemplateExecutor(AgentExecutor):
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
                    {
                        **timing.to_dict(),
                        "status": status,
                        "performance": dict(timing.performance),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            if timing_token is not None:
                reset_current_timing(timing_token)
            if acquired_slot:
                _execution_slots.release()

        qos_metadata = {
            "qos": timing.to_dict(),
            "performance": dict(timing.performance),
        }
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
            name="agent-template-result",
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
    # 当前模板只暴露一个技能: 从 NATS 读取数据、执行处理、再发布到 NATS。
    skill = AgentSkill(
        id="vehicle_accident_prefilter",
        name="Vehicle Accident Prefilter Agent",
        description=(
            "Runs MobileNetV3-Small accident screening on an input video, "
            "exports sampled frames, and publishes frames to the road agent "
            "through NATS."
        ),
        input_modes=["application/json", "text/plain"],
        output_modes=["text/plain"],
        tags=["vehicle-agent", "accident-screening"],
        examples=[
            "Screen the input video for accidents",
            (
                '{"task_description": "Screen the input video", '
                '"parameters": {"video_path": "/app/data/green14/a.mp4", '
                '"target_cluster": "vrc", '
                '"target_agent_id": "road-agent", '
                '"target_instance_id": "road-agent-1"}, '
                '"metadata": {}}'
            ),
        ],
    )

    # AgentCard 描述整个 Agent。
    # supported_interfaces 中的 url 应与部署后的实际 A2A JSON-RPC 地址一致。
    return AgentCard(
        name="Vehicle Accident Prefilter Agent",
        description=(
            "车端事故初筛 Agent：MobileNetV3-Small 扫描事故概率，"
            "导出采样帧并通过 NATS 发送给路端。"
        ),
        version="1.2.0",
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
app = FastAPI(title="Vehicle Accident Prefilter Agent", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())

# 构建一次 Agent Card，并复用于 agent-card 路由和 JSON-RPC handler。
_agent_card = _build_agent_card()

# DefaultRequestHandler 是 a2a-python 的默认 JSON-RPC 请求处理器。
# - agent_executor: 真正执行任务的对象
# - task_store: 示例中使用内存存储，服务重启后任务状态不会保留
# - agent_card: 用于校验/描述当前 Agent 能力
_request_handler = DefaultRequestHandler(
    agent_executor=AgentTemplateExecutor(),
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
