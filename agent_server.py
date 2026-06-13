# agent_server.py - Agent 节点服务（支持 A2A 协议 + 跨主体编排端点）

import asyncio
import hashlib
import json
import logging
import uuid as _uuid
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel
import requests
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI()


# ============================================================
# 跨主体会话注册中心（本 E_AOE 节点维护）
# ============================================================

class _SessionRegistry:
    """跟踪远端 T_AOE 发来的跨主体会话，支持运行中 Task 取消"""
    def __init__(self):
        # session_id → {"handle": str, "task": Optional[asyncio.Task]}
        self._sessions: dict[str, dict] = {}

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
        """取消会话关联的 asyncio.Task（若存在），返回 True 表示成功取消"""
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
    """远端 AWM 的内存工作流注册表。

    这里模拟“远端工作流管理层”应该承担的职责：
    1. 编排期接收子任务图并做校验；
    2. 为通过校验的任务图生成子工作流 ID；
    3. 运行期根据子工作流 ID 找回对应的拓扑并执行。

    由于当前工程里还没有独立的 AWM 服务，这里先用内存表模拟生命周期。
    """

    def __init__(self):
        self._workflows: dict[str, dict[str, Any]] = {}
        self._signature_index: dict[str, str] = {}

    @staticmethod
    def _signature(subtask: dict) -> str:
        # 使用任务图的稳定签名做去重，避免同一子任务图被重复注册成多个工作流。
        payload = json.dumps(subtask, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, sub_workflow_id: str) -> Optional[dict[str, Any]]:
        return self._workflows.get(sub_workflow_id)

    def register(
        self,
        *,
        source_url: str,
        subtask: dict,
        pipeline_topology: list,
        workflow_handle: str,
        validation_message: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        # 如果同一张子任务图已经注册过，则只增加引用计数，不重复创建工作流。
        signature = self._signature(subtask)
        existing_id = self._signature_index.get(signature)
        if existing_id and existing_id in self._workflows:
            workflow = self._workflows[existing_id]
            # 引用计数用于模拟“工作流被多个上层任务复用”的场景。
            workflow["reference_count"] = workflow.get("reference_count", 0) + 1
            workflow["status"] = "exists"
            workflow["validation_message"] = validation_message
            workflow["last_source_url"] = source_url
            return workflow

        # 新注册时生成一个全局唯一的子工作流 ID，后续运行期只认这个 ID。
        sub_workflow_id = f"swf_{subtask.get('task_id', _uuid.uuid4().hex[:8])}_{_uuid.uuid4().hex[:6]}"
        workflow = {
            # 基础标识：运行期和日志里主要依赖这两个字段定位工作流。
            "sub_workflow_id": sub_workflow_id,
            "workflow_handle": workflow_handle,
            # 来源地址与原始子任务图一起保存，便于调试和审计。
            "source_url": source_url,
            "subtask": subtask,
            # 远端校验后真正保存的是“可执行拓扑”，而不是原始输入文本。
            "task_description": subtask.get("task_description", ""),
            "pipeline_topology": pipeline_topology,
            "timeout_seconds": timeout_seconds,
            "validation_message": validation_message,
            # ready 表示已成功注册，后续可以被运行期调用。
            "status": "ready",
            "reference_count": 1,
            "created_at": _uuid.uuid4().hex,
            "last_source_url": source_url,
        }
        self._workflows[sub_workflow_id] = workflow
        self._signature_index[signature] = sub_workflow_id
        return workflow


_workflow_registry = _WorkflowRegistry()

class A2AMessage(BaseModel):
    message_id: str
    sender_id: str
    receiver_id: str
    message_type: str
    payload: dict


class DispatchRequest(BaseModel):
    """跨主体子任务分发请求（来自 T_AOE）"""
    subtask: dict
    session_id: str
    source_aoe_url: str = ""


class RegisterSubWorkflowRequest(BaseModel):
    """编排期子工作流注册请求。"""
    subtask: dict
    session_id: str
    source_aoe_url: str = ""


class ExecuteSubWorkflowRequest(BaseModel):
    """运行期子工作流执行请求。"""
    session_id: str
    source_aoe_url: str = ""
    timeout_seconds: int = 60

def tavily_search(query: str, max_results: int = 5) -> str:
    """使用 Tavily API 进行搜索"""
    api_key = os.getenv("TAVILY_API_KEY")
    
    if not api_key:
        return "❌ 错误: 未配置 TAVILY_API_KEY 环境变量\n\n请在 .env 文件中添加: TAVILY_API_KEY=your_key"
    
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
                "include_raw_content": False
            },
            timeout=10
        )
        
        if response.status_code != 200:
            return f"❌ 搜索失败: HTTP {response.status_code}"
        
        data = response.json()
        
        # 格式化结果
        result = f"🔍 搜索: {query}\n\n"
        
        # 添加答案摘要
        if data.get("answer"):
            result += f"📋 摘要:\n{data['answer']}\n\n"
        
        # 添加搜索结果
        result += "📚 搜索结果:\n"
        for i, item in enumerate(data.get("results", []), 1):
            result += f"\n{i}. {item.get('title', '无标题')}\n"
            result += f"   URL: {item.get('url', 'N/A')}\n"
            result += f"   {item.get('content', '无内容')[:200]}...\n"
        
        return result
        
    except requests.Timeout:
        return "❌ 搜索超时，请稍后重试"
    except Exception as e:
        return f"❌ 搜索出错: {str(e)}"

