"""
FastAPI 应用主体 — Agent Template 的核心服务

架构概览:
  外部调用方 ──A2A JSON-RPC──> / ──> a2a-python RequestHandler
                                          │
                                          ├─ AgentTemplateExecutor 解析标准 A2A Message
                                          │
                                          ├─ 从 message 文本中的 metadata 或环境变量获取 NATS 主题
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
                                          └─ 通过 A2A Task artifact 返回执行结果

标准 A2A 入口:
  GET  /.well-known/agent-card.json
  POST /

环境变量:
  A2A_AGENT_URL    : Agent Card 中声明的服务地址，默认 http://localhost:9001
  NATS_SERVER_URL  : NATS 服务器地址，默认 nats://nats:4222
  NATS_IN_SUBJECT  : 输入主题（接收上游 Agent 的数据）
  NATS_IN_DURABLE  : 输入持久化消费者名称
  NATS_OUT_SUBJECT : 输出主题（向下游 Agent 发送数据）

开发者指南:
  1. 在 agent_function() 中的 "模拟处理时间" 位置替换为你的业务逻辑
  2. 在 lifespan() 中的 "模型加载" 位置替换为你的模型初始化代码
  3. 通过标准 A2A message 文本中的 JSON metadata 覆盖 NATS 主题，或使用环境变量默认值
"""

# 标准库依赖:
# - asyncio: 模拟异步初始化/处理，也用于承载真实的异步模型推理或 IO
# - json: 解析 A2A 文本消息中的 JSON payload，并序列化返回 artifact
# - os: 从环境变量读取部署时注入的配置
# - asynccontextmanager: 以 async with 风格声明 FastAPI 启停生命周期
# - Any: 为 JSON 序列化兜底函数提供宽泛类型标注
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

# a2a-python 工具函数和类型:
# 这些封装负责把 FastAPI 请求转换成标准 A2A task / message / artifact 事件。
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

# 项目内依赖:
# - NatsComm: 对 NATS JetStream 的发送、接收和关闭连接做统一封装
# - logger: 项目统一日志格式
# - numpy_utils: 在 JSON/NATS 传输中编码和还原 numpy.ndarray
from protocols import NatsComm
from utils.logger_utils import get_logger
from utils.numpy_utils import decode_structured_numpy, encode_structured_numpy


logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────
# A2A 与 NATS 连接配置（可通过环境变量覆盖）
# ──────────────────────────────────────────────────────────────────────

# Agent Card 中暴露给调用方的访问地址。
# 部署到不同环境时通常需要覆盖为网关地址、Pod Service 地址或公网地址。
A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:9001")

# NATS 服务器地址，容器环境中通常由编排系统注入。
# 默认值 nats://nats:4222 假设 FastAPI 服务和 NATS 运行在同一容器网络内。
NATS_SERVER_URL = os.getenv("NATS_SERVER_URL", "nats://nats:4222")

# 输入主题: 本 Agent 从此主题拉取上游 Agent 的处理结果。
# 如果工作流编排器没有在 A2A metadata 中传入主题，则使用此默认值。
NATS_IN_SUBJECT = os.getenv("NATS_IN_SUBJECT", "workflow.previousagent.result")

# 输入持久化消费者名称: JetStream pull_subscribe 的 durable name
# 确保消息不丢失，消费者重启后可以从上次位置继续消费
# 注意: 如果多个 Agent 实例共用同一个 durable，它们会共享消费进度。
NATS_IN_DURABLE = os.getenv("NATS_IN_DURABLE", "workflow-previousagent-result")

# 输出主题: 本 Agent 将处理结果发布到此主题，供下游 Agent 消费。
# 下游 Agent 的 NATS_IN_SUBJECT 通常应指向这个值。
NATS_OUT_SUBJECT = os.getenv("NATS_OUT_SUBJECT", "workflow.agenttemplate.result")

logger.info("A2A Agent URL initialized as: %s", A2A_AGENT_URL)
logger.info("NATS communication initialized with server: %s", NATS_SERVER_URL)

# 全局 NATS 通信实例，所有 NATS 操作复用此连接。
# 这样可以避免每次请求都重新初始化连接对象；关闭动作集中在 helper 和 lifespan 中。
_nats_comm = NatsComm(servers=[NATS_SERVER_URL])


