# LangManus 可视化数据快速参考卡

## 三个场景的核心数据字段

### 场景 1: 编排过程
```
数据来源: DistributedState
├─ skills_content: str (Skills.md 原文)
├─ pipeline_topology: List (管道拓扑，若有)
├─ available_agents: List[AgentInfo] (从 ARDC 查询)
└─ complexity_level: str (simple/medium/complex)

数据获取方式:
- 直接读: state.get("skills_content", "")
- API 查: registry.query_agents() or registry.get_all_agents()
- 通过: from src.api.visualization import extract_orchestration_data
```

### 场景 2: 拓扑图
```
数据来源: DistributedState.execution_plan
├─ 节点: TaskAssignment (task_id, task_title, agent_id, status)
├─ 边: 隐式（列表顺序 + parallel_group）
├─ 平台标记: target_ip (127.0.0.1 = 本机, 其他 = 远端)
└─ 跨主体: cross_host_sessions: Dict[task_id → remote_url]

关键字段:
┌────────────────┬──────────┬────────────────────┐
│ 字段           │ 类型     │ 含义               │
├────────────────┼──────────┼────────────────────┤
│ task_id        │ str      │ 唯一标识           │
│ task_title     │ str      │ 显示标题           │
│ assigned_agent │ str      │ 所选智能体 ID      │
│ target_ip      │ str      │ 执行平台           │
│ parallel_group │ str      │ 并行组 ID          │
│ status         │ str      │ pending/running... │
└────────────────┴──────────┴────────────────────┘

提取方式:
from src.api.visualization import extract_topology_data
data = extract_topology_data(state)
# 返回: {nodes: [], edges: [], ...}
```

### 场景 3: 执行监控
```
数据来源: DistributedState
├─ current_task_index: int (当前任务下标)
├─ execution_plan[index]: TaskAssignment (当前任务详情)
├─ execution_plan[*].metadata.protocol: str (MCP/A2A/LLM_SIM)
├─ execution_plan[*].metadata.executor: str (具体工具)
├─ magentic_round: int (Magentic-One 轮次)
└─ progress_ledger: dict (进度账本)

当前执行智能体:
current_task = state["execution_plan"][state["current_task_index"]]
agent_id = current_task["assigned_agent_id"]
protocol = current_task["metadata"]["protocol"]

提取方式:
from src.api.visualization import extract_execution_monitoring_data
data = extract_execution_monitoring_data(state)
# 返回: {current_agent_id, current_task_status, execution_progress, ...}
```

---

## 已有数据接口

| 接口 | 方法 | 返回 | 可视化用途 |
|------|------|------|----------|
| `/api/chat/stream` | POST | SSE 事件流 | ✅ 实时任务更新 |
| `/registry/sync` | POST | 状态码 | ❌ 仅内部 |
| `/health` | GET | {status, load} | ✅ UI 连接检测 |

---

## 需要新增的 API

### 优先级 1（必须，完成可视化）
```python
# GET /api/visualization/orchestration
# 返回: {skills_content, pipeline_topology, available_agents}

# GET /api/visualization/topology  
# 返回: {nodes, edges, total_tasks, failed_tasks}

# GET /api/visualization/execution
# 返回: {current_agent_id, current_task_status, timeline, tool_calls}
```

### 优先级 2（增强可视化）
```python
# GET /api/visualization/agents-status
# 返回: {agents[], total, online}

# GET /api/execution/{workflow_id}/history
# 返回: 完整执行历史
```

---

## State 关键字段速查表

### DistributedState 中涉及可视化的所有字段

```python
class DistributedState:
    # === 编排过程 ===
    skills_content: str                          # 场景 1
    pipeline_topology: List                      # 场景 1
    complexity_level: str                        # 场景 1
    
    # === 拓扑与执行 ===
    execution_plan: List[TaskAssignment]         # 场景 2, 3
    current_task_index: int                      # 场景 3
    
    # === 跨主体 ===
    cross_host_sessions: Dict[str, str]          # 场景 2
    failed_cross_host_tasks: List[str]           # 场景 2
    failed_remote_aoe_urls: Dict[str, List[str]] # 场景 2
    
    # === Magentic-One ===
    magentic_round: int                          # 场景 3
    magentic_max_round: int                      # 场景 3
    progress_ledger: dict                        # 场景 3
    
    # === 错误跟踪 ===
    failed_tasks: List[str]                      # 场景 2, 3
    
    # === Agent 注册表 ===
    agent_registry_cache: List[AgentInfo]        # 场景 1
```

---

## TaskAssignment 完整字段

```python
class TaskAssignment(TypedDict):
    task_id: str                     # 唯一 ID
    task_title: str                  # 显示标题
    task_description: str            # 详细描述
    assigned_agent_id: str           # 所选 Agent
    target_ip: str                   # 运行主机
    target_port: int                 # 运行端口
    status: str                      # pending|running|completed|failed
    result: str                      # 执行结果
    retry_count: int                 # 重试次数
    parallel_group: str              # 并行组 ID（同值=并行）
    metadata: Optional[dict]         # 执行元数据
        ├─ protocol: str             # mcp|a2a|llm_simulator
        ├─ executor: str             # 具体工具名
        ├─ tools_called: List[str]   # [已调用工具列表] (需埋点)
        └─ timestamp: str            # 执行时间戳 (需埋点)
```

