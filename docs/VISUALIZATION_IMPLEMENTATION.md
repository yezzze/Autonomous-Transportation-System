# 可视化服务实现指南

## 快速参考：关键数据获取代码片段

### 场景 1：编排过程数据获取

```python
# 文件：src/api/visualization.py（新建）

from src.graph.distributed_types import DistributedState
from src.service.agent_registry import get_registry_client
from src.app.pipeline_parser import parse_pipeline
from typing import Dict, List, Any
import json

def extract_orchestration_data(state: DistributedState) -> Dict[str, Any]:
    """
    从 state 中提取编排过程数据（用于可视化场景1）
    """
    registry = get_registry_client()
    
    return {
        # Skills 数据
        "skills_content": state.get("skills_content", ""),
        "pipeline_topology": state.get("pipeline_topology", []),
        
        # 可用智能体
        "available_agents": [
            {
                "id": a["id"],
                "capability": a["capability"],
                "ip": a["ip"],
                "port": a["port"],
                "status": a["status"],
                "description": a["description"]
            }
            for a in registry.get_all_agents()
        ],
        
        # 本地 vs 远端智能体分类
        "local_agents": [
            a["id"] for a in registry.get_all_agents() 
            if a["ip"] in {"127.0.0.1", "localhost", "host.docker.internal"}
        ],
        "remote_agents": [
            a["id"] for a in registry.get_all_agents()
            if a["ip"] not in {"127.0.0.1", "localhost", "host.docker.internal"}
        ],
        
        # 编排决策过程
        "complexity_level": state.get("complexity_level", "unknown"),  # simple/medium/complex
        "orchestration_mode": state.get("orchestration_mode", "adaptive") if "orchestration_mode" in state else "unknown"
    }
```

### 场景 2：拓扑图数据提取

```python
def extract_topology_data(state: DistributedState) -> Dict[str, Any]:
    """
    从 state 中提取工作流拓扑数据（用于可视化场景2）
    返回格式可直接用于 Mermaid/D3.js 绘制
    """
    execution_plan = state.get("execution_plan", [])
    cross_host_sessions = state.get("cross_host_sessions", {})
    failed_tasks = state.get("failed_tasks", [])
    current_index = state.get("current_task_index", 0)
    
    # 节点列表
    nodes = []
    for i, task in enumerate(execution_plan):
        is_local = task["target_ip"] in {"127.0.0.1", "localhost", "host.docker.internal"}
        is_cross_host = task["task_id"] in cross_host_sessions
        
        nodes.append({
            "id": task["task_id"],
            "index": i,
            "title": task["task_title"],
            "agent_id": task["assigned_agent_id"],
            "status": task["status"],
            "platform": "remote" if is_cross_host else ("local" if is_local else "unknown"),
            "ip": task["target_ip"],
            "port": task["target_port"],
            "parallel_group": task.get("parallel_group", ""),
            "result": task.get("result", "")[:200] + ("..." if len(task.get("result", "")) > 200 else ""),  # 截断显示
            "is_current": i == current_index,
            "is_failed": task["task_id"] in failed_tasks,
            "remote_aoe_url": cross_host_sessions.get(task["task_id"], "")
        })
    
    # 边列表（任务依赖关系）
    edges = []
    parallel_groups = {}
    for i, task in enumerate(execution_plan):
        # 跳过并行组内部的边
        if task.get("parallel_group"):
            pg = task["parallel_group"]
            if pg not in parallel_groups:
                parallel_groups[pg] = []
            parallel_groups[pg].append(i)
        
        # 添加顺序依赖边
        if i > 0 and not task.get("parallel_group"):
            edges.append({
                "from": execution_plan[i-1]["task_id"],
                "to": task["task_id"],
                "type": "sequence"
            })
    
    # 并行组内的边
    for pg, indices in parallel_groups.items():
        if len(indices) > 1:
            # 从上一个任务到并行组
            if indices[0] > 0:
                prev_task = execution_plan[indices[0] - 1]
                for idx in indices:
                    edges.append({
                        "from": prev_task["task_id"],
                        "to": execution_plan[idx]["task_id"],
                        "type": "parallel_start"
                    })
            
            # 并行组内的任务
            for i in range(len(indices) - 1):
                edges.append({
                    "from": execution_plan[indices[i]]["task_id"],
                    "to": execution_plan[indices[i+1]]["task_id"],
                    "type": "parallel_group"
                })
    
    return {
        "nodes": nodes,
        "edges": edges,
        "total_tasks": len(execution_plan),
        "completed_tasks": len([t for t in execution_plan if t["status"] == "completed"]),
        "failed_tasks": len([t for t in execution_plan if t["status"] == "failed"]),
        "cross_host_count": len(cross_host_sessions),
        "current_task_index": current_index
    }
```

