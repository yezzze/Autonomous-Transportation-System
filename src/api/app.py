"""
FastAPI application for LangManus.
"""

import json
import logging
import os
import re
import uuid
from typing import Dict, List, Any, Optional, Union

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
import asyncio
from typing import AsyncGenerator, Dict, List, Any

from src.config import TEAM_MEMBERS
from src.service.workflow_service import run_agent_workflow

# Configure logging
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="LangManus API",
    description="API for LangManus LangGraph-based agent workflow",
    version="0.1.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# ======================================================================
# ARDC Gossip 端点（主 API 服务也支持 peer 同步）
# ======================================================================

class _RegistrySyncRequest(BaseModel):
    source_url: str
    agents: list


class _DispatchRequest(BaseModel):
    """跨主体子任务分发请求。"""
    subtask: Dict[str, Any]
    session_id: str
    source_aoe_url: str = ""


class _NatsPublishRequest(BaseModel):
    """由 AOE 代发 NATS 消息，默认投递到本 AOE 管理集群的 NATS。"""
    subject: str = Field(..., description="要发布的 NATS subject")
    payload: Dict[str, Any] = Field(default_factory=dict, description="JSON payload")
    reply_subject: Optional[str] = Field(None, description="需要等待的 reply subject")
    servers: Optional[List[str]] = Field(
        None,
        description="可选 NATS servers；默认使用当前 AOE 的 NATS_SERVERS",
    )
    stream: str = Field("WORKFLOW", description="JetStream stream 名称")
    stream_subjects: List[str] = Field(
        default_factory=lambda: ["workflow.demo.>"],
        description="stream 不存在时创建使用的 subjects",
    )
    timeout_sec: float = Field(30.0, description="等待 reply 的超时时间")


def _safe_nats_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", value)
    return token.strip("-") or uuid.uuid4().hex[:8]


class _SessionRegistry:
    """跟踪远端 AOE 发来的跨主体会话，支持运行中任务取消。"""

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        session_id: str,
        workflow_handle: str,
        task: Optional[asyncio.Task] = None,
    ):
        self._sessions[session_id] = {"handle": workflow_handle, "task": task}

    def unregister(self, session_id: str) -> Optional[str]:
        entry = self._sessions.pop(session_id, None)
        return entry["handle"] if entry else None

    def cancel_session(self, session_id: str) -> bool:
        entry = self._sessions.get(session_id)
        if not entry:
            return False
        task = entry.get("task")
        if task and not task.done():
            task.cancel()
            return True
        return False


_session_registry = _SessionRegistry()


@app.post("/registry/sync", summary="ARDC Gossip 接收 peer 推送")
async def registry_sync(req: _RegistrySyncRequest):
    """接收来自 peer 节点的 agent 列表，合并到本节点注册表"""
    from src.service.agent_registry import get_registry_client
    registry = get_registry_client()
    merged_count = registry.sync_from_peer(req.source_url, req.agents)
    return {
        "status": "ok",
        "source_url": req.source_url,
        "received_agents": len(req.agents),
        "merged_count": merged_count,
    }


@app.get("/api/registry/agents", summary="查看本集群与 peer 集群 Agent")
async def list_registry_agents():
    """返回本地 Agent、peer 同步 Agent 和去重合并后的视图。"""
    try:
        from src.service.agent_registry import get_registry_client

        registry = get_registry_client()
        return registry.get_agents_by_source()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/comm/nats/publish", summary="通过当前 AOE 向本集群 NATS 投递消息")
