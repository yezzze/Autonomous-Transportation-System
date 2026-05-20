"""
LangManus 可视化服务 (独立)
==============================

启动: python visualization_server.py [--port 8888] [--mode demo|live]

提供:
  - GET  /                            返回 H5 单页
  - GET  /api/viz/skills              当前 Skills.md 原文
  - GET  /api/viz/orchestration       场景1 数据
  - GET  /api/viz/topology            场景2 数据
  - GET  /api/viz/execution           场景3 数据
  - GET  /api/viz/full                三场景合并快照
  - WS   /ws/viz                      实时推送(每秒推一次完整快照)
  - POST /api/viz/demo/start          启动演示流(无需后端)
  - POST /api/viz/demo/reset          复位演示

数据来源策略:
  mode=live  → 优先调用 src.api.visualization 的提取函数,
               读取最近一次执行的 state(全局缓存 LATEST_STATE)
  mode=demo  → 走内置模拟引擎,逐步推进任务状态,展示动画效果
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --- 项目内引用 ----------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from src.api.visualization import (  # noqa: E402
    extract_execution_data,
    extract_full_view,
    extract_orchestration_data,
    extract_topology_data,
)

logger = logging.getLogger("viz_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="LangManus Visualization", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ===========================================================================
# 全局状态: 既支持 live (外部注入 state),也支持 demo (内置引擎)
# ===========================================================================

class VizContext:
    def __init__(self) -> None:
        self.mode: str = "demo"            # demo | live
        self.latest_state: Dict[str, Any] = {}
        self.demo_task: Optional[asyncio.Task] = None
        self.connections: Set[WebSocket] = set()

    def set_state(self, state: Dict[str, Any]) -> None:
        self.latest_state = state

    def snapshot(self) -> Dict[str, Any]:
        if not self.latest_state:
            self.latest_state = build_initial_demo_state()
        return extract_full_view(self.latest_state)


CTX = VizContext()


# ===========================================================================
# 演示引擎: 构造一份合理的 DistributedState,然后异步推进任务进度
# ===========================================================================

DEMO_AGENTS = [
    {"id": "search-agent",     "ip": "127.0.0.1",     "port": 9001, "capability": "search",   "status": "online", "description": "Web 搜索 / Tavily"},
    {"id": "crawl-agent",      "ip": "127.0.0.1",     "port": 9002, "capability": "crawl",    "status": "online", "description": "网页抓取 / Jina"},
    {"id": "code-agent",       "ip": "127.0.0.1",     "port": 9003, "capability": "code",     "status": "online", "description": "Python REPL"},
    {"id": "shell-agent",      "ip": "127.0.0.1",     "port": 9004, "capability": "shell",    "status": "online", "description": "Bash 命令执行"},
    {"id": "vision-agent",     "ip": "10.10.10.21",   "port": 9100, "capability": "vision",   "status": "online", "description": "远端视觉理解 (VL Model)"},
    {"id": "rag-agent",        "ip": "10.10.10.22",   "port": 9101, "capability": "rag",      "status": "online", "description": "远端 RAG 知识检索"},
    {"id": "report-agent",     "ip": "10.10.10.23",   "port": 9102, "capability": "report",   "status": "online", "description": "远端报告生成"},
    {"id": "translate-agent",  "ip": "10.10.10.24",   "port": 9103, "capability": "translate","status": "busy",   "description": "远端翻译 (繁忙)"},
]

DEMO_SKILLS_MD = """\
# Skills

## 任务目标
分析小红书 2024Q4 用户增长情况,生成中英双语报告。

## Pipeline (固定拓扑)
1. search       — 搜索行业数据
2. crawl        — 抓取重点页面
3. [parallel]
   - vision     — 解析图表截图
   - rag        — 内部知识库召回
4. code         — 数据建模 / 计算关键指标
5. translate    — 中英翻译
6. report       — 生成最终报告