### 场景 3：执行监控数据提取

```python
def extract_execution_monitoring_data(state: DistributedState) -> Dict[str, Any]:
    """
    从 state 中提取工作流执行监控数据（用于可视化场景3）
    """
    execution_plan = state.get("execution_plan", [])
    current_index = state.get("current_task_index", 0)
    
    if not execution_plan or current_index >= len(execution_plan):
        return {
            "current_agent": None,
            "current_task": None,
            "execution_progress": 100,
            "status": "completed"
        }
    
    current_task = execution_plan[current_index]
    
    # 构建执行时间线（从 messages 或 timeline 字段）
    timeline = []
    for i, task in enumerate(execution_plan):
        if task["status"] != "pending":
            timeline.append({
                "task_id": task["task_id"],
                "task_title": task["task_title"],
                "agent_id": task["assigned_agent_id"],
                "status": task["status"],
                "timestamp": None,  # TODO：如果 metadata 中有时间戳可填充
                "duration_ms": None,  # TODO：如果有记录可填充
            })
    
    # 工具调用详情
    tool_calls = []
    for task in execution_plan:
        metadata = task.get("metadata", {})
        if metadata.get("protocol"):
            tool_calls.append({
                "task_id": task["task_id"],
                "protocol": metadata["protocol"],
                "executor": metadata.get("executor", "unknown"),
                "success": task["status"] == "completed"
            })
    
    return {
        # 当前执行状态
        "current_task_index": current_index,
        "current_agent_id": current_task["assigned_agent_id"],
        "current_task_title": current_task["task_title"],
        "current_task_status": current_task["status"],
        "current_task_ip": current_task["target_ip"],
        "current_task_port": current_task["target_port"],
        
        # 进度信息
        "total_tasks": len(execution_plan),
        "completed_tasks": len([t for t in execution_plan if t["status"] == "completed"]),
        "failed_tasks": len([t for t in execution_plan if t["status"] == "failed"]),
        "execution_progress": (current_index / len(execution_plan) * 100) if execution_plan else 0,
        
        # 工作流状态
        "workflow_status": "running" if current_index < len(execution_plan) else "completed",
        
        # Magentic-One 轮次（若适用）
        "magentic_round": state.get("magentic_round", 0),
        "magentic_max_round": state.get("magentic_max_round", 0),
        
        # 执行时间线
        "timeline": timeline,
        
        # 工具调用汇总
        "tool_calls": tool_calls,
        "protocol_distribution": {
            p: len([tc for tc in tool_calls if tc["protocol"] == p])
            for p in set(tc["protocol"] for tc in tool_calls)
        }
    }
```

---

## 新增 API 端点实现（src/api/app.py 中添加）

```python
from fastapi import FastAPI, HTTPException, Path
from typing import Dict, Any
import os

# 假设已导入 extract_* 函数

# 全局存储：workflow_id → state 映射（简化实现）
_workflow_cache: Dict[str, DistributedState] = {}

@app.get("/api/visualization/orchestration")
async def get_orchestration_data():
    """
    获取当前编排过程数据（场景1）
    
    用于显示：
    - Skills.md 内容
    - 可用智能体列表
    - 编排模式（管道/LLM）
    """
    try:
        # TODO：从某处获取当前 state（可能需要修改工作流服务保存 state）
        state = _get_current_state()
        
        data = extract_orchestration_data(state)
        return {
            "status": "success",
            "data": data
        }
    except Exception as e:
        logger.error(f"获取编排数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/visualization/topology")
async def get_topology_data():
    """
    获取工作流拓扑数据（场景2）
    
    用于显示：
    - 任务节点（圆圈/方块）
    - 任务依赖边（箭头）
    - 并行组（高亮）
    - 跨主体标记（颜色区分）
    """
    try:
        state = _get_current_state()
        
        data = extract_topology_data(state)
        return {
            "status": "success",
            "data": data
        }
    except Exception as e:
        logger.error(f"获取拓扑数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/visualization/execution")
async def get_execution_monitoring():
    """
    获取工作流执行监控数据（场景3）
    
    用于显示：
    - 当前执行的任务和智能体
    - 实时进度条
    - 执行时间线（甘特图）
    - 工具调用链路
    """
    try:
        state = _get_current_state()
        
        data = extract_execution_monitoring_data(state)
        return {
            "status": "success",
            "data": data
        }
    except Exception as e:
        logger.error(f"获取执行监控数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/visualization/agents-status")
async def get_agents_status():
    """
    获取所有智能体实时状态（补充信息）
    
    返回：
    - 每个智能体的在线状态
    - 当前负载（任务数）
    - IP 地址和端口
    """
    try:
        registry = get_registry_client()
        agents = registry.get_all_agents()
        
        return {
            "status": "success",
            "data": {
                "agents": [
                    {
                        "id": a["id"],
                        "capability": a["capability"],
                        "ip": a["ip"],
                        "port": a["port"],
                        "status": a["status"],
                        "is_local": a["ip"] in {"127.0.0.1", "localhost", "host.docker.internal"}
                    }
                    for a in agents
                ],
                "total": len(agents),
                "online": len([a for a in agents if a["status"] == "online"])
            }
        }
    except Exception as e:
        logger.error(f"获取智能体状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _get_current_state() -> DistributedState:
    """
    获取当前工作流的 state
    
    注意：这是一个简化实现，实际需要：
    1. 从 distributed_builder 中获取当前运行的 graph
    2. 提取其内部状态
    3. 或者在工作流执行时将 state 保存到缓存
    """
    # 临时方案：返回最近一次保存的 state
    if not _workflow_cache:
        raise HTTPException(status_code=404, detail="No workflow state found")
    
    # 取最后保存的 state
    latest_state = list(_workflow_cache.values())[-1]
    return latest_state


# 注意：需要在 distributed_nodes.py 各节点处添加钩子来保存 state
# 例如在 distributed_planner_node 和 distributed_executor_node 返回前：
# _workflow_cache["current"] = state
```

