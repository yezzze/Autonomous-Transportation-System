"""
FastAPI application for LangManus.
"""

import json
import logging
import os
import re
import uuid
from typing import Dict, List, Any, Optional, Union
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
# 将 prometheus_client 的 ASGI 应用挂载到主 FastAPI 服务。
from prometheus_client import make_asgi_app
from sse_starlette.sse import EventSourceResponse
import asyncio
from typing import AsyncGenerator, Dict, List, Any

from src.api.nats_cloud_edge import (
    list_edge_agents,
    maybe_start_nats_port_forward,
    nats_status,
    resolve_nats_servers,
    stop_nats_port_forward,
    ui_config_defaults,
)
from src.config import TEAM_MEMBERS
from src.service.workflow_service import run_agent_workflow
from src.service.aoe_config import (
    AOE_CLUSTER_CONFIG_PATH as _AOE_CLUSTER_CONFIG_PATH,
    default_aoe_config,
    load_aoe_config,
)
from src.api.visualization_routes import router as viz_router

# Configure logging
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="LangManus API",
    description="API for LangManus LangGraph-based agent workflow",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
# Prometheus 通过 ServiceMonitor 定期抓取此端点；实际抓取路径为 /metrics/。
app.mount("/metrics", make_asgi_app())

# Initialize Jinja2 templates (singleton, only created once at startup)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Create the graph
# graph = build_graph()

# 挂载可视化路由 (端口 8000 → /viz, /api/viz/*, /ws/viz/*)
app.include_router(viz_router)


# ======================================================================
# ARDC Gossip 端点（主 API 服务也支持 peer 同步）
# ======================================================================

class _RegistrySyncRequest(BaseModel):
    source_url: str
    agents: list
    sub_workflows: list = []  # 子工作流列表（可选，向后兼容）
    resources: list = []  # 本地 Kubernetes 节点资源快照（可选，向后兼容）


class _DispatchRequest(BaseModel):
    """跨主体子任务分发请求。"""
    subtask: Dict[str, Any]
    session_id: str
    source_aoe_url: str = ""


class _RegisterSubWorkflowRequest(BaseModel):
    """编排期：跨主体子任务图注册请求。"""
    subtask: Dict[str, Any]
    session_id: str
    source_aoe_url: str = ""


class _ExecuteSubWorkflowRequest(BaseModel):
    """运行期：按已注册 sub_workflow_id 执行请求。"""
    session_id: str
    source_aoe_url: str = ""
    timeout_seconds: int = 60


class _FinalizeSubWorkflowRequest(BaseModel):
    """编排期：写回并冻结远端子图的最终全局路由。"""
    tasks: List[Dict[str, Any]]
    route_instances: List[Dict[str, Any]]
    frozen_signature: List[List[str]]


class _AgentTestCallRequest(BaseModel):
    """测试页面发起的单次 Agent A2A 调用。"""

    instance_id: str = Field(..., min_length=1)
    task_description: str = Field(..., min_length=1)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class _OrchestrationPlanPreviewRequest(BaseModel):
    """编排测试页的只读规划请求。"""

    app_id: Optional[str] = None
    task_description: Optional[str] = None
    skills_content: Optional[str] = ""


class _NatsPublishRequest(BaseModel):
    """由编排服务代发 NATS 消息，默认投递到本集群 NATS。"""
    subject: str = Field(..., description="要发布的 NATS subject")
    payload: Dict[str, Any] = Field(default_factory=dict, description="JSON payload")
    reply_subject: Optional[str] = Field(None, description="需要等待的 reply subject")
    servers: Optional[List[str]] = Field(
        None,
        description="可选 NATS servers；默认使用 NATS_SERVERS",
    )
    stream: str = Field("WORKFLOW", description="JetStream stream 名称")
    jetstream_domain: str = Field(
        default_factory=lambda: os.getenv("NATS_JETSTREAM_DOMAIN", "hub"),
        description="JetStream domain；云边 NATS Hub 默认使用 hub",
    )
    stream_subjects: List[str] = Field(
        default_factory=lambda: ["workflow.>"],
        description="stream 不存在时创建使用的 subjects",
    )
    timeout_sec: float = Field(30.0, description="等待 reply 的超时时间")


class _NatsReceiveRequest(BaseModel):
    """从本集群 NATS/JetStream 拉取消息，用于 UI 简单验证。"""
    subject: str = Field(..., description="要接收的 NATS subject")
    servers: Optional[List[str]] = Field(
        None,
        description="可选 NATS servers；默认使用 NATS_SERVERS",
    )
    stream: str = Field("WORKFLOW", description="JetStream stream 名称")
    jetstream_domain: str = Field(
        default_factory=lambda: os.getenv("NATS_JETSTREAM_DOMAIN", "hub"),
        description="JetStream domain；云边 NATS Hub 默认使用 hub",
    )
    stream_subjects: List[str] = Field(
        default_factory=lambda: ["workflow.>"],
        description="stream 不存在时创建使用的 subjects",
    )
    durable: Optional[str] = Field(None, description="可选 durable 名称；为空则自动生成")
    batch: int = Field(1, ge=1, le=50, description="一次最多拉取消息数")
    timeout_sec: float = Field(5.0, description="等待消息的超时时间")


class _AoePeer(BaseModel):
    name: str = "peer"
    url: str


class _AoeClusterConfig(BaseModel):
    local_name: str = "cluster"
    local_aoe_url: str = ""
    default_peer_url: str = ""
    peers: List[_AoePeer] = Field(default_factory=list)
    default_timeout_seconds: int = 60


class _AoeRegistryPullRequest(BaseModel):
    target_url: Optional[str] = None


class _AoeGossipPushRequest(BaseModel):
    target_url: Optional[str] = None


class _AoeDispatchUiRequest(BaseModel):
    target_url: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    task_description: str
    timeout_seconds: Optional[int] = None


class _AoeAgentChainTestRequest(BaseModel):
    """从 UI 触发一次 Agent B -> Agent C 的最小链路验证。"""
    target_url: Optional[str] = None
    workflow_id: Optional[str] = None
    text: str = "hello aoe"
    in_subject: str = "workflow.edge-a.agent.b.in"
    reply_subject: Optional[str] = None
    timeout_seconds: Optional[int] = None


def _safe_nats_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", value)
    return token.strip("-") or uuid.uuid4().hex[:8]


def _nats_subject_matches(pattern: str, subject: str) -> bool:
    pattern_tokens = pattern.split(".")
    subject_tokens = subject.split(".")

    for idx, token in enumerate(pattern_tokens):
        if token == ">":
            return idx == len(pattern_tokens) - 1
        if idx >= len(subject_tokens):
            return False
        if token != "*" and token != subject_tokens[idx]:
            return False
    return len(pattern_tokens) == len(subject_tokens)


def _covered_by_subjects(subject: Optional[str], patterns: List[str]) -> bool:
    if not subject:
        return True
    return any(_nats_subject_matches(pattern, subject) for pattern in patterns)


def _required_stream_subjects(req: _NatsPublishRequest) -> List[str]:
    subjects = [item.strip() for item in req.stream_subjects if item.strip()]
    for subject in [req.subject, req.reply_subject]:
        if subject and not _covered_by_subjects(subject, subjects):
            subjects.append(subject)
    return subjects or ["workflow.>"]