## 约束
- 总耗时 < 5 min
- 远端节点超时阈值 30s
"""

DEMO_PIPELINE_TOPOLOGY = [
    {"step": "search",    "capability": "search"},
    {"step": "crawl",     "capability": "crawl"},
    {"step": "vision",    "capability": "vision",    "parallel_group": "pg-analyze"},
    {"step": "rag",       "capability": "rag",       "parallel_group": "pg-analyze"},
    {"step": "code",      "capability": "code"},
    {"step": "translate", "capability": "translate"},
    {"step": "report",    "capability": "report"},
]


def build_initial_demo_state() -> Dict[str, Any]:
    """生成一个完整 state 骨架,plan 全部 pending。"""
    plan = []
    for i, step in enumerate(DEMO_PIPELINE_TOPOLOGY):
        agent = next((a for a in DEMO_AGENTS if a["capability"] == step["capability"]), DEMO_AGENTS[0])
        plan.append({
            "task_id": f"t{i+1}-{step['step']}",
            "task_title": f"Step {i+1}: {step['step']}",
            "task_description": f"调用 {agent['id']} 完成 {step['step']} 任务",
            "assigned_agent_id": agent["id"],
            "target_ip": agent["ip"],
            "target_port": agent["port"],
            "status": "pending",
            "result": "",
            "retry_count": 0,
            "parallel_group": step.get("parallel_group", ""),
            "metadata": {
                "protocol": "mcp" if agent["capability"] in {"search", "crawl", "code", "shell"} else "a2a",
                "executor": agent["capability"],
                "tools_called": [],
            },
        })
    cross = {t["task_id"]: f"http://{t['target_ip']}:{t['target_port']}"
             for t in plan if t["target_ip"] not in {"127.0.0.1", "localhost"}}
    return {
        "skills_content": DEMO_SKILLS_MD,
        "pipeline_topology": DEMO_PIPELINE_TOPOLOGY,
        "complexity_level": "medium",
        "agent_registry_cache": DEMO_AGENTS,
        "execution_plan": plan,
        "current_task_index": 0,
        "all_tasks_completed": False,
        "failed_tasks": [],
        "cross_host_sessions": cross,
        "failed_remote_aoe_urls": {},
        "magentic_round": 0,
        "magentic_max_round": 0,
        "magentic_stall_count": 0,
        "magentic_mode": "",
        "progress_ledger": {},
        "replanning_count": 0,
        "last_replan_reason": "",
    }


DEMO_TOOL_LIBRARY = {
    "search":    [["mcp.tavily.search", "mcp.serper.search"]],
    "crawl":     [["mcp.jina.read"]],
    "vision":    [["a2a.vl_model.describe_image"]],
    "rag":       [["a2a.rag.retrieve", "a2a.rag.rerank"]],
    "code":      [["mcp.python_repl.exec"]],
    "translate": [["a2a.qwen.translate"]],
    "report":    [["a2a.report.compose", "a2a.markdown.render"]],
    "shell":     [["mcp.bash.run"]],
}


async def demo_engine_loop() -> None:
    """逐步推进任务: pending → running → completed,展示进度条与高亮。"""
    state = build_initial_demo_state()
    CTX.set_state(state)
    await broadcast_snapshot()

    plan = state["execution_plan"]
    n = len(plan)

    # 按 (parallel_group | index) 分簇执行
    i = 0
    state["current_task_index"] = 0
    while i < n:
        pg = plan[i].get("parallel_group", "")
        if pg:
            cluster = []
            j = i
            while j < n and plan[j].get("parallel_group", "") == pg:
                cluster.append(j)
                j += 1
            await _demo_run_cluster(state, cluster)
            i = j
        else:
            await _demo_run_cluster(state, [i])
            i += 1
        # 推进当前指针到下一个未完成任务
        for k in range(n):
            if plan[k]["status"] in {"pending", "running"}:
                state["current_task_index"] = k
                break
        else:
            state["current_task_index"] = n
    state["all_tasks_completed"] = True
    state["current_task_index"] = n
    await broadcast_snapshot()
    logger.info("✅ Demo 流程完成")


async def _demo_run_cluster(state: Dict[str, Any], indices: List[int]) -> None:
    """一组并行/单个任务的演示执行: running → tools → completed"""
    plan = state["execution_plan"]

    # 标记 running
    for k in indices:
        plan[k]["status"] = "running"
        state["current_task_index"] = k
    await broadcast_snapshot()
    await asyncio.sleep(1.2)

    # 注入 MCP 工具调用过程
    for k in indices:
        cap = plan[k]["metadata"]["executor"]
        tool_chain = DEMO_TOOL_LIBRARY.get(cap, [["mcp.unknown.run"]])[0]
        for tool in tool_chain:
            plan[k]["metadata"]["tools_called"].append({
                "tool": tool,
                "ts": time.strftime("%H:%M:%S"),
                "ok": True,
            })
            await broadcast_snapshot()
            await asyncio.sleep(0.6)

    # 标记 completed
    for k in indices:
        plan[k]["status"] = "completed"
        plan[k]["result"] = f"[demo] {plan[k]['task_title']} 执行完毕,产出 OK"
        plan[k]["metadata"]["duration_ms"] = 1800
    await broadcast_snapshot()
    await asyncio.sleep(0.5)


# ===========================================================================
# WebSocket 广播
# ===========================================================================

async def broadcast_snapshot() -> None:
    if not CTX.connections:
        return
    payload = {"type": "snapshot", "data": CTX.snapshot(), "ts": time.time()}
    dead = []
    for ws in list(CTX.connections):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CTX.connections.discard(ws)


# ===========================================================================
# HTTP / WS 端点
# ===========================================================================

@app.get("/")
async def index():
    html = STATIC_DIR / "visualization.html"
    if not html.exists():
        raise HTTPException(404, "visualization.html not found")
    return FileResponse(html)


@app.get("/api/viz/skills")
async def get_skills():
    state = CTX.latest_state or build_initial_demo_state()
    return {"skills_content": state.get("skills_content", "")}


@app.get("/api/viz/orchestration")
async def api_orchestration():
    state = CTX.latest_state or build_initial_demo_state()
    return extract_orchestration_data(state)


@app.get("/api/viz/topology")
async def api_topology():
    state = CTX.latest_state or build_initial_demo_state()
    return extract_topology_data(state)


@app.get("/api/viz/execution")
async def api_execution():
    state = CTX.latest_state or build_initial_demo_state()
    return extract_execution_data(state)


@app.get("/api/viz/full")
async def api_full():
    return CTX.snapshot()


@app.get("/api/viz/mode")
async def get_mode():
    return {"mode": CTX.mode}


@app.post("/api/viz/mode/{mode}")
async def set_mode(mode: str):
    if mode not in {"demo", "live"}:
        raise HTTPException(400, "mode must be demo or live")
    CTX.mode = mode
    return {"mode": CTX.mode}


@app.post("/api/viz/demo/start")
async def demo_start():
    if CTX.demo_task and not CTX.demo_task.done():
        return {"status": "already_running"}
    CTX.mode = "demo"
    CTX.demo_task = asyncio.create_task(demo_engine_loop())
    return {"status": "started"}


@app.post("/api/viz/demo/reset")
async def demo_reset():
    if CTX.demo_task and not CTX.demo_task.done():
        CTX.demo_task.cancel()
    CTX.set_state(build_initial_demo_state())
    await broadcast_snapshot()
    return {"status": "reset"}


@app.post("/api/viz/state/push")
async def push_state(state: Dict[str, Any]):
    """live 模式下,外部 (server.py / agent_server.py) 调用此端点上报最新 state。"""
    CTX.mode = "live"
    CTX.set_state(state)
    await broadcast_snapshot()
    return {"status": "ok", "tasks": len(state.get("execution_plan", []))}


@app.websocket("/ws/viz")
async def ws_viz(ws: WebSocket):
    await ws.accept()
    CTX.connections.add(ws)
    logger.info(f"🔌 WS connected, total={len(CTX.connections)}")
    try:
        # 立即推一份快照
        await ws.send_json({"type": "snapshot", "data": CTX.snapshot(), "ts": time.time()})
        while True:
            msg = await ws.receive_text()
            # 简单 ping/pong
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        CTX.connections.discard(ws)
        logger.info(f"🔌 WS disconnected, total={len(CTX.connections)}")


# ===========================================================================
# 入口
# ===========================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("VIZ_PORT", 8888)))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--auto-demo", action="store_true",
                        help="启动后自动开始 demo 演示流")
    args = parser.parse_args()

    CTX.mode = args.mode
    CTX.set_state(build_initial_demo_state())

    if args.auto_demo:
        @app.on_event("startup")
        async def _autostart():
            CTX.demo_task = asyncio.create_task(demo_engine_loop())

    import uvicorn
    logger.info(f"🚀 Visualization Server  http://{args.host}:{args.port}  (mode={args.mode})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