# ──────────────────────────────────────────────────────────────────────
# NATS 数据收发辅助函数
# ──────────────────────────────────────────────────────────────────────

async def _receive_data_from_nats(
    nats_in_subject: str = NATS_IN_SUBJECT,
    nats_in_durable: str = NATS_IN_DURABLE,
) -> dict:
    """
    从 NATS JetStream 拉取消息。

    参数:
      nats_in_subject:
        要消费的 NATS subject。默认读取环境变量配置，A2A metadata 可覆盖。
      nats_in_durable:
        JetStream durable consumer 名称。相同 durable 会保留消费进度。

    返回:
      反序列化后的 message.payload，约定为 dict。

    工作流程:
      1. 通过 pull_subscribe 订阅指定主题和持久化消费者
      2. 批量拉取消息（默认 1 条，超时 5 秒）
      3. 收到消息后确认 (ack) 并返回 payload
      4. 未取到消息或发生异常时记录日志，并通过 HTTPException 向上层暴露错误
      5. finally 块中关闭 NATS 连接，避免连接泄漏

    维护提示:
      当前模板一次只拉取 1 条消息。如果业务需要批处理，可以调整 batch，
      同时将返回值从单条 payload 改为列表，并同步修改 agent_function()。
    """
    try:
        # NatsComm.receive() 是项目封装的 pull 模式消费接口。
        # batch=1 表示本次请求只消费一条上游结果；timeout_sec=5 避免请求无限等待。
        messages = await _nats_comm.receive(
            subject=nats_in_subject,
            durable=nats_in_durable,
            batch=1,
            timeout_sec=5,
        )
        for message in messages:
            # ack() 必须在业务确认“这条消息已经交给当前 Agent 处理”后调用。
            # 这里在返回 payload 前立即 ack，适合模板和轻量处理；如果业务处理失败需要重投，
            # 可以把 ack 移到 agent_function() 成功处理之后。
            logger.info("Received message on subject '%s'", nats_in_subject)
            await message.ack()
            return message.payload
        raise HTTPException(
            status_code=504,
            detail=f"No messages received on subject '{nats_in_subject}' within timeout",
        )
    except Exception as exc:
        logger.exception("Error receiving message from NATS subject '%s'", nats_in_subject)
        raise HTTPException(status_code=500, detail=f"Failed to receive message: {exc}") from exc
    finally:
        await _nats_comm.close()


async def _send_data_to_nats(data: dict, nats_out_subject: str = NATS_OUT_SUBJECT) -> None:
    """
    将数据发布到 NATS JetStream 输出主题。

    参数:
      data:
        要发给下游 Agent 的结果字典。若包含 numpy.ndarray，请先调用
        encode_structured_numpy()，否则普通 JSON 传输无法保留 dtype/shape。
      nats_out_subject:
        输出 subject。默认来自环境变量，A2A metadata 可覆盖。
    """
    # send() 返回的 ack 可用于确认 JetStream 已接收消息，日志中保留它方便排障。
    ack = await _nats_comm.send(
        subject=nats_out_subject,
        payload=data,
    )
    logger.info("Data sent to NATS subject '%s' with ack: %s", nats_out_subject, ack)


