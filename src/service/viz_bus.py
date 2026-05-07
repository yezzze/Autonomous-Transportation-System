"""
可视化工作流总线 (in-memory, asyncio-safe)
==========================================

集中存储所有"正在执行 / 最近结束"的分布式工作流的最新 state 快照,
供 visualization API + WebSocket 订阅。

核心数据:
  workflows: { workflow_id : WorkflowEntry }
  WorkflowEntry:
    id            : str
    title         : str            (用户输入摘要 / 应用名)
    status        : running | done | failed
    started_at    : float
    updated_at    : float
    state         : dict           DistributedState 浅拷贝(含 execution_plan 等)
    history       : List[event]    保留最近 N 条节点事件

事件订阅:
  每个 workflow 维护一个 asyncio.Queue 列表,WS 客户端订阅后从队列读取增量。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

MAX_HISTORY = 200


@dataclass
class WorkflowEntry:
    id: str
    title: str = ""
    status: str = "running"          # running | done | failed | cancelled
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    state: Dict[str, Any] = field(default_factory=dict)
    node_history: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    # WebSocket 订阅队列
    subscribers: Set[asyncio.Queue] = field(default_factory=set)

    def to_summary(self) -> Dict[str, Any]:
        plan = self.state.get("execution_plan", []) or []
        return {
            "id": self.id,
            "title": self.title or self.id,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "elapsed": round(self.updated_at - self.started_at, 2),
            "task_total": len(plan),
            "task_completed": sum(1 for t in plan if t.get("status") == "completed"),
            "task_running": sum(1 for t in plan if t.get("status") == "running"),
            "task_failed": sum(1 for t in plan if t.get("status") == "failed"),
            "complexity_level": self.state.get("complexity_level", ""),
            "orchestration_mode": self.state.get("orchestration_mode", ""),
            "error": self.error,
        }


class VizBus:
    """全局单例。"""
    def __init__(self) -> None:
        self.workflows: Dict[str, WorkflowEntry] = {}
        self.lock = asyncio.Lock()
        # 全局订阅(关心整个工作流列表变化)
        self.global_subscribers: Set[asyncio.Queue] = set()
        # 内置 demo 工作流的 id (前端可手动选择)
        self._demo_id: Optional[str] = None

    # ---------- 注册 / 更新 / 结束 ----------

    def register(self, title: str = "", workflow_id: Optional[str] = None) -> str:
        wid = workflow_id or f"wf_{uuid.uuid4().hex[:8]}"
        entry = WorkflowEntry(id=wid, title=title)
        self.workflows[wid] = entry
        logger.info(f"[viz_bus] register workflow id={wid}  title={title!r}")
        self._publish_global({"type": "workflow_registered", "id": wid, "title": title})
        return wid

    def update_state(self, workflow_id: str, state: Dict[str, Any],
                     node_name: Optional[str] = None) -> None:
        e = self.workflows.get(workflow_id)
        if not e:
            return
        # 浅拷贝即可,DistributedState 字段都是 list/dict/标量
        e.state = self._snapshot(state)
        e.updated_at = time.time()
        if node_name:
            e.node_history.append({
                "node": node_name,
                "ts": e.updated_at,
                "task_index": state.get("current_task_index"),
            })
            if len(e.node_history) > MAX_HISTORY:
                e.node_history.pop(0)
        # 推给该 wf 的订阅者
        self._publish_workflow(workflow_id, {"type": "state_update", "node": node_name})
        # 同时通知全局订阅者(列表里有进度变化)
        self._publish_global({"type": "workflow_progress", "id": workflow_id})

    def finish(self, workflow_id: str, status: str = "done",
               final_state: Optional[Dict[str, Any]] = None,
               error: Optional[str] = None) -> None:
        e = self.workflows.get(workflow_id)
        if not e:
            return
        if final_state is not None:
            e.state = self._snapshot(final_state)
        e.status = status
        e.error = error
        e.updated_at = time.time()
        logger.info(f"[viz_bus] finish workflow id={workflow_id}  status={status}")
        self._publish_workflow(workflow_id, {"type": "workflow_finished", "status": status})
        self._publish_global({"type": "workflow_finished", "id": workflow_id, "status": status})

    def cancel(self, workflow_id: str) -> bool:
        if workflow_id not in self.workflows:
            return False
        self.finish(workflow_id, status="cancelled")
        return True

    def get(self, workflow_id: str) -> Optional[WorkflowEntry]:
        return self.workflows.get(workflow_id)

    def list_workflows(self, limit: int = 50) -> List[Dict[str, Any]]:
        items = sorted(self.workflows.values(), key=lambda e: e.started_at, reverse=True)
        return [e.to_summary() for e in items[:limit]]

    def latest_running(self) -> Optional[WorkflowEntry]:
        running = [e for e in self.workflows.values() if e.status == "running"]
        if not running:
            return None
        return max(running, key=lambda e: e.started_at)

    def latest_any(self) -> Optional[WorkflowEntry]:
        if not self.workflows:
            return None
        return max(self.workflows.values(), key=lambda e: e.updated_at)

    # ---------- Demo 工作流 ----------

    def set_demo_id(self, wid: str) -> None:
        self._demo_id = wid

    def get_demo_id(self) -> Optional[str]:
        return self._demo_id

    # ---------- 订阅 ----------

    def subscribe_workflow(self, workflow_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        e = self.workflows.get(workflow_id)
        if e:
            e.subscribers.add(q)
        return q

    def unsubscribe_workflow(self, workflow_id: str, q: asyncio.Queue) -> None:
        e = self.workflows.get(workflow_id)
        if e and q in e.subscribers:
            e.subscribers.discard(q)

    def subscribe_global(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.global_subscribers.add(q)
        return q

    def unsubscribe_global(self, q: asyncio.Queue) -> None:
        self.global_subscribers.discard(q)

    # ---------- 内部 ----------

    def _publish_workflow(self, workflow_id: str, msg: Dict[str, Any]) -> None:
        e = self.workflows.get(workflow_id)
        if not e:
            return
        for q in list(e.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def _publish_global(self, msg: Dict[str, Any]) -> None:
        for q in list(self.global_subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    @staticmethod
    def _snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
        """轻量快照:只保留可视化需要的字段,避免大对象/不可序列化对象。"""
        keep = (
            "skills_content", "pipeline_topology", "complexity_level",
            "orchestration_mode", "agent_registry_cache", "execution_plan",
            "current_task_index", "all_tasks_completed", "failed_tasks",
            "cross_host_sessions", "failed_remote_aoe_urls",
            "magentic_round", "magentic_max_round", "magentic_stall_count",
            "magentic_mode", "progress_ledger",
            "replanning_count", "last_replan_reason", "registry_last_update",
        )
        out: Dict[str, Any] = {}
        for k in keep:
            if k in state:
                out[k] = state[k]
        return out


# 全局单例
_BUS: Optional[VizBus] = None


def get_viz_bus() -> VizBus:
    global _BUS
    if _BUS is None:
        _BUS = VizBus()
    return _BUS