async def _ensure_jetstream_stream(js, req: _NatsPublishRequest) -> Dict[str, Any]:
    required_subjects = _required_stream_subjects(req)
    try:
        info = await js.stream_info(req.stream)
    except Exception as exc:
        if exc.__class__.__name__ != "NotFoundError":
            raise
        await js.add_stream(name=req.stream, subjects=required_subjects)
        return {"created": True, "subjects": required_subjects}

    config = getattr(info, "config", None)
    current_subjects = list(getattr(config, "subjects", None) or [])
    if not current_subjects:
        current_subjects = required_subjects

    missing = [
        subject
        for subject in [req.subject, req.reply_subject]
        if subject and not _covered_by_subjects(subject, current_subjects)
    ]
    if not missing:
        return {"created": False, "subjects": current_subjects}

    merged_subjects = current_subjects[:]
    for subject in required_subjects:
        if subject not in merged_subjects:
            merged_subjects.append(subject)

    try:
        await js.update_stream(name=req.stream, subjects=merged_subjects)
    except TypeError:
        from nats.js.api import StreamConfig

        await js.update_stream(StreamConfig(name=req.stream, subjects=merged_subjects))
    return {"created": False, "updated": True, "subjects": merged_subjects, "added_subjects": missing}


AOE_CLUSTER_CONFIG_PATH = str(_AOE_CLUSTER_CONFIG_PATH)


def _clean_aoe_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        raise HTTPException(status_code=400, detail="AOE URL 不能为空")
    if not cleaned.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"AOE URL 必须以 http:// 或 https:// 开头: {cleaned}")
    return cleaned


def _default_aoe_config() -> Dict[str, Any]:
    return default_aoe_config()