@app.post("/a2a/execute")
async def execute_task(message: A2AMessage):
    """处理 A2A 任务请求"""
    
    task = message.payload
    task_description = task.get("task_description", "")
    
    # 执行真实的搜索
    if "搜索" in task_description or "search" in task_description.lower():
        # 提取搜索关键词
        query = task_description.replace("搜索", "").replace("查找", "").strip()
        
        # 调用 Tavily 搜索 API
        result = tavily_search(query)
        
        return A2AMessage(
            message_id=message.message_id,
            sender_id=message.receiver_id,
            receiver_id=message.sender_id,
            message_type="response",
            payload={
                "task_id": task["task_id"],
                "status": "success",
                "result": result
            }
        )
    
    # 其他任务类型
    return A2AMessage(
        message_id=message.message_id,
        sender_id=message.receiver_id,
        receiver_id=message.sender_id,
        message_type="response",
        payload={
            "task_id": task["task_id"],
            "status": "error",
            "error_message": "不支持的任务类型"
        }
    )

@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "online", "load": 0.2, "active_tasks": 0}


# ============================================================
# ARDC Gossip 端点
# ============================================================

class RegistrySyncRequest(BaseModel):
    """来自 peer 节点的 agent 列表推送请求"""
    source_url: str
    agents: list  # List[AgentInfo]（runtime dict）
    sub_workflows: list = []  # List[SubWorkflowInfo]（可选，向后兼容）


@app.post("/registry/sync")
async def registry_sync(req: RegistrySyncRequest):
    """
    接收来自 T_AOE / peer 节点的 gossip 推送，合并到本节点注册表。

    对应架构 §1 智能体发现（ARDC Gossip）：
    - 来源节点 POST {source_url, agents, sub_workflows} 到本端点
    - 本节点调用 registry.sync_from_peer() 更新 peer 缓存
    - 返回合并后 peer agents 总数
    """
    from src.service.agent_registry import get_registry_client

    registry = get_registry_client()
    merged_count = registry.sync_from_peer(
        req.source_url,
        req.agents,
        sub_workflows=req.sub_workflows or None,
    )
    logger.info(
        f"[ARDC] /registry/sync 处理完成: source={req.source_url}, "
        f"agents={len(req.agents)}, sub_workflows={len(req.sub_workflows)}, "
        f"merged_total={merged_count}"
    )
    return {
        "status": "ok",
        "source_url": req.source_url,
        "received_agents": len(req.agents),
        "received_sub_workflows": len(req.sub_workflows),
        "merged_count": merged_count,
    }


def _validate_and_build_workflow(subtask: dict) -> tuple[bool, str, list]:
    """验证子任务图并构造可执行的固定拓扑。

    这一步对应设计图中的“远端校验子任务图”：
    - 先检查任务描述和目标 Agent 是否存在；
    - 再把输入压缩成远端真正执行时需要的 pipeline_topology；
    - 最终返回给注册接口，由注册接口决定是否生成子工作流。
    """
    task_description = (subtask.get("task_description") or "").strip()
    if not task_description:
        return False, "task_description 不能为空", []

    agent_id = (subtask.get("assigned_agent_id") or "").strip()
    if not agent_id:
        return False, "assigned_agent_id 不能为空", []

    from src.service.agent_registry import get_registry_client

    registry = get_registry_client()
    agent = registry.get_agent_by_id(agent_id)
    if not agent:
        return False, f"Agent {agent_id} 不存在", []
    if agent.get("status") != "online":
        return False, f"Agent {agent_id} 当前不可用: {agent.get('status')}", []

    pipeline_topology = subtask.get("pipeline_topology") or [{
        # 如果上层没有显式给出拓扑，就用单节点拓扑兜底，保证最小可执行性。
        "capability": subtask.get("capability_required") or agent.get("capability", ""),
        "agent_id": agent_id,
        "description": task_description,
        "target_ip": agent.get("ip", "127.0.0.1"),
        "target_port": agent.get("port", 8000),
    }]
    return True, "子任务图校验通过", pipeline_topology


