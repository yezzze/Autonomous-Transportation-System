"""
可视化路由 (挂载到主 server.py / 端口 8000)
==============================================

提供:
  GET  /viz                              H5 页面
  GET  /viz/static/*                     静态资源(可选,目前只用 CDN)
  GET  /api/viz/workflows                所有工作流列表(running + 历史)
  GET  /api/viz/workflows/{wf_id}/full   单工作流的三场景快照
  GET  /api/viz/workflows/{wf_id}/orchestration
  GET  /api/viz/workflows/{wf_id}/topology
  GET  /api/viz/workflows/{wf_id}/execution
  WS   /ws/viz/workflows                 全局工作流变更通知
  WS   /ws/viz/workflows/{wf_id}         订阅单个工作流的实时 state
  POST /api/viz/demo/start               注册一个内置 demo 工作流
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from src.api.visualization import (
    extract_execution_data,
    extract_full_view,
    extract_orchestration_data,
    extract_topology_data,
)
from src.service.viz_bus import get_viz_bus

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent  # project root
STATIC_DIR = ROOT / "static"

router = APIRouter(tags=["visualization"])


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@router.get("/viz", include_in_schema=False)
async def viz_page():
    html = STATIC_DIR / "visualization.html"
    if not html.exists():
        raise HTTPException(404, f"visualization.html not found at {html}")
    return FileResponse(html)


# ---------------------------------------------------------------------------
# 工作流列表
# ---------------------------------------------------------------------------

@router.get("/api/viz/workflows")
async def list_workflows(limit: int = 50):
    bus = get_viz_bus()
    return {"workflows": bus.list_workflows(limit=limit), "ts": time.time()}


def _resolve_state(wf_id: str) -> Dict[str, Any]:
    bus = get_viz_bus()
    if wf_id == "latest":
        e = bus.latest_running() or bus.latest_any()
    elif wf_id == "demo":
        demo_id = bus.get_demo_id()
        e = bus.get(demo_id) if demo_id else None
    else:
        e = bus.get(wf_id)
    if not e:
        raise HTTPException(404, f"workflow {wf_id} not found")
    return e.state


@router.get("/api/viz/workflows/{wf_id}/full")
async def workflow_full(wf_id: str):
    state = _resolve_state(wf_id)
    return extract_full_view(state)


@router.get("/api/viz/workflows/{wf_id}/orchestration")
async def workflow_orchestration(wf_id: str):
    return extract_orchestration_data(_resolve_state(wf_id))


@router.get("/api/viz/workflows/{wf_id}/topology")
async def workflow_topology(wf_id: str):
    return extract_topology_data(_resolve_state(wf_id))


@router.get("/api/viz/workflows/{wf_id}/execution")
async def workflow_execution(wf_id: str):
    return extract_execution_data(_resolve_state(wf_id))


# ---------------------------------------------------------------------------
# WebSocket: 全局工作流列表变更
# ---------------------------------------------------------------------------

@router.websocket("/ws/viz/workflows")
async def ws_workflow_list(ws: WebSocket):
    await ws.accept()
    bus = get_viz_bus()
    q = bus.subscribe_global()
    try:
        # 首屏先推一次列表
        await ws.send_json({"type": "list", "workflows": bus.list_workflows()})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                # 任何事件都触发刷新列表(简单粗暴,但很省心)
                await ws.send_json({"type": "list", "workflows": bus.list_workflows(), "event": msg})
            except asyncio.TimeoutError:
                # 心跳
                await ws.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe_global(q)


# ---------------------------------------------------------------------------
# WebSocket: 单个工作流的实时 state
# ---------------------------------------------------------------------------

@router.websocket("/ws/viz/workflows/{wf_id}")
async def ws_workflow_state(ws: WebSocket, wf_id: str):
    await ws.accept()
    bus = get_viz_bus()

    # 解析 latest / demo
    real_id = wf_id
    if wf_id in ("latest", "demo"):
        e = bus.latest_running() or bus.latest_any() if wf_id == "latest" else bus.get(bus.get_demo_id() or "")
        if not e:
            await ws.send_json({"type": "error", "message": f"no workflow for {wf_id}"})
            await ws.close()
            return
        real_id = e.id

    if not bus.get(real_id):
        await ws.send_json({"type": "error", "message": f"workflow {real_id} not found"})
        await ws.close()
        return

    q = bus.subscribe_workflow(real_id)
    try:
        # 立即推一份完整快照
        e = bus.get(real_id)
        if e:
            await ws.send_json({
                "type": "snapshot",
                "workflow_id": real_id,
                "data": extract_full_view(e.state),
                "summary": e.to_summary(),
                "ts": time.time(),
            })
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                e = bus.get(real_id)
                if not e:
                    break
                await ws.send_json({
                    "type": "snapshot",
                    "workflow_id": real_id,
                    "event": msg,
                    "data": extract_full_view(e.state),
                    "summary": e.to_summary(),
                    "ts": time.time(),
                })
                if e.status in {"done", "failed", "cancelled"}:
                    # 推完最后一帧后保持连接,前端可以继续观察其他工作流
                    pass
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe_workflow(real_id, q)


# ---------------------------------------------------------------------------
# Demo: 在主服务里也能跑一份内置演示工作流
# ---------------------------------------------------------------------------

@router.post("/api/viz/demo/start")
async def demo_start():
    """注册一个内置 demo 工作流(不依赖真实 LangGraph),用于无任务时演示动画。"""
    from visualization_server import build_initial_demo_state, _demo_run_cluster  # 复用 demo 引擎
    bus = get_viz_bus()
    state = build_initial_demo_state()
    title = "[Demo] 7 任务流水线演示"
    wf_id = bus.register(title=title)
    bus.set_demo_id(wf_id)
    bus.update_state(wf_id, state, node_name="__init__")

    async def run():
        plan = state["execution_plan"]
        i = 0
        while i < len(plan):
            pg = plan[i].get("parallel_group", "")
            if pg:
                cluster, j = [i], i + 1
                while j < len(plan) and plan[j].get("parallel_group", "") == pg:
                    cluster.append(j)
                    j += 1
                await _demo_step(bus, wf_id, state, cluster)
                i = j
            else:
                await _demo_step(bus, wf_id, state, [i])
                i += 1
        state["all_tasks_completed"] = True
        state["current_task_index"] = len(plan)
        bus.finish(wf_id, status="done", final_state=state)

    asyncio.create_task(run())
    return {"workflow_id": wf_id, "status": "started"}


async def _demo_step(bus, wf_id: str, state: Dict[str, Any], indices):
    """复用 visualization_server 中的演示推进逻辑,但不依赖该模块的私有变量。"""
    plan = state["execution_plan"]
    DEMO_TOOL_LIBRARY = {
        "search":    ["mcp.tavily.search"],
        "crawl":     ["mcp.jina.read"],
        "vision":    ["a2a.vl_model.describe_image"],
        "rag":       ["a2a.rag.retrieve", "a2a.rag.rerank"],
        "code":      ["mcp.python_repl.exec"],
        "translate": ["a2a.qwen.translate"],
        "report":    ["a2a.report.compose", "a2a.markdown.render"],
        "shell":     ["mcp.bash.run"],
    }
    for k in indices:
        plan[k]["status"] = "running"
        state["current_task_index"] = k
    bus.update_state(wf_id, state, node_name="executor")
    await asyncio.sleep(1.0)
    for k in indices:
        cap = plan[k]["metadata"]["executor"]
        for tool in DEMO_TOOL_LIBRARY.get(cap, ["mcp.unknown.run"]):
            plan[k]["metadata"]["tools_called"].append({
                "tool": tool, "ts": time.strftime("%H:%M:%S"), "ok": True,
            })
            bus.update_state(wf_id, state, node_name="executor")
            await asyncio.sleep(0.5)
    for k in indices:
        plan[k]["status"] = "completed"
        plan[k]["result"] = f"[demo] {plan[k]['task_title']} 执行完毕"
        plan[k]["metadata"]["duration_ms"] = 1500
    bus.update_state(wf_id, state, node_name="executor")
    await asyncio.sleep(0.4)


@router.post("/api/viz/demo/reset")
async def demo_reset():
    bus = get_viz_bus()
    demo_id = bus.get_demo_id()
    if demo_id and bus.get(demo_id):
        bus.cancel(demo_id)
    bus.set_demo_id(None)
    return {"status": "reset"}