async def publish_nats_message(req: _NatsPublishRequest):
    """
    由 AOE 代调用本集群 NATS。

    用法示例：从集群 B 调集群 A 时，直接请求集群 A 的这个 HTTP 端点；
    端点会使用集群 A AOE 的 NATS_SERVERS，把消息投给集群 A 内的 Agent B/C。
    """
    try:
        from nats.aio.client import Client as NATS
        from nats.errors import TimeoutError as NatsTimeoutError
        from nats.js.errors import NotFoundError
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"缺少 nats-py 依赖，请安装 nats-py 后重启 AOE: {e}",
        )

    servers = req.servers or [
        item.strip()
        for item in os.getenv("NATS_SERVERS", "nats://nats:4222").split(",")
        if item.strip()
    ]
    nc = NATS()
    try:
        await nc.connect(
            servers=servers,
            connect_timeout=5,
            reconnect_time_wait=2,
            max_reconnect_attempts=3,
        )
        js = nc.jetstream()
        try:
            await js.stream_info(req.stream)
        except NotFoundError:
            await js.add_stream(name=req.stream, subjects=req.stream_subjects)

        reply_sub = None
        durable = None
        if req.reply_subject:
            durable = f"aoe-reply-{_safe_nats_token(req.reply_subject)}-{uuid.uuid4().hex[:6]}"
            reply_sub = await js.pull_subscribe(req.reply_subject, durable=durable)

        ack = await js.publish(req.subject, json.dumps(req.payload).encode())
        response: Dict[str, Any] = {
            "status": "sent",
            "servers": servers,
            "subject": req.subject,
            "stream": ack.stream,
            "seq": ack.seq,
        }

        if reply_sub:
            try:
                messages = await reply_sub.fetch(1, timeout=req.timeout_sec)
            except NatsTimeoutError:
                response["reply"] = None
                response["reply_status"] = "timeout"
            else:
                raw = messages[0]
                await raw.ack()
                response["reply_status"] = "received"
                response["reply_subject"] = raw.subject
                response["reply"] = json.loads(raw.data.decode())
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[NATS Publish] failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if nc.is_connected:
            await nc.drain()


@app.post("/orchestration/dispatch", summary="跨 AOE 子任务分发接收端")
async def orchestration_dispatch(req: _DispatchRequest):
    """
    接收其他 Kubernetes 集群 AOE 发来的子任务，在本集群 AOE 内执行。

    这是跨多个 K8S 集群时的 E_AOE 入口；调用方通常来自
    src.graph.distributed_nodes.dispatch_subtask_to_remote_aoe()。
    """
    import uuid
    from src.distributed_workflow import run_distributed_workflow

    task_id = req.subtask.get("task_id", uuid.uuid4().hex[:8])
    task_desc = req.subtask.get("task_description", "")
    timeout = int(req.subtask.get("timeout_seconds", 60))
    workflow_handle = f"remote_wf_{task_id}_{uuid.uuid4().hex[:6]}"

    logger.info(
        "[E_AOE] 收到跨集群子任务: task_id=%s, source=%s, desc=%s",
        task_id,
        req.source_aoe_url,
        task_desc[:120],
    )

    workflow_task = asyncio.create_task(
        run_distributed_workflow(
            user_input=task_desc,
            adaptive_mode=True,
            timeout_seconds=timeout,
        )
    )
    _session_registry.register(req.session_id, workflow_handle, task=workflow_task)

    try:
        result = await asyncio.wait_for(
            asyncio.shield(workflow_task),
            timeout=float(timeout),
        )
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "completed",
            "result": str(result)[:3000],
        }
    except asyncio.TimeoutError:
        workflow_task.cancel()
        logger.warning("[E_AOE] 子任务超时: task_id=%s", task_id)
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "timeout",
            "result": f"子任务执行超时（{timeout}s）",
        }
    except asyncio.CancelledError:
        logger.info("[E_AOE] 子任务被取消: task_id=%s", task_id)
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "cancelled",
            "result": "子任务已被终止",
        }
    except Exception as e:
        logger.error("[E_AOE] 子任务失败: task_id=%s, error=%s", task_id, e)
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "error",
            "result": f"子任务执行失败: {str(e)}",
        }


@app.delete("/orchestration/session/{session_id}", summary="关闭跨 AOE 会话")
async def close_orchestration_session(session_id: str):
    """取消或清理远端 AOE 创建的跨主体会话。"""
    cancelled = _session_registry.cancel_session(session_id)
    if cancelled:
        logger.info("[E_AOE] 工作流 Task 已取消: session_id=%s", session_id)

    workflow_handle = _session_registry.unregister(session_id)
    if not workflow_handle:
        return {"status": "not_found", "session_id": session_id}

    try:
        from src.runtime.lifecycle_manager import get_lifecycle_manager

        alcm = get_lifecycle_manager()
        for instance_id in list(alcm._instances.keys()):
            instance = alcm._instances.get(instance_id)
            if instance and workflow_handle in getattr(instance, "subscribers", []):
                alcm.unsubscribe(instance_id, workflow_handle)
    except Exception as e:
        logger.warning("[E_AOE] ALCM 退订失败（非关键）: %s", e)

    return {
        "status": "closed",
        "session_id": session_id,
        "workflow_handle": workflow_handle,
    }