@app.post("/orchestration/register_subworkflow")
async def register_subworkflow(req: RegisterSubWorkflowRequest):
    """
    编排期：接收子任务图，校验后注册为远端子工作流。

    返回 sub_workflow_id / workflow_handle / execute_url，供本地运行期调用。
    """
    task_id = req.subtask.get("task_id", _uuid.uuid4().hex[:8])
    local_aoe_url = os.getenv("LOCAL_AOE_URL", "http://localhost:8000")
    workflow_handle = f"remote_wf_{task_id}_{_uuid.uuid4().hex[:6]}"

    # 先在远端做输入校验，确保“编排期注册”阶段就把不可执行的图挡掉。
    is_valid, validation_message, pipeline_topology = _validate_and_build_workflow(req.subtask)
    if not is_valid:
        logger.warning(f"[E_AOE] 子任务图校验失败: task_id={task_id}, reason={validation_message}")
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
    execute_url = f"{local_aoe_url}/orchestration/execute/{workflow['sub_workflow_id']}"
    workflow["execute_url"] = execute_url

    logger.info(
        f"[E_AOE] 子工作流注册成功: task_id={task_id}, swf={workflow['sub_workflow_id']}, "
        f"status={workflow['status']}"
    )
    return {
        # 远端返回给本地的最小执行信息：ID、句柄、执行入口和校验结果。
        "status": workflow.get("status", "ready"),
        "task_id": task_id,
        "workflow_handle": workflow["workflow_handle"],
        "sub_workflow_id": workflow["sub_workflow_id"],
        "execute_url": execute_url,
        "validation_message": workflow["validation_message"],
        "source_aoe_url": req.source_aoe_url,
    }


@app.post("/orchestration/execute/{sub_workflow_id}")
async def execute_subworkflow(sub_workflow_id: str, req: ExecuteSubWorkflowRequest):
    """
    运行期：按已注册的子工作流 ID 执行。
    """
    # 运行期执行只接受子工作流 ID，不再接受完整子任务图；
    # 这样就能保证“编排期建图”和“运行期执行”两阶段解耦。
    # 运行期只根据子工作流 ID 找回远端注册表中的拓扑，不再接收完整子任务图。
    workflow = _workflow_registry.get(sub_workflow_id)
    if not workflow:
        return {
            "status": "not_found",
            "sub_workflow_id": sub_workflow_id,
            "session_id": req.session_id,
            "result": "未找到已注册的子工作流",
        }

    workflow_handle = workflow["workflow_handle"]
    # 会话超时优先采用本次调用的参数，其次回退到注册期保存的默认超时。
    timeout = int(req.timeout_seconds or workflow.get("timeout_seconds", 60))

    from src.distributed_workflow import run_distributed_workflow

    workflow_task = asyncio.create_task(
        run_distributed_workflow(
            user_input=workflow.get("task_description", ""),
            pipeline_topology=workflow.get("pipeline_topology", []),
            adaptive_mode=False,
            timeout_seconds=timeout,
            workflow_id=workflow_handle,
        )
    )
    # 这里把运行中的 asyncio.Task 记录到会话表，
    # 这样后续的 /orchestration/session/{id} 就可以中断这个远端执行。
    # 注册到会话表，便于停止/取消接口在运行中中断对应任务。
    _session_registry.register(req.session_id, workflow_handle, task=workflow_task)

    try:
        result = await asyncio.wait_for(asyncio.shield(workflow_task), timeout=float(timeout))
        logger.info(f"[E_AOE] 子工作流执行完成: swf={sub_workflow_id}, session_id={req.session_id}")
        return {
            "status": "completed",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": str(result)[:3000],
        }

    except asyncio.TimeoutError:
        workflow_task.cancel()
        logger.warning(f"[E_AOE] 子工作流执行超时: swf={sub_workflow_id}, session_id={req.session_id}")
        return {
            "status": "timeout",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": f"子工作流执行超时（{timeout}s）",
        }

    except asyncio.CancelledError:
        logger.info(f"[E_AOE] 子工作流被取消: swf={sub_workflow_id}, session_id={req.session_id}")
        return {
            "status": "cancelled",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": "子工作流已取消",
        }

    except Exception as e:
        logger.error(f"[E_AOE] 子工作流执行失败: swf={sub_workflow_id}, error={e}")
        return {
            "status": "error",
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "sub_workflow_id": sub_workflow_id,
            "result": f"子工作流执行失败: {str(e)}",
        }