---

## 修改 distributed_nodes.py 的埋点位置

### 位置 1：Planner 节点完成后

```python
# src/graph/distributed_nodes.py，distributed_planner_node 函数末尾

# 在 return Command(...) 之前添加：
if os.getenv("ENABLE_VIZ_CACHE") == "1":
    from src.api.visualization import _workflow_cache
    _workflow_cache["current"] = state  # 保存当前 state 用于可视化

return Command(
    update={...},
    goto="executor"
)
```

### 位置 2：Executor 节点任务完成后

```python
# src/graph/distributed_nodes.py，distributed_executor_node 函数末尾

# 保存状态到缓存（支持可视化实时更新）
if os.getenv("ENABLE_VIZ_CACHE") == "1":
    try:
        from src.api.visualization import _workflow_cache
        import copy
        _workflow_cache["current"] = copy.deepcopy(state)
    except Exception:
        pass  # 非关键操作，失败不影响主流程

return Command(
    update={...},
    goto="monitor"
)
```

---

## 前端可视化框架建议

### 依赖包

```json
{
  "devDependencies": {
    "mermaid": "^10.6.1",
    "d3": "^7.8.5",
    "chart.js": "^4.4.0",
    "echarts": "^5.4.3",
    "axios": "^1.6.0"
  }
}
```

### 场景 1：编排过程（Skills 展示）

```html
<div id="orchestration">
  <h2>编排过程</h2>
  
  <!-- Skills.md 预览 -->
  <div id="skills-preview">
    <!-- 内容动态渲染 -->
  </div>
  
  <!-- 可用智能体列表 -->
  <div id="agents-list">
    <table>
      <thead>
        <tr>
          <th>智能体 ID</th>
          <th>能力</th>
          <th>地址</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody id="agents-body">
        <!-- 动态填充 -->
      </tbody>
    </table>
  </div>
  
  <!-- 编排模式指示 -->
  <div id="mode-indicator">
    当前模式：<span id="mode-text">管道</span>
  </div>
</div>

<script>
async function loadOrchestrationData() {
  const res = await fetch('/api/visualization/orchestration');
  const { data } = await res.json();
  
  // 渲染 Skills
  document.getElementById('skills-preview').innerHTML = 
    marked(data.skills_content || "无自定义技能");
  
  // 渲染智能体列表
  const tbody = document.getElementById('agents-body');
  tbody.innerHTML = data.available_agents.map(a => `
    <tr class="${data.local_agents.includes(a.id) ? 'local' : 'remote'}">
      <td>${a.id}</td>
      <td>${a.capability}</td>
      <td>${a.ip}:${a.port}</td>
      <td><span class="badge ${a.status}">${a.status}</span></td>
    </tr>
  `).join('');
  
  document.getElementById('mode-text').textContent = 
    data.pipeline_topology.length > 0 ? '管道' : 'LLM 规划';
}

loadOrchestrationData();
setInterval(loadOrchestrationData, 5000);  // 每 5 秒刷新
</script>
```