def _parse_a2a_text_payload(text: str) -> tuple[str, dict]:
    """
    解析标准 A2A text message。

    支持普通文本，或形如:
    {"task_description": "...", "metadata": {"nats_in_subject": "..."}}
    的 JSON 文本。

    返回:
      (task_description, metadata)

    设计目的:
      A2A 的 text/plain 输入足够通用，但模板还需要携带工作流运行时配置。
      因此这里约定: 普通文本作为任务描述；JSON 文本中 metadata 用于覆盖 NATS 配置。
    """
    if not text:
        # 空消息仍然返回稳定的 tuple，避免调用方额外处理 None。
        return "", {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # 不是 JSON 时，把整段文本当作任务描述；metadata 为空，后续使用环境变量默认值。
        return text, {}

    if not isinstance(payload, dict):
        # JSON 数组、字符串、数字等都不符合模板约定，同样退回普通文本模式。
        return text, {}

    # metadata 只接受 dict。这样可以避免调用方传入字符串/列表后在配置解析阶段报错。
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

    # 为了兼容不同调用方的字段命名，任务描述允许多个别名。
    # 如果都不存在，保留原始 text，便于日志中看到完整输入。
    task_description = (
        payload.get("task_description")
        or payload.get("description")
        or payload.get("message")
        or text
    )
    return str(task_description), metadata


def _resolve_nats_config(metadata: dict) -> tuple[str, str, str]:
    """
    从 A2A message metadata 或环境变量解析 NATS subject 配置。

    支持的 metadata 字段:
      nats_in_subject:
        本次请求要读取的输入 subject。
      nats_in_durable:
        本次请求使用的 durable consumer 名称。
      nats_out_subject:
        本次请求要发布的输出 subject。

    优先级:
      A2A metadata > 环境变量默认值。

    durable 推导规则:
      如果调用方覆盖了 nats_in_subject 但没有显式提供 nats_in_durable，
      模板会把 subject 中的 "." 替换为 "-" 作为 durable，避免不同输入主题
      默认共用同一个消费进度。
    """
    nats_in_subject = metadata.get("nats_in_subject") or NATS_IN_SUBJECT
    nats_in_durable = metadata.get("nats_in_durable")
    if not nats_in_durable:
        nats_in_durable = (
            nats_in_subject.replace(".", "-")
            if metadata.get("nats_in_subject")
            else NATS_IN_DURABLE
        )
    nats_out_subject = metadata.get("nats_out_subject") or NATS_OUT_SUBJECT
    return nats_in_subject, nats_in_durable, nats_out_subject


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
    try:
        # TODO: 替换为实际的模型加载代码
        # 例如: model = MyModel.load("path/to/model")
        # 模板中用 sleep 模拟耗时初始化，提醒开发者这里允许执行异步 IO。
        await asyncio.sleep(1)
        logger.info("Model loaded successfully during startup")
    except Exception as exc:
        logger.exception("Failed to load model during startup")
        raise RuntimeError(f"Startup model loading failed: {exc}") from exc

    try:
        yield
    finally:
        # 关闭阶段统一释放 NATS 连接。即使请求级 helper 已经关闭过连接，
        # close() 也应保持幂等，确保服务退出时不会遗留网络资源。
        await _nats_comm.close()


# ──────────────────────────────────────────────────────────────────────
# 核心 Agent 业务逻辑
# ──────────────────────────────────────────────────────────────────────

async def agent_function(
    nats_in_subject: str = NATS_IN_SUBJECT,
    nats_in_durable: str = NATS_IN_DURABLE,
    nats_out_subject: str = NATS_OUT_SUBJECT,
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
      nats_in_subject / nats_in_durable / nats_out_subject:
        本次任务使用的 NATS 配置。通常由 A2A metadata 解析得到；
        未指定时使用模块顶部的环境变量默认值。

    返回:
      A2A artifact 中展示的轻量执行结果。真正传给下游 Agent 的业务数据
      已通过 _send_data_to_nats() 发布到 NATS。
    """
    # 1. 读取上游 Agent 的输出。模板默认要求上游把结果发布到 nats_in_subject。
    data = await _receive_data_from_nats(
        nats_in_subject=nats_in_subject,
        nats_in_durable=nats_in_durable,
    )

    # 2. 还原 numpy 结构。
    # NATS payload 通常是 JSON 友好的 dict，无法直接承载 ndarray；
    # decode_structured_numpy() 会递归识别项目约定的 numpy 编码结构并恢复为 ndarray。
    decode_data = decode_structured_numpy(data)

    # TODO: 在此处编写你的业务逻辑
    # ─── 开发者替换区: 在此处添加你的业务逻辑 ───
    # 示例: result_tensor = model.inference(decode_data)
    # 在真实 Agent 中，通常会在这里调用 lifespan() 中加载好的模型，
    # 或执行清洗、聚合、特征计算、格式转换等业务逻辑。
    await asyncio.sleep(1)

    # 3. 组织下游 payload。
    # 这里把输入原样编码后返回，表示模板 Agent 已成功处理。
    # 开发时可将 processed_data 替换成模型输出、统计结果或其他业务结构。
    result = {
        "status": "success",
        "processed_data": encode_structured_numpy(decode_data),
    }
    # ─── 开发者替换区结束 ───

    # 4. 发布给下游 Agent。A2A 响应只返回简短状态，业务数据走 NATS。
    await _send_data_to_nats(result, nats_out_subject=nats_out_subject)

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
      3. 从 A2A message 文本中解析任务描述和 NATS metadata
      4. 调用 agent_function() 执行业务逻辑
      5. 写入 artifact，并把任务标记为 COMPLETED 或 FAILED

    这个类是 A2A 层和业务层的边界:
      - A2A 协议相关的 task/status/artifact 处理放在这里
      - 具体业务和 NATS 数据处理放在 agent_function()
    """

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 如果 a2a-python 已经为当前请求创建了 task，则继续使用它；
        # 否则根据用户消息新建 task，并先发送给事件队列，让调用方能看到任务已创建。
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

        # A2A text message 是模板的唯一输入模式。
        # 普通文本用于描述任务；JSON 文本可以额外携带 NATS metadata。
        text = get_message_text(context.message)
        task_description, metadata = _parse_a2a_text_payload(text)
        nats_in_subject, nats_in_durable, nats_out_subject = _resolve_nats_config(metadata)

        logger.info(
            "Processing A2A task: description=%s, nats_in=%s, nats_out=%s",
            task_description,
            nats_in_subject,
            nats_out_subject,
        )

        try:
            # 将协议层解析出的 NATS 配置交给业务函数。
            # execute() 不直接处理业务 payload，保持 A2A 桥接层职责单一。
            result = await agent_function(
                nats_in_subject=nats_in_subject,
                nats_in_durable=nats_in_durable,
                nats_out_subject=nats_out_subject,
            )
        except Exception as exc:
            # 任意未处理异常都会转成 A2A FAILED 状态。
            # 详细堆栈写入服务日志，返回给调用方的 message 保持简短。
            logger.exception("Agent execution failed")
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(f"Request failed: {exc}"),
            )
            return

        # artifact 是 A2A task 的标准结果载体。
        # ensure_ascii=False 保留中文等非 ASCII 字符，便于调试和前端展示。
        result_text = json.dumps(result, ensure_ascii=False, default=_json_default)
        await updater.add_artifact(
            parts=[new_text_part(text=result_text, media_type="text/plain")],
            name="agent-template-result",
        )

        # 业务函数也可以通过 {"status": "..."} 表达失败。
        # 当前模板只有 success，但保留该分支方便后续扩展业务级错误。
        if result.get("status") != "success":
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("Request failed."),
            )
            return

        # 只有业务函数成功返回且 status == success，才把 A2A task 标记为完成。
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Request is completed!"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 模板暂不支持取消。
        # 如果后续业务存在长时间推理/批处理，可在这里接入取消标志、任务队列撤销等机制。
        raise NotImplementedError("Cancel is not supported.")


def _json_default(value: Any):
    """
    json.dumps(default=...) 的兜底序列化函数。

    A2A artifact 最终需要文本化。如果 result 中仍残留 numpy.ndarray
    或类似对象，tolist() 可以把它转成普通 Python list；其他未知对象退回 str()。
    """
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
        id="agent_template_nats_processor",
        name="Agent Template NATS Processor",
        description=(
            "Triggers the template Agent business logic, reads upstream data "
            "from NATS, processes it, and publishes results to NATS."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["agent-template"],
        examples=[
            "Process the input data",
            (
                '{"task_description": "Process the input data", '
                '"metadata": {"nats_in_subject": "workflow.input", '
                '"nats_out_subject": "workflow.output"}}'
            ),
        ],
    )

    # AgentCard 描述整个 Agent。
    # supported_interfaces 中的 url 应与部署后的实际 A2A JSON-RPC 地址一致。
    return AgentCard(
        name="Agent Template",
        description="FastAPI + NATS Agent Template exposed through a2a-python.",
        version="0.1.1",
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


# FastAPI 应用实例。
# lifespan 负责启动/关闭阶段的资源管理；A2A 路由随后被挂载到该应用上。
app = FastAPI(title="Agent Template API", lifespan=lifespan)

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