@app.on_event("startup")
async def _start_gossip():
    """
    应用启动时，若配置了 PEER_AOE_URLS 环境变量则启动 gossip 后台任务。
    PEER_AOE_URLS 格式：逗号分隔的 HTTP URL，如
        PEER_AOE_URLS=http://192.168.1.20:8000,http://192.168.1.21:8000
    LOCAL_AOE_URL 指定本节点地址（默认 http://localhost:8000）。
    """
    import os
    from src.service.agent_registry import get_registry_client

    peer_urls_raw = os.getenv("PEER_AOE_URLS", "")
    peer_urls = [u.strip() for u in peer_urls_raw.split(",") if u.strip()]
    local_url = os.getenv("LOCAL_AOE_URL", "http://localhost:8000")
    interval = int(os.getenv("GOSSIP_INTERVAL", "30"))

    registry = get_registry_client()
    await registry.start_gossip_background(
        peer_urls=peer_urls,
        local_url=local_url,
        interval=interval,
    )


class ContentItem(BaseModel):
    type: str = Field(..., description="The type of content (text, image, etc.)")
    text: Optional[str] = Field(None, description="The text content if type is 'text'")
    image_url: Optional[str] = Field(
        None, description="The image URL if type is 'image'"
    )


class ChatMessage(BaseModel):
    role: str = Field(
        ..., description="The role of the message sender (user or assistant)"
    )
    content: Union[str, List[ContentItem]] = Field(
        ...,
        description="The content of the message, either a string or a list of content items",
    )


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., description="The conversation history")
    debug: Optional[bool] = Field(False, description="Whether to enable debug logging")
    deep_thinking_mode: Optional[bool] = Field(
        False, description="Whether to enable deep thinking mode"
    )
    search_before_planning: Optional[bool] = Field(
        False, description="Whether to search before planning"
    )


@app.post("/api/chat/stream")
async def chat_endpoint(request: ChatRequest, req: Request):
    """
    Chat endpoint for LangGraph invoke.

    Args:
        request: The chat request
        req: The FastAPI request object for connection state checking

    Returns:
        The streamed response
    """
    try:
        # Convert Pydantic models to dictionaries and normalize content format
        messages = []
        for msg in request.messages:
            message_dict = {"role": msg.role}

            # Handle both string content and list of content items
            if isinstance(msg.content, str):
                message_dict["content"] = msg.content
            else:
                # For content as a list, convert to the format expected by the workflow
                content_items = []
                for item in msg.content:
                    if item.type == "text" and item.text:
                        content_items.append({"type": "text", "text": item.text})
                    elif item.type == "image" and item.image_url:
                        content_items.append(
                            {"type": "image", "image_url": item.image_url}
                        )

                message_dict["content"] = content_items

            messages.append(message_dict)

        async def event_generator():
            try:
                async for event in run_agent_workflow(
                    messages,
                    request.debug,
                    request.deep_thinking_mode,
                    request.search_before_planning,
                ):
                    # Check if client is still connected
                    if await req.is_disconnected():
                        logger.info("Client disconnected, stopping workflow")
                        break
                    yield {
                        "event": event["event"],
                        "data": json.dumps(event["data"], ensure_ascii=False),
                    }
            except asyncio.CancelledError:
                logger.info("Stream processing cancelled")
                raise

        return EventSourceResponse(
            event_generator(),
            media_type="text/event-stream",
            sep="\n",
        )
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================================
# 应用管理层 API (APPM / AW / ALRE / DISP)
# ======================================================================