@app.post("/orchestration/dispatch")
async def dispatch_subtask(req: DispatchRequest):
    """
    兼容旧接口：编排期先注册，再运行期执行。
    """
    register_result = await register_subworkflow(
        RegisterSubWorkflowRequest(
            subtask=req.subtask,
            session_id=req.session_id,
            source_aoe_url=req.source_aoe_url,
        )
    )
    if register_result.get("status") == "rejected":
        return register_result

    return await execute_subworkflow(
        register_result["sub_workflow_id"],
        ExecuteSubWorkflowRequest(
            session_id=req.session_id,
            source_aoe_url=req.source_aoe_url,
            timeout_seconds=int(req.subtask.get("timeout_seconds", 60)),
        ),
    )


# ============================================================
# 跨主体编排端点（E_AOE 角色）
# ============================================================

@app.post("/orchestration/dispatch")
async def dispatch_subtask(req: DispatchRequest):
    """
    接收来自 T_AOE 的跨主体子任务分发。

    流程（对应接口文档 §2.2）：
    1. E_AOE 接收子任务描述
    2. 在本地启动 distributed_workflow 执行子任务
    3. 返回 workflow_handle + 执行结果

    Args:
        req.subtask:         子任务图描述，包含 task_description, timeout_seconds 等
        req.session_id:      会话 ID，用于后续清理
        req.source_aoe_url:  T_AOE 的地址（记录用）
    """
    task_id = req.subtask.get("task_id", _uuid.uuid4().hex[:8])
    task_desc = req.subtask.get("task_description", "")
    timeout = int(req.subtask.get("timeout_seconds", 60))
    sub_workflow_id = req.subtask.get("sub_workflow_id", "")

    logger.info(
        f"[E_AOE] 收到跨主体子任务: task_id={task_id}, "
        f"source={req.source_aoe_url}, "
        f"{'swf=' + sub_workflow_id if sub_workflow_id else ''}"
        f"desc={task_desc[:80]}"
    )

    workflow_handle = f"remote_wf_{task_id}_{_uuid.uuid4().hex[:6]}"

    from src.distributed_workflow import run_distributed_workflow

    # 子工作流路径：查找本地预定义的 pipeline 拓扑
    pipeline_topology = []
    if sub_workflow_id:
        from src.service.agent_registry import get_registry_client
        swf_def = get_registry_client().get_sub_workflow_by_id(sub_workflow_id)
        if swf_def:
            pipeline_topology = swf_def["pipeline"]
            logger.info(
                f"[E_AOE] 子工作流模式: swf_id={sub_workflow_id}, "
                f"pipeline={len(pipeline_topology)} 步"
            )
        else:
            logger.warning(
                f"[E_AOE] 子工作流 {sub_workflow_id} 未在本地注册，"
                f"降级为自适应编排"
            )

    # 用 create_task 包装，使 DELETE /session/{id} 能通过 task.cancel() 中断执行
    workflow_task = asyncio.create_task(
        run_distributed_workflow(
            user_input=task_desc,
            adaptive_mode=(not pipeline_topology),
            timeout_seconds=timeout,
            pipeline_topology=pipeline_topology,
        )
    )
    _session_registry.register(req.session_id, workflow_handle, task=workflow_task)

    try:
        # shield 保护 task 不被 wait_for 超时取消；外部 task.cancel() 仍可穿透
        result = await asyncio.wait_for(
            asyncio.shield(workflow_task),
            timeout=float(timeout),
        )
        logger.info(f"[E_AOE] 子任务完成: task_id={task_id}")
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "completed",
            "result": str(result)[:3000],
        }

    except asyncio.TimeoutError:
        workflow_task.cancel()
        logger.warning(f"[E_AOE] 子任务超时: task_id={task_id}")
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "timeout",
            "result": f"子任务执行超时（{timeout}s）",
        }

    except asyncio.CancelledError:
        # 由 DELETE /orchestration/session/{id} 触发的主动取消
        logger.info(f"[E_AOE] 子任务被主动取消: task_id={task_id}")
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "cancelled",
            "result": "子任务已被终止（stop_app 触发）",
        }

    except Exception as e:
        logger.error(f"[E_AOE] 子任务失败: task_id={task_id}, error={e}")
        return {
            "task_id": task_id,
            "session_id": req.session_id,
            "workflow_handle": workflow_handle,
            "status": "error",
            "result": f"子任务执行失败: {str(e)}",
        }