### 场景 2：拓扑图（Mermaid 渲染）

```html
<div id="topology">
  <h2>工作流拓扑</h2>
  <div id="mermaid-container"></div>
</div>

<script>
async function loadTopologyData() {
  const res = await fetch('/api/visualization/topology');
  const { data } = await res.json();
  
  // 生成 Mermaid 图定义
  let mermaidDef = "graph TD\n";
  
  data.nodes.forEach(node => {
    const status_color = 
      node.status === "completed" ? "green" :
      node.status === "failed" ? "red" :
      node.is_current ? "yellow" : "gray";
    
    mermaidDef += `    ${node.id}["${node.title}<br/>${node.agent_id}"]:::${status_color}\n`;
  });
  
  data.edges.forEach(edge => {
    const style = edge.type === "parallel_start" ? "-->" : "->";
    mermaidDef += `    ${edge.from} ${style} ${edge.to}\n`;
  });
  
  // 样式定义
  mermaidDef += `
    classDef green fill:#4CAF50,color:#fff
    classDef red fill:#f44336,color:#fff
    classDef yellow fill:#ffeb3b,color:#000
    classDef gray fill:#9e9e9e,color:#fff
  `;
  
  document.getElementById('mermaid-container').innerHTML = mermaidDef;
  mermaid.contentLoaded();
}

loadTopologyData();
setInterval(loadTopologyData, 2000);  // 每 2 秒刷新
</script>
```

### 场景 3：执行监控（时间线 + 进度）

```html
<div id="execution-monitor">
  <h2>执行监控</h2>
  
  <!-- 当前任务卡片 -->
  <div id="current-task-card">
    <h3 id="current-title">任务加载中...</h3>
    <p>智能体：<span id="current-agent">--</span></p>
    <p>状态：<span id="current-status" class="badge">pending</span></p>
    <p>地址：<span id="current-ip">--</span></p>
  </div>
  
  <!-- 进度条 -->
  <div id="progress-bar">
    <div id="progress-fill"></div>
  </div>
  <p>进度：<span id="progress-text">0/0</span></p>
  
  <!-- 执行时间线（甘特图）-->
  <canvas id="timeline-chart"></canvas>
  
  <!-- 工具调用列表 -->
  <table id="tools-table">
    <thead>
      <tr>
        <th>任务</th>
        <th>协议</th>
        <th>执行器</th>
        <th>成功</th>
      </tr>
    </thead>
    <tbody id="tools-body">
      <!-- 动态填充 -->
    </tbody>
  </table>
</div>

<script>
async function loadExecutionData() {
  const res = await fetch('/api/visualization/execution');
  const { data } = await res.json();
  
  // 当前任务
  document.getElementById('current-title').textContent = 
    data.current_task_title || '无';
  document.getElementById('current-agent').textContent = 
    data.current_agent_id || '--';
  document.getElementById('current-status').textContent = 
    data.current_task_status || 'pending';
  document.getElementById('current-ip').textContent = 
    `${data.current_task_ip}:${data.current_task_port}`;
  
  // 进度条
  const percent = Math.round(data.execution_progress);
  document.getElementById('progress-fill').style.width = percent + '%';
  document.getElementById('progress-text').textContent = 
    `${data.completed_tasks}/${data.total_tasks}`;
  
  // 工具列表
  const tbody = document.getElementById('tools-body');
  tbody.innerHTML = data.tool_calls.map(t => `
    <tr>
      <td>${t.task_id}</td>
      <td>${t.protocol.toUpperCase()}</td>
      <td>${t.executor}</td>
      <td><span class="badge ${t.success ? 'success' : 'error'}">
        ${t.success ? '✓' : '✗'}
      </span></td>
    </tr>
  `).join('');
}

loadExecutionData();
setInterval(loadExecutionData, 1000);  // 每 1 秒刷新
</script>
```

---

## 总结：实现优先级

| 优先级 | 任务 | 工作量 | 依赖 |
|--------|------|--------|------|
| **1** | 新增 3 个 API 端点 | 2h | 无 |
| **1** | 在 distributed_nodes.py 添加埋点 | 1h | 无 |
| **2** | 前端场景 1（Skills + 智能体列表） | 3h | API 端点 1 |
| **2** | 前端场景 2（拓扑图 Mermaid） | 4h | API 端点 2 |
| **2** | 前端场景 3（执行监控） | 5h | API 端点 3 |
| **3** | 优化：WebSocket 实时推送 | 6h | 无 |
| **3** | 优化：工具调用详情埋点 | 3h | 无 |

**总估时**：约 1-2 周（按优先级递减）

