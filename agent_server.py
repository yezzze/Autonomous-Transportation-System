# agent_server.py - Agent 节点服务（支持 A2A 协议 + 跨主体编排端点）

import asyncio
import logging
import uuid as _uuid
from typing import Optional

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


@app.post("/registry/sync")
async def registry_sync(req: RegistrySyncRequest):
    """
    接收来自 T_AOE / peer 节点的 gossip 推送，合并到本节点注册表。

    对应架构 §1 智能体发现（ARDC Gossip）：
    - 来源节点 POST {source_url, agents} 到本端点
    - 本节点调用 registry.sync_from_peer() 更新 peer 缓存
    - 返回合并后 peer agents 总数
    """
    from src.service.agent_registry import get_registry_client

    registry = get_registry_client()
    merged_count = registry.sync_from_peer(req.source_url, req.agents)
    logger.info(
        f"[ARDC] /registry/sync 处理完成: source={req.source_url}, "
        f"agents={len(req.agents)}, merged_total={merged_count}"
    )
    return {
        "status": "ok",
        "source_url": req.source_url,
        "received_agents": len(req.agents),
        "merged_count": merged_count,
    }


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

    logger.info(
        f"[E_AOE] 收到跨主体子任务: task_id={task_id}, "
        f"source={req.source_aoe_url}, desc={task_desc[:80]}"
    )

    workflow_handle = f"remote_wf_{task_id}_{_uuid.uuid4().hex[:6]}"

    from src.distributed_workflow import run_distributed_workflow

    # 用 create_task 包装，使 DELETE /session/{id} 能通过 task.cancel() 中断执行
    workflow_task = asyncio.create_task(
        run_distributed_workflow(
            user_input=task_desc,
            adaptive_mode=True,
            timeout_seconds=timeout,
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