@app.delete("/orchestration/session/{session_id}")
async def close_session(session_id: str):
    """
    接收 T_AOE 的会话清理通知：
    1. 取消正在运行的工作流 Task（§2.4 跨主体停止编排核心）
    2. 退订对应 Agent 实例的 ALCM 引用计数

    对应接口文档 §2.2 / §2.4
    """
    # 先取消 Task（若仍在运行）
    cancelled = _session_registry.cancel_session(session_id)
    if cancelled:
        logger.info(f"[E_AOE] 工作流 Task 已取消: session_id={session_id}")

    workflow_handle = _session_registry.unregister(session_id)
    if not workflow_handle:
        return {"status": "not_found", "session_id": session_id}

    try:
        from src.runtime.lifecycle_manager import get_lifecycle_manager
        alcm = get_lifecycle_manager()
        # 退订所有订阅了此 workflow_handle 的实例
        for instance_id in list(alcm._instances.keys()):
            instance = alcm._instances.get(instance_id)
            if instance and workflow_handle in getattr(instance, "subscribers", []):
                remaining = alcm.unsubscribe(instance_id, workflow_handle)
                logger.info(
                    f"[E_AOE] 退订实例: instance={instance_id}, "
                    f"refs_remaining={remaining}"
                )
        logger.info(
            f"[E_AOE] 会话清理完成: session_id={session_id}, "
            f"workflow={workflow_handle}"
        )
    except Exception as e:
        logger.warning(f"[E_AOE] ALCM 退订失败（非关键）: {e}")

    return {
        "status": "closed",
        "session_id": session_id,
        "workflow_handle": workflow_handle,
    }

@app.on_event("startup")
async def _start_gossip():
    """
    E_AOE 节点启动时，若配置了 PEER_AOE_URLS 则自动加入 gossip 网络。

    环境变量：
      PEER_AOE_URLS   逗号分隔的 peer 地址，如 http://192.168.1.10:8000
      LOCAL_AOE_URL   本节点地址，默认 http://localhost:{port}
      GOSSIP_INTERVAL 推送间隔（秒），默认 30
    """
    peer_urls_raw = os.getenv("PEER_AOE_URLS", "")
    peer_urls = [u.strip() for u in peer_urls_raw.split(",") if u.strip()]
    local_url = os.getenv("LOCAL_AOE_URL", "http://localhost:8080")
    interval = int(os.getenv("GOSSIP_INTERVAL", "30"))

    from src.service.agent_registry import get_registry_client
    registry = get_registry_client()
    await registry.start_gossip_background(
        peer_urls=peer_urls,
        local_url=local_url,
        interval=interval,
    )


@app.on_event("startup")
async def _start_qos_asd_feedback():
    """
    QoS → ASD 反馈闭环：节点启动时向 QoSMonitor 注册告警回调。

    当某 Agent 的 QoS 指标超过阈值（延迟过高或成功率过低）时，
    回调自动触发 AgentScheduler.redeploy_agent()，冷却窗口内重复告警只触发一次。

    冷却时间：REDEPLOY_COOLDOWN_SECS 环境变量，默认 60 秒。
    """
    import threading
    import time as _time
    from src.runtime.qos_monitor import get_qos_monitor
    from src.service.agent_scheduler import get_agent_scheduler

    cooldown_secs = int(os.getenv("REDEPLOY_COOLDOWN_SECS", "60"))
    _redeploy_cooldown: dict[str, float] = {}

    def _qos_redeploy_cb(agent_id: str, metrics) -> None:
        now = _time.monotonic()
        last = _redeploy_cooldown.get(agent_id, 0.0)
        if now - last < cooldown_secs:
            logger.warning(
                f"[QoS→ASD] ⏳ 冷却期内跳过重部署: agent_id={agent_id}, "
                f"remaining={cooldown_secs - (now - last):.0f}s"
            )
            return
        _redeploy_cooldown[agent_id] = now
        logger.info(f"[QoS→ASD] 🔁 触发自动重部署: agent_id={agent_id}")

        def _do_redeploy():
            try:
                get_agent_scheduler().redeploy_agent(agent_id)
            except Exception as exc:
                logger.error(f"[QoS→ASD] 重部署异常: agent_id={agent_id}, err={exc}")

        threading.Thread(target=_do_redeploy, daemon=True, name=f"redeploy-{agent_id}").start()

    get_qos_monitor().register_alert_callback(_qos_redeploy_cb)
    logger.info("[QoS→ASD] ✅ QoS 告警回调已注册")


if __name__ == "__main__":
    import uvicorn
    import sys

    # 从命令行参数获取端口，默认 8080
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    # 使用 0.0.0.0 监听所有网络接口，或使用 127.0.0.1 仅本地访问
    uvicorn.run(app, host="0.0.0.0", port=port)