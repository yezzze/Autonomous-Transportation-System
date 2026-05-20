"""
可视化数据提取层
================

为 visualization_server.py 提供从 DistributedState 与 AgentRegistry 中
抽取三类视图所需的数据:

  - 场景 1 编排过程  : Skills 解析、可用 Agent 列表、本地/远端分组
  - 场景 2 拓扑结果  : 节点(任务) + 边(顺序/并行) + 平台标签
  - 场景 3 执行监控  : 当前 Agent、进度、协议级 MCP 调用

不修改任何原有节点逻辑,只读取 state 与 registry。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

LOCAL_HOSTS = {"127.0.0.1", "localhost", "host.docker.internal", "0.0.0.0"}


def _is_local_ip(ip: str) -> bool:
    return (ip or "").strip() in LOCAL_HOSTS


# ---------------------------------------------------------------------------
# 场景 1: 编排过程
# ---------------------------------------------------------------------------

def extract_orchestration_data(state: Dict[str, Any]) -> Dict[str, Any]:
    """提取编排过程视图所需数据。

    输出字段:
      - skills_content        : str        Skills.md 原文
      - pipeline_topology     : list       管道固定拓扑(若存在)
      - complexity_level      : str        simple/medium/complex
      - orchestration_mode    : str        Sequential/Concurrent/Magentic
      - available_agents      : list[dict] 全部候选 Agent
      - local_agents          : list[str]  本机 Agent ID
      - remote_agents         : list[str]  远端 Agent ID
      - selected_agents       : list[str]  已被 execution_plan 选中的 Agent ID
      - platform_groups       : dict       按平台(IP:port)分组的 Agent
    """
    # 延迟 import,允许 mock 模式下不依赖完整后端
    try:
        from src.service.agent_registry import get_registry_client
        registry = get_registry_client()
        all_agents = registry.get_all_agents() or []
    except Exception:
        all_agents = state.get("agent_registry_cache", []) or []

    execution_plan = state.get("execution_plan", []) or []
    selected = list({t.get("assigned_agent_id", "") for t in execution_plan if t.get("assigned_agent_id")})

    available = []
    local_ids: List[str] = []
    remote_ids: List[str] = []
    platform_groups: Dict[str, List[str]] = {}

    for a in all_agents:
        ip = a.get("ip", "")
        port = a.get("port", 0)
        platform_key = f"{ip}:{port}"
        is_local = _is_local_ip(ip)
        item = {
            "id": a.get("id", ""),
            "capability": a.get("capability", ""),
            "ip": ip,
            "port": port,
            "status": a.get("status", "online"),
            "description": a.get("description", ""),
            "platform": "local" if is_local else "remote",
            "platform_key": platform_key,
            "is_selected": a.get("id") in selected,
        }
        available.append(item)
        (local_ids if is_local else remote_ids).append(a.get("id", ""))
        platform_groups.setdefault(platform_key, []).append(a.get("id", ""))

    return {
        "skills_content": state.get("skills_content", "") or "",
        "pipeline_topology": state.get("pipeline_topology", []) or [],
        "complexity_level": state.get("complexity_level", "unknown"),
        "orchestration_mode": _infer_mode(state),
        "available_agents": available,
        "local_agents": local_ids,
        "remote_agents": remote_ids,
        "selected_agents": selected,
        "platform_groups": platform_groups,
        "registry_last_update": state.get("registry_last_update", ""),
    }


def _infer_mode(state: Dict[str, Any]) -> str:
    if state.get("magentic_round", 0) > 0 or state.get("progress_ledger"):
        return "Magentic-One"
    cl = state.get("complexity_level", "")
    if cl == "simple":
        return "Sequential"
    if cl == "medium":
        return "Concurrent"
    if cl == "complex":
        return "Magentic-One"
    return state.get("orchestration_mode") or "Adaptive"


# ---------------------------------------------------------------------------
# 场景 2: 拓扑图
# ---------------------------------------------------------------------------

def extract_topology_data(state: Dict[str, Any]) -> Dict[str, Any]:
    """构建可视化所需的拓扑节点 + 边。

    节点字段: id / index / title / agent_id / status / platform /
              ip / port / parallel_group / is_current / is_failed /
              remote_aoe_url / result(截断)
    边字段:   from / to / type(sequence|parallel_start|parallel_group)
    """
    plan = state.get("execution_plan", []) or []
    cross = state.get("cross_host_sessions", {}) or {}
    failed = set(state.get("failed_tasks", []) or [])
    failed_remote = state.get("failed_remote_aoe_urls", {}) or {}
    current_index = state.get("current_task_index", 0)

    nodes: List[Dict[str, Any]] = []
    for i, t in enumerate(plan):
        task_id = t.get("task_id", f"task_{i}")
        ip = t.get("target_ip", "")
        is_cross = task_id in cross
        result = t.get("result", "") or ""
        nodes.append({
            "id": task_id,
            "index": i,
            "title": t.get("task_title", task_id),
            "description": t.get("task_description", ""),
            "agent_id": t.get("assigned_agent_id", ""),
            "status": t.get("status", "pending"),
            "platform": "remote" if is_cross else ("local" if _is_local_ip(ip) else "remote"),
            "ip": ip,
            "port": t.get("target_port", 0),
            "parallel_group": t.get("parallel_group", "") or "",
            "remote_aoe_url": cross.get(task_id, ""),
            "is_current": i == current_index and t.get("status") == "running",
            "is_failed": (task_id in failed) or (t.get("status") == "failed"),
            "retry_count": t.get("retry_count", 0),
            "failed_remote_history": failed_remote.get(task_id, []),
            "result_preview": (result[:200] + "...") if len(result) > 200 else result,
        })

    edges = _build_edges(plan)

    counts = _count_status(plan)
    return {
        "nodes": nodes,
        "edges": edges,
        "current_task_index": current_index,
        "total": len(plan),
        "counts": counts,
        "cross_host_count": len(cross),
        "platforms": _group_by_platform(nodes),
    }


def _build_edges(plan: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """根据 parallel_group 推导拓扑边。

    规则:
      - parallel_group 相同 → 同组并行,以前一节点(非同组)为入口,组内首尾相连
      - 否则按下标顺序串行
    """
    edges: List[Dict[str, str]] = []
    if not plan:
        return edges

    def tid(i: int) -> str:
        return plan[i].get("task_id", f"task_{i}")

    last_finished = None  # 上一个串行/并行块的末尾节点 id 列表
    i = 0
    n = len(plan)
    while i < n:
        pg = plan[i].get("parallel_group", "") or ""
        if pg:
            # 收集所有相同 parallel_group 的连续任务
            group_idx = [i]
            j = i + 1
            while j < n and (plan[j].get("parallel_group", "") or "") == pg:
                group_idx.append(j)
                j += 1
            # 入边: 上一节点 → 组内每个节点
            if last_finished:
                for src in last_finished:
                    for k in group_idx:
                        edges.append({"from": src, "to": tid(k), "type": "parallel_start"})
            last_finished = [tid(k) for k in group_idx]
            i = j
        else:
            if last_finished:
                for src in last_finished:
                    edges.append({"from": src, "to": tid(i), "type": "sequence"})
            last_finished = [tid(i)]
            i += 1
    return edges


def _count_status(plan: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for t in plan:
        s = t.get("status", "pending")
        out[s] = out.get(s, 0) + 1
    return out


def _group_by_platform(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for n in nodes:
        key = f"{n['ip']}:{n['port']}" if n["ip"] else "unknown"
        g = grouped.setdefault(key, {
            "key": key,
            "ip": n["ip"],
            "port": n["port"],
            "platform": n["platform"],
            "task_ids": [],
        })
        g["task_ids"].append(n["id"])
    return list(grouped.values())


# ---------------------------------------------------------------------------
# 场景 3: 工作流执行监控
# ---------------------------------------------------------------------------

def extract_execution_data(state: Dict[str, Any]) -> Dict[str, Any]:
    """实时执行监控数据。"""
    plan = state.get("execution_plan", []) or []
    idx = state.get("current_task_index", 0)
    counts = _count_status(plan)
    total = len(plan)
    progress = (counts["completed"] / total * 100) if total else 0.0

    current: Optional[Dict[str, Any]] = None
    if 0 <= idx < total:
        t = plan[idx]
        meta = t.get("metadata", {}) or {}
        current = {
            "index": idx,
            "task_id": t.get("task_id"),
            "title": t.get("task_title"),
            "agent_id": t.get("assigned_agent_id"),
            "status": t.get("status", "pending"),
            "ip": t.get("target_ip"),
            "port": t.get("target_port"),
            "protocol": meta.get("protocol", "unknown"),
            "executor": meta.get("executor", ""),
            "tools_called": meta.get("tools_called", []),
            "duration_ms": meta.get("duration_ms"),
        }

    timeline: List[Dict[str, Any]] = []
    for i, t in enumerate(plan):
        if t.get("status", "pending") == "pending" and i > idx:
            continue
        meta = t.get("metadata", {}) or {}
        timeline.append({
            "index": i,
            "task_id": t.get("task_id"),
            "title": t.get("task_title"),
            "agent_id": t.get("assigned_agent_id"),
            "status": t.get("status", "pending"),
            "protocol": meta.get("protocol"),
            "executor": meta.get("executor"),
            "tools_called": meta.get("tools_called", []),
            "duration_ms": meta.get("duration_ms"),
            "timestamp": meta.get("timestamp"),
        })

    return {
        "current": current,
        "current_task_index": idx,
        "total": total,
        "counts": counts,
        "progress_percent": round(progress, 1),
        "all_completed": state.get("all_tasks_completed", False),
        "magentic": {
            "round": state.get("magentic_round", 0),
            "max_round": state.get("magentic_max_round", 0),
            "stall_count": state.get("magentic_stall_count", 0),
            "mode": state.get("magentic_mode", ""),
            "ledger": state.get("progress_ledger", {}),
        },
        "timeline": timeline,
        "replanning_count": state.get("replanning_count", 0),
        "last_replan_reason": state.get("last_replan_reason", ""),
    }


# ---------------------------------------------------------------------------
# 综合视图
# ---------------------------------------------------------------------------

def extract_full_view(state: Dict[str, Any]) -> Dict[str, Any]:
    """返回三个场景合并的完整快照,适合一次性推送给前端。"""
    return {
        "orchestration": extract_orchestration_data(state),
        "topology": extract_topology_data(state),
        "execution": extract_execution_data(state),
    }