def _load_aoe_config() -> Dict[str, Any]:
    try:
        base = load_aoe_config(AOE_CLUSTER_CONFIG_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 AOE 配置失败: {e}")
    base["local_aoe_url"] = (base.get("local_aoe_url") or "").strip()
    base["default_peer_url"] = (base.get("default_peer_url") or "").strip().rstrip("/")
    base["peers"] = [
        {"name": (peer.get("name") or "peer").strip(), "url": _clean_aoe_url(peer.get("url", ""))}
        for peer in base.get("peers", [])
        if peer.get("url")
    ]
    return base


def _save_aoe_config(config: _AoeClusterConfig) -> Dict[str, Any]:
    data = config.model_dump()
    data["local_aoe_url"] = _clean_aoe_url(data["local_aoe_url"])
    data["default_peer_url"] = _clean_aoe_url(data["default_peer_url"]) if data.get("default_peer_url") else ""
    data["peers"] = [
        {"name": (peer.get("name") or "peer").strip(), "url": _clean_aoe_url(peer.get("url", ""))}
        for peer in data.get("peers", [])
    ]
    os.makedirs(os.path.dirname(AOE_CLUSTER_CONFIG_PATH), exist_ok=True)
    with open(AOE_CLUSTER_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def _resolve_target_aoe_url(target_url: Optional[str]) -> str:
    if target_url:
        return _clean_aoe_url(target_url)
    config = _load_aoe_config()
    if config.get("default_peer_url"):
        return _clean_aoe_url(config["default_peer_url"])
    peers = config.get("peers") or []
    if peers:
        return _clean_aoe_url(peers[0]["url"])
    raise HTTPException(status_code=400, detail="未配置目标 AOE URL")


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


class _WorkflowRegistry:
    """远端 AWM 的内存工作流注册表（app.py 版本）。"""

    def __init__(self):
        self._workflows: Dict[str, Dict[str, Any]] = {}

    def get(self, sub_workflow_id: str) -> Optional[Dict[str, Any]]:
        return self._workflows.get(sub_workflow_id)

    def get_by_handle(self, workflow_handle: str) -> Optional[Dict[str, Any]]:
        return next(
            (item for item in self._workflows.values() if item.get("workflow_handle") == workflow_handle),
            None,
        )

    def register(
        self,
        *,
        source_url: str,
        subtask: Dict[str, Any],
        pipeline_topology: List[Dict[str, Any]],
        workflow_handle: str,
        validation_message: str,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        sub_workflow_id = f"swf_{subtask.get('task_id', uuid.uuid4().hex[:8])}_{uuid.uuid4().hex[:6]}"
        workflow = {
            "sub_workflow_id": sub_workflow_id,
            "workflow_handle": workflow_handle,
            "source_url": source_url,
            "subtask": subtask,
            "task_description": subtask.get("task_description", ""),
            "pipeline_topology": pipeline_topology,
            "timeout_seconds": timeout_seconds,
            "validation_message": validation_message,
            "status": "registering",
            "reference_count": 1,
            "created_at": uuid.uuid4().hex,
            "last_source_url": source_url,
        }
        self._workflows[sub_workflow_id] = workflow
        return workflow

    def remove(self, sub_workflow_id: str) -> Optional[Dict[str, Any]]:
        return self._workflows.pop(sub_workflow_id, None)


_workflow_registry = _WorkflowRegistry()


def _validate_and_build_workflow(subtask: Dict[str, Any]) -> tuple[bool, str, List[Dict[str, Any]]]:
    """远端校验子任务图并构造可执行拓扑。"""
    task_description = (subtask.get("task_description") or "").strip()
    if not task_description:
        return False, "task_description 不能为空", []

    from src.service.agent_registry import get_registry_client

    registry = get_registry_client()
    pipeline_topology = subtask.get("pipeline_topology")
    if not pipeline_topology:
        agent_id = (subtask.get("assigned_agent_id") or "").strip()
        if not agent_id:
            return False, "assigned_agent_id 不能为空", []
        pipeline_topology = [{
            "task_id": subtask.get("task_id", uuid.uuid4().hex[:8]),
            "capability": subtask.get("capability_required", ""),
            "agent_id": agent_id,
            "description": task_description,
            "parameters": dict(subtask.get("parameters") or {}),
        }]
    if any(not isinstance(step, dict) for step in pipeline_topology):
        return False, "远端子任务图必须是非空的线性节点列表", []
    task_ids = [str(step.get("task_id") or "") for step in pipeline_topology]
    if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        return False, "远端子任务图 task_id 必须存在且唯一", []
    for step in pipeline_topology:
        agent_id = str(step.get("agent_id") or "").strip()
        if not agent_id:
            return False, f"节点 {step.get('task_id')} 缺少 agent_id", []
        agent = registry.get_agent_by_id(agent_id)
        if not agent:
            return False, f"Agent {agent_id} 不存在", []
        if agent.get("status") != "online":
            return False, f"Agent {agent_id} 当前不可用: {agent.get('status')}", []
    return True, "子任务图校验通过", pipeline_topology


def _cleanup_remote_workflow(workflow: Dict[str, Any], final_status: str = "closed") -> None:
    """Idempotently unsubscribe the exact instances owned by a remote workflow."""
    if workflow.get("resources_released"):
        workflow["status"] = final_status
        return
    from src.runtime.lifecycle_manager import get_lifecycle_manager

    alcm = get_lifecycle_manager()
    released: set[str] = set()
    for binding in workflow.get("route_instances", []):
        instance_id = binding.get("instance_id")
        if instance_id and instance_id not in released:
            alcm.unsubscribe(instance_id, workflow["workflow_handle"])
            released.add(instance_id)
    workflow["resources_released"] = True
    workflow["status"] = final_status


async def _expire_remote_workflow(sub_workflow_id: str, timeout_seconds: int) -> None:
    await asyncio.sleep(max(1, timeout_seconds))
    workflow = _workflow_registry.get(sub_workflow_id)
    if workflow and workflow.get("status") in {"registering", "ready", "finalized"}:
        _cleanup_remote_workflow(workflow, "expired")
        _session_registry.unregister(workflow.get("session_id", ""))


@app.post("/registry/sync", summary="ARDC Gossip 接收 peer 推送")
async def registry_sync(req: _RegistrySyncRequest):
    """接收 peer 的 Agent、子工作流和资源快照，合并到本节点内存缓存。"""
    from src.service.agent_registry import get_registry_client
    registry = get_registry_client()
    merged_count = registry.sync_from_peer(
        req.source_url,
        req.agents,
        sub_workflows=req.sub_workflows or None,
        resources=req.resources,
    )
    return {
        "status": "ok",
        "source_url": req.source_url,
        "received_agents": len(req.agents),
        "received_sub_workflows": len(req.sub_workflows),
        "received_resources": len(req.resources),
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


@app.get("/api/aoe/config", summary="读取 AOE 跨集群配置")
async def get_aoe_config():
    """读取当前集群本地 AOE 配置文件。"""
    return {
        "config": _load_aoe_config(),
        "path": AOE_CLUSTER_CONFIG_PATH,
    }


@app.put("/api/aoe/config", summary="保存 AOE 跨集群配置")
async def update_aoe_config(req: _AoeClusterConfig):
    """保存当前集群本地 AOE 配置文件。该文件不进入 Git。"""
    return {
        "status": "saved",
        "config": _save_aoe_config(req),
        "path": AOE_CLUSTER_CONFIG_PATH,
    }


@app.post("/api/aoe/gossip/push", summary="手动向目标 AOE 推送本地 Agent 注册表")
async def push_aoe_gossip(req: _AoeGossipPushRequest):
    """立即执行一次 ARDC gossip push，便于 UI 手动打通发现链路。"""
    from src.service.agent_registry import get_registry_client

    config = _load_aoe_config()
    target_url = _resolve_target_aoe_url(req.target_url)
    local_url = _clean_aoe_url(config.get("local_aoe_url") or os.getenv("LOCAL_AOE_URL", "http://localhost:8001"))
    registry = get_registry_client()
    ok = await registry.push_to_peer(target_url, local_url)
    return {
        "status": "ok" if ok else "failed",
        "target_url": target_url,
        "local_url": local_url,
        "local_agents": len(registry.get_local_agents()),
    }


@app.post("/api/aoe/registry/pull", summary="从目标 AOE 拉取其本地 Agent 注册表")
async def pull_aoe_registry(req: _AoeRegistryPullRequest):
    """
    从目标 AOE 的 /api/registry/agents 读取 local agents，并合并成本 AOE peer agents。
    这让 B 可以主动从 A 拉取能力，不依赖 A 侧 gossip 是否已经推送成功。
    """
    import httpx
    from src.service.agent_registry import get_registry_client

    target_url = _resolve_target_aoe_url(req.target_url)
    registry = get_registry_client()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{target_url}/api/registry/agents")
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"拉取远端 registry 失败: {e}")

    remote_agents = payload.get("local", [])
    merged_count = registry.sync_from_peer(target_url, remote_agents)
    return {
        "status": "ok",
        "target_url": target_url,
        "received_agents": len(remote_agents),
        "merged_count": merged_count,
        "agents": remote_agents,
    }


@app.post("/api/aoe/dispatch", summary="从 UI 转发子任务到目标 AOE")
async def dispatch_aoe_from_ui(req: _AoeDispatchUiRequest):
    """从当前 AOE 通过 HTTP 调用目标 AOE 的 /orchestration/dispatch（兼容入口）。"""
    import httpx

    target_url = _resolve_target_aoe_url(req.target_url)
    config = _load_aoe_config()
    timeout = int(req.timeout_seconds or config.get("default_timeout_seconds") or 60)
    task_id = req.task_id or f"ui_task_{uuid.uuid4().hex[:8]}"
    session_id = req.session_id or f"ui_session_{uuid.uuid4().hex[:8]}"
    local_url = _clean_aoe_url(config.get("local_aoe_url") or os.getenv("LOCAL_AOE_URL", "http://localhost:8001"))
    body = {
        "session_id": session_id,
        "source_aoe_url": local_url,
        "subtask": {
            "task_id": task_id,
            "task_description": req.task_description,
            "timeout_seconds": timeout,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=float(timeout) + 10.0) as client:
            resp = await client.post(f"{target_url}/orchestration/dispatch", json=body)
            text = resp.text
            resp.raise_for_status()
            try:
                result = resp.json()
            except Exception:
                result = {"raw": text}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"转发到远端 AOE 失败: {e}")

    return {
        "status": "ok",
        "target_url": target_url,
        "request": body,
        "response": result,
    }


@app.post("/api/aoe/agent-chain-test", summary="验证目标 AOE 的 AgentB 到 AgentC 链路")
async def test_aoe_agent_chain(req: _AoeAgentChainTestRequest):
    """
    通过目标 AOE 的 /api/comm/nats/publish 投递到 Agent B，并等待 Agent B 从 Agent C
    收到结果后回传。用于 /ui 手动验证最小 AOE 链路。
    """
    import httpx

    target_url = _resolve_target_aoe_url(req.target_url)
    config = _load_aoe_config()
    timeout = int(req.timeout_seconds or config.get("default_timeout_seconds") or 30)
    workflow_id = req.workflow_id or f"ui_aoe_{uuid.uuid4().hex[:8]}"
    cluster_id = os.getenv("CLUSTER_ID", os.getenv("AOE_CLUSTER_NAME", "edge-b")).strip() or "edge-b"
    reply_subject = req.reply_subject or f"workflow.{cluster_id}.reply.{_safe_nats_token(workflow_id)}"
    body = {
        "subject": req.in_subject,
        "payload": {
            "workflow_id": workflow_id,
            "text": req.text,
            "reply_subject": reply_subject,
        },
        "reply_subject": reply_subject,
        "timeout_sec": float(timeout),
        "jetstream_domain": os.getenv("NATS_JETSTREAM_DOMAIN", "hub"),
        "stream_subjects": ["workflow.>"],
    }
    try:
        async with httpx.AsyncClient(timeout=float(timeout) + 10.0) as client:
            resp = await client.post(f"{target_url}/api/comm/nats/publish", json=body)
            text = resp.text
            resp.raise_for_status()
            try:
                result = resp.json()
            except Exception:
                result = {"raw": text}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AgentB/AgentC 链路验证失败: {e}")

    return {
        "status": "ok",
        "target_url": target_url,
        "workflow_id": workflow_id,
        "subject": req.in_subject,
        "reply_subject": reply_subject,
        "request": body,
        "response": result,
        "chain_ok": result.get("reply_status") == "received" and bool(result.get("reply")),
    }


@app.get("/api/comm/nats/config", summary="云边 NATS UI 默认配置")
async def get_nats_ui_config():
    return {"config": ui_config_defaults()}


@app.get("/api/comm/nats/status", summary="NATS / JetStream 连接状态")
async def get_nats_status(servers: Optional[str] = None):
    parsed = [s.strip() for s in servers.split(",") if s.strip()] if servers else None
    return await nats_status(parsed)


@app.get("/api/comm/nats/agents", summary="从 K8s 发现 Agent 与 subject")
async def get_nats_edge_agents(cluster_id: Optional[str] = None):
    return list_edge_agents(cluster_id)


@app.post("/api/comm/nats/publish", summary="向本集群 NATS 投递 JetStream 消息（UI 调试）")
async def publish_nats_message(req: _NatsPublishRequest):
    """
    供 Web UI 从宿主机连接 NATS（默认 127.0.0.1 + port-forward）。

    业务 Agent 在集群内仍使用 nats://nats:4222；跨集群仅通过 subject 路由。
    """
    try:
        from nats.aio.client import Client as NATS
        from nats.errors import TimeoutError as NatsTimeoutError
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"缺少 nats-py 依赖，请安装 nats-py 后重启 AOE: {e}",
        )

    servers = resolve_nats_servers(req.servers)
    nc = NATS()
    try:
        await nc.connect(
            servers=servers,
            connect_timeout=5,
            reconnect_time_wait=2,
            max_reconnect_attempts=3,
        )
        js = nc.jetstream(domain=req.jetstream_domain or None)
        stream_state = await _ensure_jetstream_stream(js, req)

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
            "jetstream_domain": req.jetstream_domain,
            "stream": ack.stream,
            "stream_subjects": stream_state.get("subjects"),
            "stream_created": bool(stream_state.get("created")),
            "stream_updated": bool(stream_state.get("updated")),
            "seq": ack.seq,
            "delivery": "jetstream",
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
    兼容旧入口：先注册子工作流，再按 sub_workflow_id 执行。

    新设计建议调用：
    1. POST /orchestration/register_subworkflow（编排期）
    2. POST /orchestration/execute/{sub_workflow_id}（运行期）

    本接口保留仅用于兼容旧调用方。
    """
    task_id = req.subtask.get("task_id", uuid.uuid4().hex[:8])
    timeout = int(req.subtask.get("timeout_seconds", 60))

    register_result = await register_subworkflow(
        _RegisterSubWorkflowRequest(
            subtask=req.subtask,
            session_id=req.session_id,
            source_aoe_url=req.source_aoe_url,
        )
    )
    if register_result.get("status") not in {"ready", "exists"} or not register_result.get("sub_workflow_id"):
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": register_result.get("workflow_handle", ""),
            "status": register_result.get("status", "registration_error"),
            "result": register_result.get("validation_message", "远端子工作流注册失败"),
            "sub_workflow_id": register_result.get("sub_workflow_id", ""),
        }

    workflow = _workflow_registry.get(register_result["sub_workflow_id"])
    compatibility_tasks = []
    for step in workflow.get("pipeline_topology", []):
        compatibility_tasks.append({
            "task_id": step["task_id"],
            "assigned_agent_id": step["agent_id"],
            "parameters": dict(step.get("parameters") or {}),
        })
    from src.service.workflow_routing import plan_signature
    finalize_result = await finalize_subworkflow(
        register_result["sub_workflow_id"],
        _FinalizeSubWorkflowRequest(
            tasks=compatibility_tasks,
            route_instances=register_result.get("route_instances", []),
            frozen_signature=[list(item) for item in plan_signature(compatibility_tasks)],
        ),
    )
    if finalize_result.get("status") != "finalized":
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": register_result.get("workflow_handle", ""),
            "status": "finalize_error",
            "result": finalize_result.get("detail", "远端子工作流冻结失败"),
            "sub_workflow_id": register_result.get("sub_workflow_id", ""),
        }

    execute_result = await execute_subworkflow(
        sub_workflow_id=register_result["sub_workflow_id"],
        req=_ExecuteSubWorkflowRequest(
            session_id=req.session_id,
            source_aoe_url=req.source_aoe_url,
            timeout_seconds=timeout,
        ),
    )
    execute_result["task_id"] = task_id
    return execute_result


@app.post("/orchestration/register_subworkflow", summary="编排期：注册跨 AOE 子工作流")
async def register_subworkflow(req: _RegisterSubWorkflowRequest):
    """
    编排期入口：接收子任务图并在本端注册为可复用子工作流。
    返回 sub_workflow_id / workflow_handle / execute_url。
    """
    task_id = req.subtask.get("task_id", uuid.uuid4().hex[:8])
    local_aoe_url = _clean_aoe_url(_load_aoe_config().get("local_aoe_url") or os.getenv("LOCAL_AOE_URL", "http://localhost:8001"))
    workflow_handle = f"remote_wf_{task_id}_{uuid.uuid4().hex[:6]}"

    is_valid, validation_message, pipeline_topology = _validate_and_build_workflow(req.subtask)
    if not is_valid:
        logger.warning("[E_AOE] 子任务图校验失败: task_id=%s, reason=%s", task_id, validation_message)
        return {
            "status": "rejected",
            "task_id": task_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": "",
            "execute_url": "",
            "validation_message": validation_message,
            "source_aoe_url": req.source_aoe_url,
        }

    timeout_seconds = int(req.subtask.get("timeout_seconds", 60))
    workflow = _workflow_registry.register(
        source_url=req.source_aoe_url,
        subtask=req.subtask,
        pipeline_topology=pipeline_topology,
        workflow_handle=workflow_handle,
        validation_message=validation_message,
        timeout_seconds=timeout_seconds,
    )
    workflow["session_id"] = req.session_id
    workflow["cluster_id"] = _load_aoe_config()["local_name"]
    workflow["route_instances"] = []
    deployed_instance_ids: List[str] = []
    deployed_by_agent: Dict[str, Any] = {}
    try:
        from src.app.agent_warehouse import get_agent_warehouse
        from src.runtime.lifecycle_manager import get_lifecycle_manager
        from src.runtime.resource_selection import resource_config_for_image

        warehouse = get_agent_warehouse()
        alcm = get_lifecycle_manager()
        images = warehouse.list_images()
        for step in pipeline_topology:
            agent_id = str(step["agent_id"])
            instance = deployed_by_agent.get(agent_id)
            if instance is None:
                matches = [image for image in images if image.name == agent_id]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"Agent {agent_id} 在远端 Agent Warehouse 中匹配到 {len(matches)} 个镜像"
                    )
                image = matches[0]
                resource_config = resource_config_for_image(
                    image,
                    capability=str(
                        step.get("capability")
                        or getattr(image, "capability", "")
                        or agent_id
                    ),
                )
                instance = alcm.deploy_agent(agent_id, image.image_id, resource_config)
                deployed_instance_ids.append(instance.instance_id)
                deployed_by_agent[agent_id] = instance
                if instance.status != "running":
                    raise RuntimeError(instance.error_message or f"Agent {agent_id} 部署失败")
                if not alcm.subscribe(instance.instance_id, workflow_handle):
                    raise RuntimeError(f"Agent {agent_id} 实例订阅失败")
            workflow["route_instances"].append({
                "task_id": step["task_id"],
                "agent_id": instance.agent_id,
                "instance_id": instance.instance_id,
                "cluster_id": instance.cluster_id,
                "status": instance.status,
            })
        workflow["status"] = "ready"
        _session_registry.register(req.session_id, workflow_handle)
        workflow["expiry_task"] = asyncio.create_task(
            _expire_remote_workflow(workflow["sub_workflow_id"], timeout_seconds)
        )
    except Exception as exc:
        logger.error("[E_AOE] 子工作流实例部署失败: task_id=%s, error=%s", task_id, exc)
        try:
            from src.runtime.lifecycle_manager import get_lifecycle_manager
            alcm = get_lifecycle_manager()
            subscribed_ids = {item["instance_id"] for item in workflow.get("route_instances", [])}
            for instance_id in deployed_instance_ids:
                if instance_id in subscribed_ids:
                    alcm.unsubscribe(instance_id, workflow_handle)
                elif alcm.get_instance(instance_id):
                    alcm.shutdown_agent(instance_id, force=True)
        finally:
            _workflow_registry.remove(workflow["sub_workflow_id"])
        return {
            "status": "deployment_error",
            "task_id": task_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": "",
            "execute_url": "",
            "validation_message": "远端实例部署失败",
            "deployment_error": str(exc),
            "source_aoe_url": req.source_aoe_url,
        }
    execute_url = f"{local_aoe_url}/orchestration/execute/{workflow['sub_workflow_id']}"
    workflow["execute_url"] = execute_url

    logger.info(
        "[E_AOE] 子工作流注册成功: task_id=%s, swf=%s, status=%s",
        task_id,
        workflow["sub_workflow_id"],
        workflow.get("status", "ready"),
    )
    return {
        "status": workflow.get("status", "ready"),
        "task_id": task_id,
        "workflow_handle": workflow["workflow_handle"],
        "sub_workflow_id": workflow["sub_workflow_id"],
        "execute_url": execute_url,
        "validation_message": workflow["validation_message"],
        "source_aoe_url": req.source_aoe_url,
        "cluster_id": workflow["cluster_id"],
        "route_instances": workflow["route_instances"],
    }


@app.post("/orchestration/finalize/{sub_workflow_id}", summary="编排期：冻结远端子工作流路由")
async def finalize_subworkflow(sub_workflow_id: str, req: _FinalizeSubWorkflowRequest):
    from src.service.workflow_routing import plan_signature
    workflow = _workflow_registry.get(sub_workflow_id)
    if not workflow:
        return {"status": "not_found", "sub_workflow_id": sub_workflow_id}
    if workflow.get("status") != "ready":
        return {"status": "invalid_state", "sub_workflow_id": sub_workflow_id}

    expected_tasks = workflow["pipeline_topology"]
    expected_ids = [str(step["task_id"]) for step in expected_tasks]
    received_ids = [str(task.get("task_id") or "") for task in req.tasks]
    expected_bindings = [
        (str(item["task_id"]), str(item["agent_id"]), str(item["instance_id"]), str(item["cluster_id"]))
        for item in workflow["route_instances"]
    ]
    received_bindings = [
        (
            str(item.get("task_id") or ""), str(item.get("agent_id") or ""),
            str(item.get("instance_id") or ""), str(item.get("cluster_id") or ""),
        )
        for item in req.route_instances
    ]
    if received_ids != expected_ids:
        return {"status": "binding_mismatch", "detail": "远端节点集合或顺序不一致"}
    if received_bindings != expected_bindings:
        return {"status": "binding_mismatch", "detail": "远端实际实例绑定不一致"}
    for task, expected in zip(req.tasks, expected_tasks):
        if str(task.get("assigned_agent_id") or "") != str(expected.get("agent_id") or ""):
            return {"status": "binding_mismatch", "detail": "远端节点 Agent 不一致"}
    global_rows = {str(row[0]): tuple(str(value) for value in row) for row in req.frozen_signature if row}
    for row in plan_signature(req.tasks):
        if global_rows.get(row[0]) != row:
            return {"status": "binding_mismatch", "detail": "冻结签名与最终系统路由不一致"}

    workflow["finalized_tasks"] = [dict(task) for task in req.tasks]
    workflow["frozen_signature"] = [list(item) for item in req.frozen_signature]
    workflow["pipeline_topology"] = [
        {
            **dict(step),
            "parameters": dict(task.get("parameters") or {}),
        }
        for step, task in zip(expected_tasks, req.tasks)
    ]
    workflow["status"] = "finalized"
    expiry_task = workflow.get("expiry_task")
    if expiry_task:
        expiry_task.cancel()
        workflow["expiry_task"] = None
    return {"status": "finalized", "sub_workflow_id": sub_workflow_id}


@app.post("/orchestration/execute/{sub_workflow_id}", summary="运行期：执行已注册跨 AOE 子工作流")
async def execute_subworkflow(sub_workflow_id: str, req: _ExecuteSubWorkflowRequest):
    """运行期入口：按 sub_workflow_id 执行远端已注册工作流。"""
    from src.distributed_workflow import run_distributed_workflow
    from src.service.workflow_routing import plan_signature

    workflow = _workflow_registry.get(sub_workflow_id)
    if not workflow:
        return {
            "status": "not_found",
            "sub_workflow_id": sub_workflow_id,
            "session_id": req.session_id,
            "result": "未找到已注册的子工作流",
        }
    if workflow.get("status") != "finalized":
        return {
            "status": "not_finalized",
            "sub_workflow_id": sub_workflow_id,
            "session_id": req.session_id,
            "result": "远端子工作流尚未完成路由冻结",
        }

    workflow_handle = workflow["workflow_handle"]
    workflow["status"] = "running"
    timeout = int(req.timeout_seconds or workflow.get("timeout_seconds", 60))

    workflow_task = asyncio.create_task(
        # The finalized segment already contains its global boundary parameters.
        run_distributed_workflow(
            user_input=workflow.get("task_description", ""),
            adaptive_mode=False,
            timeout_seconds=timeout,
            workflow_id=workflow_handle,
            route_instances=workflow.get("route_instances", []),
            route_prevalidated=True,
            execution_plan=[dict(task) for task in workflow.get("finalized_tasks", [])],
            frozen_plan_signature=[
                list(item) for item in plan_signature(workflow.get("finalized_tasks", []))
            ],
        )
    )
    _session_registry.register(req.session_id, workflow_handle, task=workflow_task)

    try:
        result = await asyncio.wait_for(asyncio.shield(workflow_task), timeout=float(timeout))
        workflow["status"] = "finalized"
        logger.info("[E_AOE] 子工作流执行完成: swf=%s, session_id=%s", sub_workflow_id, req.session_id)
        return {
            "status": "completed",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": str(result)[:3000],
        }
    except asyncio.TimeoutError:
        workflow_task.cancel()
        workflow["status"] = "error"
        logger.warning("[E_AOE] 子工作流执行超时: swf=%s, session_id=%s", sub_workflow_id, req.session_id)
        return {
            "status": "timeout",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": f"子工作流执行超时（{timeout}s）",
        }
    except asyncio.CancelledError:
        workflow["status"] = "error"
        logger.info("[E_AOE] 子工作流被取消: swf=%s, session_id=%s", sub_workflow_id, req.session_id)
        return {
            "status": "cancelled",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": "子工作流已取消",
        }
    except Exception as e:
        workflow["status"] = "error"
        logger.error("[E_AOE] 子工作流执行失败: swf=%s, session_id=%s, err=%s", sub_workflow_id, req.session_id, e)
        return {
            "status": "error",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": f"子工作流执行失败: {str(e)}",
        }
    finally:
        # A finalized remote workflow is reusable for subsequent queries and
        # remains subscribed until DELETE /orchestration/session/{session_id}.
        if workflow.get("status") == "error":
            _cleanup_remote_workflow(workflow, "error")


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
        workflow = _workflow_registry.get_by_handle(workflow_handle)
        if workflow:
            expiry_task = workflow.get("expiry_task")
            if expiry_task:
                expiry_task.cancel()
            _cleanup_remote_workflow(workflow, "closed")
    except Exception as e:
        logger.warning("[E_AOE] ALCM 退订失败（非关键）: %s", e)

    return {
        "status": "closed",
        "session_id": session_id,
        "workflow_handle": workflow_handle,
    }


@app.on_event("startup")
async def _on_startup():
    """启动 NATS port-forward（可选）与 ARDC gossip（可选）。"""
    maybe_start_nats_port_forward()

    if os.getenv("ENABLE_AOE_GOSSIP", "0").strip().lower() in {"1", "true", "yes", "on"}:
        import re
        from src.service.agent_registry import get_registry_client

        config = _load_aoe_config()
        peer_urls_raw = os.getenv("PEER_AOE_URLS", "")
        peer_urls = [u.strip() for u in re.split(r"[\s,]+", peer_urls_raw) if u.strip()]
        if not peer_urls:
            peer_urls = [peer["url"] for peer in config.get("peers", [])]
        if not peer_urls:
            return
        local_url = os.getenv(
            "LOCAL_AOE_URL", config.get("local_aoe_url") or "http://localhost:8000"
        ).strip()
        interval = int(os.getenv("GOSSIP_INTERVAL", "30"))
        registry = get_registry_client()
        await registry.start_gossip_background(
            peer_urls=peer_urls,
            local_url=local_url,
            interval=interval,
        )


@app.on_event("shutdown")
async def _on_shutdown():
    stop_nats_port_forward()


@app.on_event("startup")
async def _restore_scheduled_apps():
    """
    服务启动时，自动恢复配置了 schedule_auto_restart: true 的周期调度应用。
    """
    try:
        from src.app.app_manager import get_app_manager
        manager = get_app_manager()
        await manager.restore_schedules()
    except Exception as e:
        logger.warning(f"[Startup] 恢复周期调度失败（非关键）: {e}")


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


# ======================================================================
# 周期调度 API
# ======================================================================

@app.post("/api/apps/{app_id}/schedule/start", summary="启动周期调度")
async def start_schedule(app_id: str):
    """
    启动应用的周期调度。
    需在 GuidanceFile.constraints 中配置 schedule_interval_seconds。
    可选配置 schedule_max_parallel（默认 5）、schedule_max_history（默认 100）。
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        app = manager.get_app(app_id)
        if not app:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        success = await manager.start_schedule(app_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="启动调度失败：应用可能已在调度中或未配置 schedule_interval_seconds",
            )

        return {
            "app_id": app_id,
            "status": "scheduled",
            "message": "周期调度已启动",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"start_schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/apps/{app_id}/schedule/stop", summary="停止周期调度")
async def stop_schedule(app_id: str):
    """
    停止应用的周期调度。
    活跃的工作流实例继续运行直到完成。
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        app = manager.get_app(app_id)
        if not app:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        success = await manager.stop_schedule(app_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="停止调度失败：应用当前未处于周期调度状态",
            )

        return {
            "app_id": app_id,
            "status": "stopped",
            "message": "周期调度已停止",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"stop_schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/apps/{app_id}/schedule/status", summary="获取调度状态")
async def get_schedule_status(app_id: str):
    """
    获取应用的周期调度状态。
    返回间隔、活跃实例数、累计执行次数等信息。
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        app = manager.get_app(app_id)
        if not app:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        from src.service.workflow_scheduler import get_workflow_scheduler
        scheduler = get_workflow_scheduler()
        status = scheduler.get_schedule_status(app_id)

        if not status:
            return {
                "app_id": app_id,
                "scheduled": False,
                "message": "应用未处于周期调度状态",
            }

        return {
            "app_id": app_id,
            "scheduled": True,
            **status,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/apps/{app_id}/schedule/history", summary="获取调度执行历史")
async def get_schedule_history(app_id: str, limit: int = 50):
    """
    获取应用的周期调度执行历史。
    返回最近 N 次执行记录，包含 run_id、开始时间、完成时间、状态等。
    """
    try:
        from src.app.app_manager import get_app_manager

        manager = get_app_manager()
        app = manager.get_app(app_id)
        if not app:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        from src.service.workflow_scheduler import get_workflow_scheduler
        scheduler = get_workflow_scheduler()
        history = scheduler.get_history(app_id, limit=limit)

        return {
            "app_id": app_id,
            "total": len(history),
            "records": history,
        }
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
    agents_required: Optional[List[str]] = Field(None, description="新的所需 Agent 能力列表")
    images: Optional[List[Dict[str, Any]]] = Field(None, description="新的 Agent 镜像清单")
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
            agents_required=request.agents_required,
            images=request.images,
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


@app.get("/api/agents/instances/by-cluster", summary="按集群获取活动 Agent 实例")
async def list_agent_instances_by_cluster():
    """汇总本集群和当前可用 peer 集群的活动实例。"""
    import httpx

    inactive_statuses = {"stopped"}
    config = _load_aoe_config()
    local_name = config.get("local_name") or "cluster"

    def attach_qos(instances, metrics):
        metrics_by_agent = {
            metric.get("agent_id"): metric
            for metric in metrics
            if metric.get("agent_id")
        }
        for instance in instances:
            instance["qos"] = metrics_by_agent.get(instance.get("agent_id"), {})
        return instances

    try:
        from src.runtime.lifecycle_manager import get_lifecycle_manager
        from src.runtime.qos_monitor import get_qos_monitor

        local_instances = [
            instance.to_dict()
            for instance in get_lifecycle_manager().list_instances()
            if instance.status not in inactive_statuses
        ]
        try:
            local_metrics = get_qos_monitor().get_metrics_dict()
        except Exception:
            local_metrics = []
        attach_qos(local_instances, local_metrics)
        local_cluster = {
            "name": local_name,
            "url": config.get("local_aoe_url") or "",
            "is_local": True,
            "status": "ok",
            "error": None,
            "instances": local_instances,
        }
    except Exception as e:
        local_cluster = {
            "name": local_name,
            "url": config.get("local_aoe_url") or "",
            "is_local": True,
            "status": "error",
            "error": str(e),
            "instances": [],
        }

    async def fetch_peer(client, peer):
        name = peer.get("name") or "peer"
        url = (peer.get("url") or "").rstrip("/")
        cluster = {
            "name": name,
            "url": url,
            "is_local": False,
            "status": "ok",
            "error": None,
            "instances": [],
        }
        try:
            response = await client.get(f"{url}/api/agents/instances")
            response.raise_for_status()
            rows = response.json().get("instances", [])
            cluster["instances"] = [
                row for row in rows if row.get("status") not in inactive_statuses
            ]
            try:
                qos_response = await client.get(f"{url}/api/qos/metrics")
                qos_response.raise_for_status()
                metrics = qos_response.json().get("metrics", [])
            except Exception:
                metrics = []
            attach_qos(cluster["instances"], metrics)
        except Exception as e:
            cluster["status"] = "error"
            cluster["error"] = str(e)
        return cluster

    peers = config.get("peers") or []
    if not peers:
        return {"clusters": [local_cluster]}

    async with httpx.AsyncClient(timeout=5.0) as client:
        peer_clusters = await asyncio.gather(
            *(fetch_peer(client, peer) for peer in peers)
        )
    available_peers = [
        cluster for cluster in peer_clusters if cluster["status"] == "ok"
    ]
    return {"clusters": [local_cluster, *available_peers]}


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


@app.delete("/api/agents/instances/{instance_id}", summary="停止 Agent 实例")
async def stop_agent_instance(instance_id: str):
    """强制停止指定实例；Kubernetes 后端同时删除 Deployment、Pod 和 Service。"""
    from src.runtime.lifecycle_manager import get_lifecycle_manager

    manager = get_lifecycle_manager()
    instance = manager.get_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"实例 {instance_id} 不存在")
    if instance.status == "stopped":
        return {"instance_id": instance_id, "status": "stopped", "message": "实例已停止"}
    if not manager.shutdown_agent(instance_id, force=True):
        detail = instance.error_message or f"实例 {instance_id} 停止失败"
        raise HTTPException(status_code=500, detail=detail)
    return {"instance_id": instance_id, "status": "stopped", "message": "停止成功"}


@app.post("/tests/call", summary="测试调用运行中的 Agent 实例")
async def test_call_agent(request: _AgentTestCallRequest):
    """校验运行实例并通过其 Agent ID 发起一次标准 A2A 调用。"""
    from src.runtime.lifecycle_manager import get_lifecycle_manager
    from src.service.a2a_client import get_global_a2a_client
    from src.service.agent_registry import get_registry_client
    from src.service.message_router import get_message_router

    instance = get_lifecycle_manager().get_instance(request.instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"实例 {request.instance_id} 不存在")
    if instance.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"实例 {request.instance_id} 当前状态为 {instance.status}，无法调用",
        )

    agent_url = get_message_router().route_direct(instance.agent_id)
    if not agent_url:
        raise HTTPException(
            status_code=503,
            detail=f"Agent {instance.agent_id} 当前没有可用路由",
        )

    agent_info = get_registry_client().get_agent_by_id(instance.agent_id) or {}
    task_id = f"test_{uuid.uuid4().hex}"

    from src.protocols.a2a_protocol import A2ATaskRequest

    task_request = A2ATaskRequest(
        task_id=task_id,
        task_type=agent_info.get("capability") or "test",
        task_description=request.task_description,
        parameters=request.parameters,
        metadata=request.metadata,
    )
    response = await get_global_a2a_client().send_task_request(agent_url, task_request)

    return {
        "instance_id": instance.instance_id,
        "agent_id": instance.agent_id,
        "task_id": task_id,
        "status": response.state,
        "result": response.result,
        "error_message": response.error_message,
        "metadata": response.metadata,
    }


@app.post("/tests/orchestration/plan", summary="只读生成智能体编排预览")
async def preview_orchestration_plan(request: _OrchestrationPlanPreviewRequest):
    """复用正式 Planner 生成计划，但不部署、调用 Agent 或注册远端子工作流。"""
    from src.api.visualization import extract_full_view
    from src.app.pipeline_parser import parse_pipeline
    from src.distributed_workflow import generate_execution_plan

    task_description = (request.task_description or "").strip()
    skills_content = request.skills_content or ""
    source = "custom"

    if request.app_id:
        from src.app.app_manager import get_app_manager

        app_info = get_app_manager().get_app(request.app_id)
        if app_info is None:
            raise HTTPException(status_code=404, detail=f"应用 {request.app_id} 不存在")
        if app_info.guidance_file is None:
            raise HTTPException(status_code=422, detail=f"应用 {request.app_id} 没有编排指导文件")
        task_description = app_info.guidance_file.task_description.strip()
        skills_content = app_info.guidance_file.skills_content or ""
        source = "application"

    if not task_description:
        raise HTTPException(status_code=422, detail="任务描述不能为空")

    pipeline_topology = parse_pipeline(skills_content) or []
    planning = await generate_execution_plan(
        task_description,
        skills_content=skills_content,
        pipeline_topology=pipeline_topology,
        timeout_seconds=30,
    )
    state: Dict[str, Any] = {
        "messages": [],
        "execution_plan": planning["execution_plan"],
        "skills_content": skills_content,
        "pipeline_topology": pipeline_topology,
        "current_task_index": 0,
        "failed_tasks": [],
        "cross_host_sessions": {},
        "failed_remote_aoe_urls": {},
        "complexity_level": planning["complexity_level"],
        "orchestration_mode": planning["orchestration_mode"],
        "plan_generated": True,
    }

    view = extract_full_view(state)
    return {
        "source": source,
        "app_id": request.app_id,
        "task_description": task_description,
        "execution_plan": state.get("execution_plan", []),
        "orchestration": view["orchestration"],
        "topology": view["topology"],
        "task_graph": view["topology"],
    }


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


@app.post("/api/orchestration/alerts", summary="接收 Alertmanager 告警")
async def receive_alertmanager_webhook(payload: Dict[str, Any]):
    """
    接收 Alertmanager webhook。

    此接口只负责可靠接收与记录告警状态。扩缩容、迁移等动作应由编排层策略
    控制器消费告警后执行，不能在 webhook 请求路径中直接操作 Kubernetes。
    """
    try:
        from src.service.alertmanager_receiver import get_alertmanager_receiver

        # 接收器使用 fingerprint 幂等保存告警，避免 Alertmanager 重试造成重复动作。
        result = get_alertmanager_receiver().receive(payload)
        return {"status": "accepted", **result}
    except Exception as e:
        logger.error(f"receive_alertmanager_webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orchestration/alerts", summary="查询编排层收到的告警")
async def list_orchestration_alerts(active_only: bool = False):
    """供自动扩缩容控制器或运维界面查询编排层当前保存的告警。"""
    from src.service.alertmanager_receiver import get_alertmanager_receiver

    return {
        # active_only=true 时过滤掉已经由 Alertmanager 通知恢复的告警。
        "alerts": get_alertmanager_receiver().list_alerts(active_only=active_only),
    }


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
        # Kubernetes Python 客户端是同步的，放入线程避免阻塞 FastAPI 事件循环。
        await asyncio.to_thread(registry.refresh_from_kubernetes)
        nodes = [
            node for node in registry.get_all_nodes_with_peers()
            if node.status == "online"
            and node.cpu_available >= min_cpu
            and node.mem_available_mb >= min_mem_mb
            and (not node_type or node.node_type == node_type)
        ]
        nodes.sort(key=lambda node: node.cpu_available, reverse=True)
        return {
            "summary": registry.get_summary(include_peers=True),
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


@app.get("/api/apps/{app_id}/agent-views", summary="获取应用关联 Agent 视图")
async def list_app_agent_views(app_id: str):
    """
    返回当前应用关联的运行中 Agent 前端视图信息。

    前端会使用这些 URL 以纵向堆叠 iframe 的方式展示每个智能体页面。
    """
    try:
        from src.app.app_manager import get_app_manager
        from src.service.agent_registry import get_registry_client
        from src.service.agent_scheduler import get_agent_scheduler

        manager = get_app_manager()
        app = manager.get_app(app_id)
        if not app:
            raise HTTPException(status_code=404, detail=f"应用 {app_id} 不存在")

        scheduler = get_agent_scheduler()
        registry = get_registry_client()

        running_deployments = scheduler.get_running_agents()
        deployments_by_agent = {}
        for deployment in running_deployments:
            deployments_by_agent.setdefault(deployment.agent_id, []).append(deployment)

        registry_agents = {agent["id"]: agent for agent in registry.get_all_agents()}
        image_order = {image_id: index for index, image_id in enumerate(app.image_ids or [])}
        required_capabilities = set()
        if app.guidance_file and app.guidance_file.agents_required:
            required_capabilities = {str(item).strip().lower() for item in app.guidance_file.agents_required if str(item).strip()}

        views = []
        for deployment in running_deployments:
            if app.image_ids and deployment.image_id not in set(app.image_ids):
                continue

            agent = registry_agents.get(deployment.agent_id)
            if not agent:
                continue

            ip = agent.get("ip")
            port = agent.get("port")
            if not ip or not port:
                continue

            capability = str(agent.get("capability") or "").strip().lower()
            if required_capabilities and capability not in required_capabilities and not app.image_ids:
                continue

            views.append({
                "agent_id": deployment.agent_id,
                "deployment_id": deployment.deployment_id,
                "image_id": deployment.image_id,
                "capability": capability,
                "status": deployment.status,
                "ip": ip,
                "port": port,
                "frontend_url": f"http://{ip}:{port}",
                "backend": deployment.backend,
                "updated_at": deployment.updated_at,
                "sort_key": image_order.get(deployment.image_id, 10_000),
            })

        if not views:
            for agent_id, deployment_list in deployments_by_agent.items():
                agent = registry_agents.get(agent_id)
                if not agent:
                    continue
                ip = agent.get("ip")
                port = agent.get("port")
                if not ip or not port:
                    continue
                capability = str(agent.get("capability") or "").strip().lower()
                if required_capabilities and capability not in required_capabilities:
                    continue
                latest_deployment = deployment_list[-1]
                views.append({
                    "agent_id": latest_deployment.agent_id,
                    "deployment_id": latest_deployment.deployment_id,
                    "image_id": latest_deployment.image_id,
                    "capability": capability,
                    "status": latest_deployment.status,
                    "ip": ip,
                    "port": port,
                    "frontend_url": f"http://{ip}:{port}",
                    "backend": latest_deployment.backend,
                    "updated_at": latest_deployment.updated_at,
                    "sort_key": image_order.get(latest_deployment.image_id, 10_000),
                })

        views.sort(key=lambda item: (item["sort_key"], item["agent_id"], item["deployment_id"]))
        return {"app_id": app_id, "views": views}
    except HTTPException:
        raise
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

# ======================================================================
# ? Agent Builder API (Auto-Agent Integration)
# ======================================================================

class AgentGenerateRequest(BaseModel):
    """Agent generation request from Markdown configuration"""
    agent_md_content: str = Field(..., description="agent.md content (role definition)")
    workflow_md_content: Optional[str] = Field(None, description="workflow.md content (optional)")
    agent_name: str = Field("custom-agent", description="Agent name")
    capability: str = Field("chat", description="Capability type: chat/nlp/search/compute/vision")
    version: str = Field("1.0.0", description="Version string")
    install: bool = Field(True, description="Auto-install to warehouse")


@app.get("/api/agent-builder/templates", summary="Get Agent templates")
async def get_agent_templates():
    try:
        from src.app.agent_warehouse import get_agent_warehouse
        warehouse = get_agent_warehouse()
        return warehouse.get_agent_templates()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/agent-builder/generate", summary="Generate Agent package")
async def generate_agent_package(request: AgentGenerateRequest):
    try:
        from src.app.agent_warehouse import get_agent_warehouse
        warehouse = get_agent_warehouse()
        result = warehouse.generate_agent(
            agent_md_content=request.agent_md_content,
            workflow_md_content=request.workflow_md_content,
            agent_name=request.agent_name,
            capability=request.capability,
            version=request.version,
            install=request.install,
        )
        return {
            "success": True,
            **result,
            "deploy_instructions": {
                "unzip": f"unzip {result['agent_id']}.zip -d my-agent && cd my-agent",
                "env": "export DEEPSEEK_API_KEY=sk-your-key",
                "start": "docker-compose up -d",
                "port": 9001,
                "a2a_endpoint": "POST /a2a/execute",
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Agent generation failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agent-builder/list", summary="List generated Agents")
async def list_generated_agents():
    try:
        from src.app.agent_warehouse import get_agent_warehouse
        warehouse = get_agent_warehouse()
        return {"agents": warehouse.list_generated_agents()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agent-builder/download/{agent_id}", summary="Download Agent package")
async def download_agent_package(agent_id: str):
    try:
        from src.app.agent_warehouse import get_agent_warehouse
        warehouse = get_agent_warehouse()
        zip_path = warehouse.download_agent(agent_id)
        if not zip_path:
            raise HTTPException(status_code=404, detail="Agent package not found")
        return FileResponse(
            path=str(zip_path),
            filename=f"{agent_id}.zip",
            media_type="application/zip",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Web UI
# ======================================================================

@app.get("/ui",response_class=HTMLResponse, include_in_schema=False)
async def web_ui(request: Request):
    """应用管理层 Web UI 控制台 - 直接使用 Jinja2Templates 渲染"""
    return templates.TemplateResponse(request, "ui.html")


@app.get("/test", response_class=HTMLResponse, include_in_schema=False)
async def agent_test_page(request: Request):
    """Agent 实例 A2A 调用测试页面。"""
    return templates.TemplateResponse(request, "test.html")


@app.get("/ui/apps/{app_id}", response_class=HTMLResponse, include_in_schema=False)
async def app_details_page(request: Request, app_id: str):
    """应用详情页（前端模板）。显示应用逻辑、运行状态与智能体视图占位。"""
    return templates.TemplateResponse(request, "app_details.html")