class InstallAppRequest(BaseModel):
    name: str = Field(..., description="应用名称")
    task_description: str = Field(..., description="任务总体描述（传递给编排引擎）")
    orchestration_mode: str = Field("adaptive", description="编排模式: adaptive|sequential|magentic")
    agents_required: List[str] = Field(default_factory=list, description="所需 Agent 能力列表")
    constraints: Dict[str, Any] = Field(default_factory=dict, description="约束条件（如 max_rounds, timeout_seconds）")
    images: List[Dict[str, Any]] = Field(default_factory=list, description="可选 Agent 镜像清单")
    skills_md: Optional[str] = Field(None, description="Skills.md 内容：应用专属技能指引")


class InstallAppResponse(BaseModel):
    app_id: str
    name: str
    status: str
    message: str


class ResourceConfigRequest(BaseModel):
    cpu_cores: Optional[float] = Field(None, gt=0, description="CPU 核心数")
    memory_mb: Optional[int] = Field(None, gt=0, description="内存 MB")
    node_id: Optional[str] = Field(None, description="目标节点 ID 或 kubernetes nodeSelector")
    gpu_count: Optional[int] = Field(None, ge=0, description="GPU 数量")


class StartAppRequest(BaseModel):
    resource_config: Optional[ResourceConfigRequest] = None


class ScaleDeploymentRequest(BaseModel):
    replicas: Optional[int] = Field(None, ge=1, description="目标副本数")
    cpu_cores: Optional[float] = Field(None, gt=0, description="新的 CPU 核心数")
    memory_mb: Optional[int] = Field(None, gt=0, description="新的内存 MB")
    gpu_count: Optional[int] = Field(None, ge=0, description="新的 GPU 数量")


@app.get("/api/apps/", summary="获取所有应用列表")
async def list_apps():
    """
    DISP: 获取所有应用列表（含所有状态）
    对应接口文档 §4 交互呈现
    """
    try:
        from src.app.display import get_all_app_list
        return {"apps": get_all_app_list()}
    except Exception as e:
        logger.error(f"list_apps error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/apps/running", summary="获取运行中的应用列表")