---

## 前端实现速度优化

### 最小可行产品（MVP，1-2 天）
```
1. 复用 /api/chat/stream SSE 流
2. 添加 3 个数据提取函数到 src/api/visualization.py
3. 前端用原生 HTML/JS + Mermaid 库
   - 场景 1：简单表格
   - 场景 2：Mermaid 图
   - 场景 3：进度条 + 日志
```

### 完整版本（2-3 周）
```
- React 框架 + TypeScript
- ECharts 高级图表
- WebSocket 实时推送
- 工具链路详情展示
```

---

## 埋点清单（优先级）

### 高：立即添加（在 distributed_nodes.py）
- [x] execution_plan[i]["metadata"]["protocol"]
- [x] execution_plan[i]["metadata"]["executor"]
- [ ] execution_plan[i]["metadata"]["tools_called"] ← 需新增
- [ ] execution_plan[i]["metadata"]["timestamp"] ← 需新增

### 中：完善可视化
- [ ] DistributedState.execution_timeline ← 新增字段
- [ ] 工具参数和输出记录
- [ ] 任务执行时长统计

### 低：高级功能
- [ ] 资源利用率监控
- [ ] 成本统计
- [ ] 故障链路详情

---

## 代码片段：从 State 获取数据

### 获取当前执行的智能体
```python
if state.get("execution_plan") and "current_task_index" in state:
    idx = state["current_task_index"]
    if idx < len(state["execution_plan"]):
        current_task = state["execution_plan"][idx]
        agent_id = current_task["assigned_agent_id"]
        status = current_task["status"]
        protocol = current_task.get("metadata", {}).get("protocol", "unknown")
```

### 获取所有可用智能体
```python
from src.service.agent_registry import get_registry_client

registry = get_registry_client()
all_agents = registry.get_all_agents()  # 包括本地 + Gossip peer 代理
local_agents = [a for a in all_agents 
                if a["ip"] in {"127.0.0.1", "localhost"}]
remote_agents = [a for a in all_agents if a["ip"] not in {...}]
```

### 判断任务是否跨主体
```python
task_id = "task_001"
cross_host_sessions = state.get("cross_host_sessions", {})
if task_id in cross_host_sessions:
    remote_url = cross_host_sessions[task_id]  # http://192.168.1.10:9000
    print(f"此任务将在 {remote_url} 执行")
```

### 获取任务执行进度
```python
execution_plan = state.get("execution_plan", [])
if execution_plan:
    total = len(execution_plan)
    completed = len([t for t in execution_plan if t["status"] == "completed"])
    failed = len([t for t in execution_plan if t["status"] == "failed"])
    progress = (completed / total * 100) if total else 0
    print(f"进度: {completed}/{total} ({progress:.1f}%)")
```

---

## 文件位置速查

| 功能 | 文件位置 |
|------|---------|
| State 定义 | `src/graph/distributed_types.py` (L33-91) |
| Planner 节点 | `src/graph/distributed_nodes.py` (L272-648) |
| Executor 节点 | `src/graph/distributed_nodes.py` (L654-997) |
| Agent 注册表 | `src/service/agent_registry.py` (L25-424) |
| Pipeline 解析 | `src/app/pipeline_parser.py` (L123-196) |
| MCP 工具 | `src/service/mcp_client.py` (L16-268) |
| Progress Ledger | `src/graph/progress_ledger.py` (L18-30) |
| 现有 Web UI | `web_ui.py` (L1-1050) |
| 主 API | `src/api/app.py` (L1-682) |

---

## 最常用的 3 行代码

```python
# 1. 获取当前执行的智能体
current_agent = state["execution_plan"][state["current_task_index"]]["assigned_agent_id"]

# 2. 获取所有在线智能体
all_agents = get_registry_client().get_all_agents()

# 3. 检查任务是否跨主体
is_remote = task_id in state.get("cross_host_sessions", {})
```

---

## 调试技巧

### 打印当前 State（在 distributed_nodes.py 中）
```python
import json
logger.info(f"Current state: {json.dumps(state, indent=2, default=str)}")
```

### 监控 SSE 流（浏览器控制台）
```javascript
const eventSource = new EventSource('/api/chat/stream');
eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log(data);
};
```

### 查询注册表（Python REPL）
```python
from src.service.agent_registry import get_registry_client
registry = get_registry_client()
print(registry.get_registry_summary())
```

---

## 常见问题

**Q: Skills.md 在哪里？**
A: `state.get("skills_content")` 中（由 GuidanceFile 注入）

**Q: 怎么区分本机和远端智能体？**
A: 检查 `agent_info["ip"]` 是否在 `{"127.0.0.1", "localhost", "host.docker.internal"}` 中

**Q: 并行任务怎么表示？**
A: 同一个 `parallel_group` 字段值的任务是并行的

**Q: 当前执行哪个智能体？**
A: `state["execution_plan"][state["current_task_index"]]["assigned_agent_id"]`

**Q: 工具调用在哪里记录？**
A: `execution_plan[i]["metadata"]["protocol"]` 和 `["executor"]` 字段（不够详细，需埋点）

---