async def list_running_apps():
    """
    DISP: 获取当前运行中的应用，返回应用接口信息
    对应接口文档 §4 交互呈现
    """
    try:
        from src.app.display import get_running_app_list
        return {"apps": get_running_app_list()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/apps/{app_id}", summary="获取单个应用信息")
async def get_app(app_id: str):
    """
    DISP: 获取应用详情和对外接口信息
    """
    try:
        from src.app.display import get_app_interface
        info = get_app_interface(app_id)
        if not info:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")
        return info
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/apps/install", response_model=InstallAppResponse, summary="安装应用")
async def install_app(request: InstallAppRequest):
    """
    APPM: 安装应用（存储指导文件 + 可选安装 Agent 镜像）
    对应接口文档 §1 安装/卸载应用
    """
    try:
        from src.app.app_manager import get_app_manager
        from src.app.models import AgentImage, GuidanceFile
        import uuid

        app_id = f"app_{uuid.uuid4().hex[:8]}"
        guidance = GuidanceFile(
            app_id=app_id,
            task_description=request.task_description,
            agents_required=request.agents_required,
            orchestration_mode=request.orchestration_mode,
            constraints=request.constraints,
            skills_content=request.skills_md,
        )
        manager = get_app_manager()
        images = []
        for item in request.images:
            images.append(
                AgentImage(
                    image_id=item["image_id"],
                    name=item.get("name") or item["image_id"].split(":")[0],
                    version=item.get("version", "latest"),
                    capability=item.get("capability") or item.get("name") or item["image_id"].split(":")[0],
                    description=item.get("description", ""),
                    exposed_external=item.get("exposed_external", False),
                    metadata=item.get("metadata", {}),
                    registered=item.get("registered", False),
                )
            )

        app_info = manager.install(name=request.name, guidance_file=guidance, images=images)

        return InstallAppResponse(
            app_id=app_info.app_id,
            name=app_info.name,
            status=app_info.status,
            message="安装成功",
        )
    except Exception as e:
        logger.error(f"install_app error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/apps/{app_id}/start", summary="启动应用")
async def start_app(app_id: str, request: Optional[StartAppRequest] = None):
    """
    APPM: 启动应用，触发编排层工作流
    对应接口文档 §2 启动应用
    """
    try:
        from src.app.app_manager import get_app_manager
        from src.runtime.models import ResourceConfig

        manager = get_app_manager()
        resource_config = None
        if request and request.resource_config:
            resource_config = ResourceConfig(**request.resource_config.model_dump())

        handle = await manager.start(app_id, resource_config=resource_config)

        if handle is None:
            app = manager.get_app(app_id)
            if not app:
                raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")
            raise HTTPException(
                status_code=500,
                detail=f"启动失败: {app.error_message or '未知错误'}",
            )

        return {
            "app_id": app_id,
            "workflow_handle": handle,
            "status": "running",
            "resource_config": resource_config.to_dict() if resource_config else None,
            "app_interface_url": f"/api/apps/{app_id}/interface",
            "message": "启动成功",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"start_app error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/apps/{app_id}/stop", summary="停止应用")
async def stop_app(app_id: str):
    """
    APPM: 停止应用，通知编排层停止工作流
    对应接口文档 §3 停止应用
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        success = await manager.stop(app_id)

        if not success:
            app = manager.get_app(app_id)
            if not app:
                raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        return {"app_id": app_id, "status": "stopped", "message": "停止成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/apps/{app_id}", summary="卸载应用")
async def uninstall_app(app_id: str):
    """
    APPM: 卸载应用，删除指导文件和镜像注册
    对应接口文档 §1 安装/卸载应用
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        success = manager.uninstall(app_id)

        if not success:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        return {"app_id": app_id, "message": "卸载成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class UpdateAppRequest(BaseModel):
    name: Optional[str] = Field(None, description="新应用名称")
    task_description: Optional[str] = Field(None, description="新任务描述")
    skills_md: Optional[str] = Field(None, description="新 Skills.md 内容")
    orchestration_mode: Optional[str] = Field(None, description="新编排模式")
    constraints: Optional[Dict[str, Any]] = Field(None, description="要合并的约束条件")


@app.patch("/api/apps/{app_id}", summary="更新应用配置")
async def update_app(app_id: str, request: UpdateAppRequest):
    """
    APPM: 更新应用配置（无需重新安装）
    支持更新名称、任务描述、Skills.md、编排模式、约束条件。
    对正在运行的应用修改配置不会立即生效，需重启应用。
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        app = manager.update(
            app_id=app_id,
            name=request.name,
            task_description=request.task_description,
            skills_content=request.skills_md,
            orchestration_mode=request.orchestration_mode,
            constraints=request.constraints,
        )
        if app is None:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        return {"app_id": app_id, "status": app.status, "message": "更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_app error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================================
# 运行层 API (ALCM / QoS)
# ======================================================================

@app.get("/api/agents/instances", summary="获取 Agent 实例列表")
async def list_agent_instances():
    """
    ALCM: 获取运行层所有 Agent 实例
    """
    try:
        from src.runtime.lifecycle_manager import get_lifecycle_manager

        manager = get_lifecycle_manager()
        instances = manager.list_instances()
        return {"instances": [inst.to_dict() for inst in instances]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/instances/{instance_id}", summary="获取单个 Agent 实例")
async def get_agent_instance(instance_id: str):
    """ALCM: 查询单个 Agent 实例详情"""
    try:
        from src.runtime.lifecycle_manager import get_lifecycle_manager

        manager = get_lifecycle_manager()
        inst = manager.get_instance(instance_id)
        if not inst:
            raise HTTPException(status_code=404, detail=f"实例 {instance_id} 不存在")
        return inst.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/qos/metrics", summary="获取所有 Agent QoS 指标")
async def get_qos_metrics():
    """
    QoS Monitor: 获取所有 Agent 的调用质量统计
    供资源 Agent 读取，用于通信/计算资源调度决策
    """
    try:
        from src.runtime.qos_monitor import get_qos_monitor

        monitor = get_qos_monitor()
        return {
            "summary": monitor.get_summary(),
            "metrics": monitor.get_metrics_dict(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/qos/metrics/{agent_id}", summary="获取单个 Agent QoS 指标")
async def get_agent_qos_metrics(agent_id: str):
    """QoS Monitor: 查询单个 Agent 的 QoS 指标"""
    try:
        from src.runtime.qos_monitor import get_qos_monitor

        monitor = get_qos_monitor()
        m = monitor.get_metrics(agent_id)
        if not m:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} 暂无 QoS 数据")
        return m.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================================
# 资源层 API (RRDC / ASD)
# ======================================================================

@app.get("/api/resources/", summary="查询可用资源")
async def query_resources(
    min_cpu: float = 0.5,
    min_mem_mb: int = 256,
    node_type: Optional[str] = None,
):
    """
    RRDC: 查询满足条件的节点资源列表
    """
    try:
        from src.service.resource_registry import get_resource_registry

        registry = get_resource_registry()
        nodes = registry.query_available_resources(
            min_cpu=min_cpu,
            min_mem_mb=min_mem_mb,
            node_type=node_type,
        )
        return {
            "summary": registry.get_summary(),
            "nodes": [n.to_dict() for n in nodes],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AppQueryRequest(BaseModel):
    query: str = Field(..., description="用户输入的查询内容")


@app.post("/api/apps/{app_id}/interface", summary="向应用发送查询")
async def query_app_interface(app_id: str, request: AppQueryRequest):
    """
    DISP: 应用运行时交互接口
    接收用户输入，使用应用配置的编排模式执行一次工作流，返回结果。
    对应接口文档 §4 交互呈现
    """
    try:
        from src.app.app_logic_engine import get_app_logic_engine

        engine = get_app_logic_engine()
        result = await engine.run_query(app_id=app_id, user_input=request.query)

        # 序列化 result 内部的 LangChain 消息对象，避免 JSON 序列化失败
        if isinstance(result.get("result"), dict):
            raw_state = result["result"]
            messages = raw_state.get("messages", [])
            serialized_msgs = []
            for m in messages:
                if hasattr(m, "type") and hasattr(m, "content"):
                    # LangChain BaseMessage
                    serialized_msgs.append({"type": m.type, "content": m.content})
                elif isinstance(m, dict):
                    serialized_msgs.append(m)
                else:
                    serialized_msgs.append({"type": "unknown", "content": str(m)})
            result["result"] = {
                **{k: v for k, v in raw_state.items() if k != "messages"},
                "messages": serialized_msgs,
            }

        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"query_app_interface error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/apps/{app_id}/interface", summary="获取应用接口说明")
async def get_app_interface_info(app_id: str):
    """
    返回应用的接口说明，包括如何调用 POST /interface 发送查询
    """
    try:
        from src.app.display import get_app_interface
        info = get_app_interface(app_id)
        if not info:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")
        info["usage"] = {
            "method": "POST",
            "url": f"/api/apps/{app_id}/interface",
            "body": {"query": "你的问题"},
            "description": "向该应用发送查询，使用应用配置的编排模式执行工作流",
        }
        return info
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/deployments", summary="获取 Agent 部署记录")
async def list_agent_deployments():
    """
    ASD: 获取所有运行中的 Agent 部署记录
    """
    try:
        from src.service.agent_scheduler import get_agent_scheduler

        scheduler = get_agent_scheduler()
        running = scheduler.get_running_agents()
        return {"deployments": [r.to_dict() for r in running]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/warehouse/images", summary="获取 Agent 镜像仓库列表")
async def list_warehouse_images():
    """
    AW: 获取当前仓库中的镜像列表，供 UI 安装应用时选择能力与镜像。
    """
    try:
        from src.app.agent_warehouse import get_agent_warehouse

        warehouse = get_agent_warehouse()
        images = warehouse.list_images()
        return {"images": [img.to_dict() for img in images]}
    except Exception as e:
        logger.error(f"list_warehouse_images error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/agents/deployments/{deployment_id}/scale", summary="扩缩容 Agent 部署")
async def scale_agent_deployment(deployment_id: str, request: ScaleDeploymentRequest):
    """
    ASD: 更新部署副本数和资源配置。
    Kubernetes 后端会 patch Deployment.spec.replicas 与容器 resources。
    """
    try:
        from src.service.agent_scheduler import get_agent_scheduler

        scheduler = get_agent_scheduler()
        record = scheduler.scale_deployment(
            deployment_id=deployment_id,
            replicas=request.replicas,
            cpu_cores=request.cpu_cores,
            memory_mb=request.memory_mb,
            gpu_count=request.gpu_count,
        )
        if record is None:
            raise HTTPException(status_code=404, detail=f"部署 {deployment_id} 不存在")
        return {"deployment": record.to_dict(), "message": "扩缩容成功"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"scale_agent_deployment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================================
# Web UI
# ======================================================================

@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def web_ui():
    """应用管理层 Web UI 控制台"""
    from src.api.ui import get_ui_html
    return HTMLResponse(content=get_ui_html())


# ======================================================================
# Demo: 车辆协同感知 — 可视化演示（DEMO_MODE=1 时激活事件总线）
# ======================================================================

class _DemoStartRequest(BaseModel):
    task_description: str


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo_page():
    """车辆协同感知演示可视化页面"""
    from src.api.demo_ui import get_demo_html
    return HTMLResponse(content=get_demo_html())


@app.websocket("/demo/ws")
async def demo_ws(ws: WebSocket):
    """
    Demo WebSocket 端点 — 订阅 DemoEventBus，实时推送结构化事件到前端。
    每个浏览器连接对应一个独立的订阅队列（fan-out 广播）。
    """
    from src.service.demo_bus import get_demo_bus
    await ws.accept()
    try:
        async for event in get_demo_bus().subscribe():
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[Demo WS] 连接关闭: {e}")


@app.post("/demo/start", summary="启动协同感知演示工作流")
async def demo_start(request: _DemoStartRequest):
    """
    启动 app_vehicle_demo 工作流（后台异步执行）。
    Demo 事件通过 DemoEventBus 推送到 /demo/ws WebSocket 连接。
    """
    import uuid
    wf_id = f"demo_{uuid.uuid4().hex[:8]}"
    asyncio.create_task(_run_demo_workflow(request.task_description, wf_id))
    from src.service.demo_bus import get_demo_bus
    get_demo_bus().publish("log", {"level": "info", "message": f"工作流 {wf_id} 开始执行"})
    return {"status": "accepted", "workflow_id": wf_id}


async def _run_demo_workflow(task_desc: str, wf_id: str) -> None:
    """后台任务：运行 app_vehicle_demo 工作流并在完成时推送 demo:task_complete 事件"""
    from src.service.demo_bus import get_demo_bus
    bus = get_demo_bus()
    try:
        from src.app.app_logic_engine import get_app_logic_engine
        engine = get_app_logic_engine()
        result = await engine.run_query(app_id="app_vehicle_demo", user_input=task_desc)
        # 提取最终消息内容
        raw_state = result.get("result", {}) or {}
        messages = raw_state.get("messages", []) if isinstance(raw_state, dict) else []
        final_result = ""
        for msg in reversed(messages):
            content = (
                msg.content if hasattr(msg, "content")
                else msg.get("content") if isinstance(msg, dict)
                else None
            )
            if content and len(str(content)) > 50:
                final_result = str(content)[:1500]
                break
        bus.publish("demo:task_complete", {"result": final_result})
    except Exception as e:
        logger.error(f"[Demo] 工作流执行失败: {e}")
        bus.publish("log", {"level": "error", "message": f"工作流执行失败: {e}"})
        bus.publish("demo:task_complete", {"result": f"执行出错: {e}"})


@app.post("/demo/toggle-vehicleB-failure", summary="切换VehicleB故障模拟")
async def demo_toggle_vehicleB():
    """
    切换 VehicleB 感知节点故障模拟状态：
    - 开启时：下次执行 perception_vehicleB 任务时注入失败，触发 §2.3 failover 切换到 VehicleC
    - 关闭时：恢复正常执行
    """
    from src.service.demo_bus import set_vehicleB_failed, is_vehicleB_failed, get_demo_bus
    new_state = not is_vehicleB_failed()
    set_vehicleB_failed(new_state)
    get_demo_bus().publish("demo:status", {"vehicleB_failed": new_state})
    return {"vehicleB_failed": new_state, "message": f"VehicleB 故障模拟: {'开启' if new_state else '关闭'}"}
