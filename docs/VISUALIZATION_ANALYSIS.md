# LangManus 动态可视化服务 - 深度数据调研报告

## 摘要

本报告深度分析了 LangManus 项目的三个关键可视化场景的数据来源、结构和获取方式。

---

## 场景 1: 编排过程（任务分解与 Agent 选择）

### 1.1 Skills 数据结构

**定义位置**：`src/app/models.py` (L76-96)

```python
@dataclass
class GuidanceFile:
    app_id: str
    task_description: str                   # 任务总体描述
    agents_required: List[str]              # 所需 Agent 能力列表
    orchestration_mode: str = "adaptive"    # "adaptive" | "sequential" | "magentic"
    constraints: Dict[str, Any]             # 约束条件
    skills_content: Optional[str] = None    # **关键**：Skills.md 内容（注入给编排引擎）
    metadata: Dict[str, Any]
```

**Skills.md 解析**：`src/app/pipeline_parser.py`

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `capability` | str | 能力标签（小写） | `"search"`, `"nlp"`, `"compute"` |
| `description` | str | 自定义任务描述 | `"搜索与用户查询相关的最新资讯"` |
| `agent_id` | str | 绑定的 Agent ID | `"search_agent_001"` |

**Pipeline 拓扑解析**：

```python
# 数据结构
PipelineTopology = List[PipelineStep]
PipelineStep = Union[AgentStep, List[AgentStep]]  # 列表=并行组
AgentStep = {"capability": str, "description": str, "agent_id": str}

# 示例：Pipeline 段落
## Pipeline
search:搜索最新竞品资讯
-> [nlp:对搜索结果做摘要分析, compute:计算指标]  # 并行组
-> nlp:生成结构化报告
```

**关键 API**：
- `parse_pipeline(skills_content: str) -> Optional[PipelineTopology]` — 从 Skills.md 提取拓扑
- 返回值非空时跳过 LLM Planner，直接执行固定拓扑

---

### 1.2 每个平台的智能体候选列表

**注册表客户端**：`src/service/agent_registry.py` (L25-176)

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `id` | str | AgentInfo | 智能体唯一标识 |
| `ip` | str | AgentInfo | 运行主机 IP（本机=127.0.0.1） |
| `port` | int | AgentInfo | 监听端口（8080-8085） |
| `capability` | str | AgentInfo | 能力标签（search/nlp/compute/vision/code_execution/web_interaction） |
| `status` | str | AgentInfo | 状态（online/offline/busy） |
| `description` | str | AgentInfo | 能力描述 |

**本机智能体默认列表**（配置文件优先，无则使用 Mock）：

```json
[
  {"id": "search_agent_001", "ip": "127.0.0.1", "port": 8080, "capability": "search", "status": "online"},
  {"id": "compute_agent_001", "ip": "127.0.0.1", "port": 8081, "capability": "compute", "status": "online"},
  {"id": "vision_agent_001", "ip": "127.0.0.1", "port": 8082, "capability": "vision", "status": "online"},
  {"id": "nlp_agent_001", "ip": "127.0.0.1", "port": 8083, "capability": "nlp", "status": "online"},
  {"id": "code_agent_001", "ip": "127.0.0.1", "port": 8084, "capability": "code_execution", "status": "online"},
  {"id": "web_agent_001", "ip": "127.0.0.1", "port": 8085, "capability": "web_interaction", "status": "online"}
]
```

**远端智能体发现**：Gossip 协议

| 方向 | 接口 | 数据结构 |
|------|------|--------|
| **推送** | `POST /registry/sync` | `{"source_url": str, "agents": List[AgentInfo]}` |
| **查询** | `registry.query_agents(capability=None)` | 返回在线 Agent + Peer 代理合并列表 |

**跨节点代理缓存**（gossip 后台线程）：
```python
self._peer_agents: Dict[str, List[AgentInfo]] = {}      # peer_url → [Agent列表]
self._peer_last_seen: Dict[str, float] = {}             # peer_url → 最后同步时间戳
```

**查询 API**：

```python
# 方法1：精确匹配（按能力类型）
available_agents = registry_client.query_agents(capability="search")

# 方法2：获取所有在线智能体
available_agents = registry_client.get_all_agents()

# 方法3：按 ID 查询单个
agent_info = registry_client.get_agent_by_id("search_agent_001")
```

---

### 1.3 Agent 选择与匹配逻辑

**匹配流程**：`src/graph/distributed_nodes.py` (L272-450)

```
用户请求 
  ↓
Skills.md 管道解析（若有）
  ├─ 是 → 跳过 LLM Planner，直接生成固定拓扑执行计划 ✅
  └─ 否 → LLM 规划器
       ↓
     调用 LLM (qwq-plus / reasoning-model)
       ↓
     LLM 分析用户请求 + 可用 Agent 列表
       ↓
     生成 JSON 执行计划（包含 task_id / assigned_agent_id / task_description）
       ↓
     补充 IP:port 信息（从 ARDC 查询）
       ↓
     生成 TaskAssignment 列表
```

**关键数据变换**：

| 阶段 | 输入 | 输出 | 关键字段 |
|------|------|------|---------|
| Planner | `messages[-1]` (用户请求) | JSON 执行计划 | `tasks: [{task_id, task_title, task_description, assigned_agent_id}]` |
| 匹配补充 | JSON 计划 + ARDC 查询 | `TaskAssignment[]` | `target_ip`, `target_port` |
| 管道模式 | `pipeline_topology` | `TaskAssignment[]` | 并行组 `parallel_group` 字段 |

**跨主体匹配**：`src/graph/distributed_nodes.py` (L236-265)

```python
# 识别跨主体任务
cross_host_sessions = identify_cross_host_tasks(
    execution_plan=execution_plan,
    available_agents=available_agents
)
# 返回：{task_id → remote_aoe_url}
# 如：{"task_001": "http://192.168.1.10:9000"}
```

**选择后的标记方式**（在 DistributedState 中）：

```python
# 字段名称 | 类型 | 位置 | 用途
execution_plan: List[TaskAssignment]      # 任务赋值表
current_task_index: int                    # 当前执行的任务索引
cross_host_sessions: Dict[str, str]        # {task_id → 远端AOE_URL}
failed_cross_host_tasks: List[str]         # 失败过的跨主体任务 ID
failed_remote_aoe_urls: Dict[str, List[str]]  # {task_id → [尝试过的失败URL]}
```

---

## 场景 2: 编排结果（拓扑图可视化）

### 2.1 工作流拓扑结构

**定义位置**：`src/app/pipeline_parser.py`, `src/graph/distributed_nodes.py`

| 组件 | 数据结构 | 来源 |
|------|--------|------|
| **拓扑类型** | `PipelineTopology = List[PipelineStep]` | 固定管道或 LLM 规划 |
| **节点** | `AgentStep = {"capability", "description", "agent_id"}` | 管道解析或 LLM 输出 |
| **边** | 隐式（列表顺序 + 并行组） | 管道语法或 LLM 任务依赖描述 |

**拓扑示例**：

```python
# 管道 1：串行链
[
  {"capability": "search", "description": "搜索资讯", "agent_id": "search_agent_001"},
  {"capability": "nlp", "description": "做摘要", "agent_id": "nlp_agent_001"},
  {"capability": "nlp", "description": "生成报告", "agent_id": "nlp_agent_001"}
]

# 管道 2：并行组
[
  {"capability": "search", "description": "...", "agent_id": "search_agent_001"},
  [  # 并行组（List[AgentStep]）
    {"capability": "nlp", "description": "摘要分析", "agent_id": "nlp_agent_001"},
    {"capability": "compute", "description": "计算指标", "agent_id": "compute_agent_001"}
  ],
  {"capability": "vision", "description": "图表生成", "agent_id": "vision_agent_001"}
]
```

**可视化图结点信息**：

```python
ExecutionPlan = List[TaskAssignment]

class TaskAssignment(TypedDict):
    task_id: str                     # 唯一标识
    task_title: str                  # 显示标题
    task_description: str            # 详细描述
    assigned_agent_id: str           # 所选 Agent
    target_ip: str                   # **平台标记** 本机 vs 远端
    target_port: int                 # 通信端口
    status: str                      # pending|running|completed|failed
    result: str                      # 执行结果
    retry_count: int                 # 重试次数
    parallel_group: str              # 并行组 ID（同值 = 并行）
```

**平台分布字段**：

| 字段 | 类型 | 判断逻辑 | 示例 |
|------|------|--------|------|
| `target_ip` | str | `in LOCAL_NODE_IDS` ? 本机 : 远端 | `"127.0.0.1"` / `"192.168.1.10"` |
| `target_port` | int | 本机范围 8000-8100，远端 9000 | `8080` / `9000` |
| `cross_host_sessions[task_id]` | Optional[str] | 存在 → 跨主体 | `"http://192.168.1.10:9000"` |

**LOCAL_NODE_IDS 定义**：

```python
LOCAL_NODE_IDS = {"localhost", "host.docker.internal", "127.0.0.1"}
```

---

### 2.2 跨主体路由信息

**数据结构**：`src/graph/distributed_types.py` (L74-83)

```python
# 跨主体会话表（State 字段）
cross_host_sessions: Dict[str, str]  # {task_id → 远端AOE_URL}
# 示例：
# {
#   "task_001": "http://192.168.1.10:9000",
#   "task_003": "http://192.168.1.20:9000"
# }

# 故障跟踪（用于故障转移）
failed_cross_host_tasks: List[str]   # [task_id, ...]
failed_remote_aoe_urls: Dict[str, List[str]]  # {task_id → [已尝试失败的URL列表]}
```

**生成时机**：`src/graph/distributed_nodes.py` (L236-265)

```python
# Planner 输出执行计划后，立即识别跨主体任务
cross_host_sessions = identify_cross_host_tasks(
    execution_plan=execution_plan,
    available_agents=available_agents
)
```

**故障转移逻辑**（§2.3 重编排）：

```python
# 若跨主体执行失败，调用
alternative_url = find_alternative_remote_aoe(
    task=current_task,
    failed_urls=failed_remote_aoe_urls.get(task_id, [])
)
# 返回：可替代的 remote_aoe_url 或 None

# 记录已失败的节点
failed_remote_aoe_urls[task_id] = [原始URL, 新尝试URL, ...]
```

---

## 场景 3: 工作流执行（实时监控）

### 3.1 当前正在执行的智能体

**State 字段**：`src/graph/distributed_types.py` (L49-56)

| 字段 | 类型 | 更新频率 | 用途 |
|------|------|--------|------|
| `current_task_index` | int | 每任务完成后 +1 | 指向当前任务在 execution_plan 中的下标 |
| `execution_plan[current_task_index]` | TaskAssignment | 实时更新 | 当前任务的完整信息 |
| `magentic_round` | int | Magentic-One 模式每轮 +1 | Magentic-One 编排的轮次计数 |

**获取当前执行智能体**：

```python
current_task = state["execution_plan"][state["current_task_index"]]
current_agent_id = current_task["assigned_agent_id"]
current_task_status = current_task["status"]  # pending|running|completed|failed
```

**任务状态变化流程**（executor 节点）：

```
pending  → 执行中 → completed  (成功)
         ↘        ↙
              failed  (失败或重试)
```

---

### 3.2 Agent 调用的 MCP 工具记录

**MCP 工具集定义**：`src/service/mcp_client.py` (L121-145)

```python
class MCPToolRegistry:
    MCP_SERVERS = {
        # 目前默认禁用（注释）：
        # "search": ["npx", "@modelcontextprotocol/server-brave-search"],
        # "filesystem": ["npx", "@modelcontextprotocol/server-filesystem", "/tmp"],
        # "puppeteer": ["npx", "@modelcontextprotocol/server-puppeteer"],
    }
```

**本机工具集**：`src/tools/` 目录

| 工具 | 文件 | 调用方式 | 日志记录 |
|------|------|--------|--------|
| `bash_tool` | `src/tools/bash_tool.py` | 直接执行命令 | ❌ 未记录到 state |
| `python_repl` | `src/tools/python_repl.py` | 执行 Python 代码 | ❌ 未记录到 state |
| `search` | `src/tools/search.py` | 网络搜索 | ❌ 未记录到 state |
| `crawl` | `src/tools/crawl.py` | 网页爬取 | ❌ 未记录到 state |
| `file_management` | `src/tools/file_management.py` | 文件操作 | ❌ 未记录到 state |

**工具调用记录位置**：

```python
# src/graph/distributed_nodes.py executor 节点 (L956-962)
# 记录在 execution_plan 元数据中（但不详细）：
updated_plan[current_index]["metadata"] = {
    "protocol": result_data.get("protocol", "unknown"),  # "mcp" / "a2a" / "llm_simulator"
    "executor": result_data.get("tool_used") or result_data.get("agent_used", "unknown")
}
```

**现状分析**：

- ✅ **协议级别记录**：MCP vs A2A 协议有记录
- ❌ **工具级别记录**：具体调用了哪个 tool（search/bash/python）**无记录**
- ❌ **工具参数记录**：工具的输入参数**无记录**
- ❌ **工具输出详情**：只记录最终结果，中间步骤无记录

---

### 3.3 事件流与进度回调机制

#### 3.3.1 现有事件系统

**事件总线**：`src/service/demo_bus.py`（Demo 模式特定）

```python
# 事件发布
get_demo_bus().publish("event_type", {"payload": ...})

# 已定义事件：
"demo:plan_ready"        # 任务规划完成
"demo:dispatch_start"    # 任务分发开始
"demo:dispatch_done"     # 任务执行完成
```

**事件订阅示例**（Web UI）：`web_ui.py` (L105-146)

```python
# 日志消费线程
async def consume_logs():
    while True:
        while not log_queue.empty():
            log_data = log_queue.get_nowait()
            await manager.broadcast({
                "type": "log",
                "data": log
            })
```

#### 3.3.2 进度账本（Magentic-One 模式）

**Progress Ledger**：`src/graph/progress_ledger.py` (L18-30)

```python
class ProgressLedger(TypedDict):
    is_request_satisfied: ProgressLedgerItem      # 任务完成？
    is_in_loop: ProgressLedgerItem                # 陷入循环？
    is_progress_being_made: ProgressLedgerItem    # 有进展？
    next_speaker: ProgressLedgerItem              # 下一个谁发言？
    instruction_or_question: ProgressLedgerItem   # 给什么指令？
```

**集成位置**：`src/graph/distributed_types.py` (L70)

```python
progress_ledger: dict  # 进度账本（Magentic-One 模式）
```

#### 3.3.3 HTTP API 回调端点

**主 API 服务**：`src/api/app.py` (L120-183)

```python
@app.post("/api/chat/stream")
async def chat_endpoint(request: ChatRequest, req: Request):
    async def event_generator():
        async for event in run_agent_workflow(...):
            yield {
                "event": event["event"],
                "data": json.dumps(event["data"], ensure_ascii=False)
            }
    return EventSourceResponse(event_generator(), ...)
```

**事件类型**：

```python
# 推断的事件类型（根据 workflow_service.py）
"messages_update"        # 消息更新
"state_update"           # 状态更新
"task_completed"         # 任务完成
"plan_generated"         # 执行计划生成
```

---

## 综合数据提取方案

### 关键字段清单

#### 场景 1：编排过程

```python
# 直接读取 state
skills_content: str                          # Skills.md 原文
pipeline_topology: List[PipelineStep]        # 管道拓扑（若有）

# 查询 ARDC（API 调用）
available_agents: List[AgentInfo]            # 候选智能体列表

# Planner 输出（state.messages）
execution_plan: List[TaskAssignment]         # 最终分配表
```

#### 场景 2：拓扑图

```python
# 从 execution_plan 提取
topology = [
  {
    "task_id": str,
    "agent_id": str,
    "platform": "local" | "remote",
    "ip": str,
    "port": int,
    "parallel_group": str,
    "status": str
  }
]

# 跨主体映射
cross_host_sessions: Dict[str, str]          # {task_id → remote_url}
failed_cross_host_tasks: List[str]           # 失败任务 ID
```

#### 场景 3：执行监控

```python
# 当前执行状态
current_task = execution_plan[current_task_index]
current_agent_id = current_task["assigned_agent_id"]

# 进度信息
current_task_index: int                      # 当前任务索引
magentic_round: int                          # Magentic-One 轮次

# 工具调用记录
execution_plan[i]["metadata"]["protocol"]    # mcp | a2a | llm_simulator
execution_plan[i]["metadata"]["executor"]    # 具体工具名
execution_plan[i]["result"]                  # 执行结果

# 事件流（WebSocket）
/api/chat/stream                             # SSE 流端点
```

---

## 需要新增的埋点

### 优先级 1（高）：立即补充

| 位置 | 字段 | 类型 | 用途 |
|------|------|------|------|
| `TaskAssignment.metadata` | `tools_called` | List[str] | 此任务调用的工具列表 |
| `TaskAssignment.metadata` | `tool_details` | Dict[str, dict] | 工具调用细节（参数、输出）|
| 新建 Event | `tool_execution` | dict | 工具执行事件（实时） |

### 优先级 2（中）：完善可视化

| 位置 | 字段 | 类型 | 用途 |
|------|------|------|------|
| `DistributedState` | `execution_timeline` | List[dict] | 任务执行时间线 |
| 新增端点 | `GET /api/execution/{workflow_id}` | JSON | 获取完整执行历史 |
| 新增端点 | `GET /api/agents/topology` | JSON | 实时拓扑结构 |

### 优先级 3（低）：高级可视化

| 位置 | 字段 | 类型 | 用途 |
|------|------|------|------|
| `DistributedState` | `resource_utilization` | dict | 资源使用情况 |
| `DistributedState` | `cost_metrics` | dict | 成本统计（若适用） |

---

## 推荐数据获取方式

### 场景 1：编排过程（Skills + 可用 Agent）

```python
# 方式 A：直接读取 State（推荐，最快）
skills = state.get("skills_content", "")
pipeline_topo = state.get("pipeline_topology", [])

# 方式 B：查询 API（远程获取）
registry = get_registry_client()
agents = registry.get_all_agents()

# 方式 C：订阅事件（实时）
# 监听 Planner 节点输出消息
planner_msg = [m for m in state["messages"] if m.name == "planner"][-1]
```

### 场景 2：拓扑图

```python
# 方式 A：直接读 state.execution_plan（推荐）
execution_plan = state.get("execution_plan", [])
cross_host = state.get("cross_host_sessions", {})

# 方式 B：HTTP API 端点（新增）
# GET /api/execution/{workflow_id}/topology

# 方式 C：从日志解析（不推荐）
# grep "tasks=" distributed_nodes.log
```

### 场景 3：执行监控（实时）

```python
# 方式 A：WebSocket SSE 订阅（推荐，最实时）
# WebSocket /api/chat/stream
# 接收 {"event": "task_update", "data": {...}}

# 方式 B：轮询 API（非推荐，延迟高）
# GET /api/execution/{workflow_id}/current-task

# 方式 C：读取完整 State（有延迟）
# state["execution_plan"][state["current_task_index"]]
```

---

## 已有接口与新增需求

### 已有接口

| 端点 | 方法 | 用途 | 是否可用于可视化 |
|------|------|------|--------|
| `/api/chat/stream` | POST | 主编排流程 | ✅ 是，SSE 流 |
| `/registry/sync` | POST | Agent gossip 同步 | ❌ 不适用 |
| `GET /health` | GET | 健康检查 | ✅ 可用于 UI 连接检测 |
| `POST /orchestration/dispatch` | POST | 跨主体子任务 | ❌ 内部接口 |

### 需要新增的接口

```python
# 1. 获取执行历史
GET /api/execution/{workflow_id}
{
  "workflow_id": str,
  "status": "running" | "completed" | "failed",
  "execution_plan": List[TaskAssignment],
  "current_task_index": int,
  "cross_host_sessions": Dict,
  "progress_ledger": dict,
  "timeline": List[{timestamp, task_id, status}]
}

# 2. 获取实时拓扑
GET /api/agents/topology
{
  "nodes": [
    {
      "id": "agent_id",
      "ip": "ip:port",
      "capability": "str",
      "status": "online|offline",
      "current_tasks": int
    }
  ],
  "edges": [
    {
      "from": "task_id",
      "to": "task_id",
      "type": "sequence" | "parallel"
    }
  ]
}

# 3. 获取工具调用详情
GET /api/execution/{workflow_id}/tools
[
  {
    "task_id": str,
    "tool_name": str,
    "parameters": dict,
    "output": str,
    "duration_ms": int
  }
]

# 4. WebSocket 事件流（已有，但需要明确事件定义）
WS /api/execution/events/{workflow_id}
```

---

## 数据流示意图

```
用户请求
  ↓
[Planner 节点]
  ├─ 读取 state.skills_content & state.pipeline_topology
  ├─ 查询 ARDC: registry.get_all_agents()
  ├─ LLM 规划（若无管道）
  └─ 输出 execution_plan + cross_host_sessions
       ↓
  [WebSocket 广播 plan_ready]
       ↓
[Executor 节点]（循环每个任务）
  ├─ 取出 execution_plan[current_task_index]
  ├─ 判断是否跨主体（check cross_host_sessions）
  ├─ 执行 (MCP | A2A | LLM-Simulator)
  ├─ 记录 metadata（protocol, executor）
  └─ 输出 execution_plan[i].result + 状态
       ↓
  [WebSocket 广播 task_completed]
       ↓
[Monitor 节点]（判断后续）
  ├─ 检查 failed_tasks
  ├─ 触发重规划（若需要）
  └─ 或结束工作流
```

---

## 性能与可扩展性考量

### 推荐架构

```
┌─────────────────────────────────────────┐
│ Web UI (可视化前端)                      │
│ ├─ Mermaid.js (拓扑图)                   │
│ ├─ D3.js (实时时间线)                    │
│ └─ WebSocket (SSE 流)                    │
└──────────┬──────────────────────────────┘
           │
┌──────────▼──────────────────────────────┐
│ FastAPI 后端 (/api/chat/stream)         │
│ ├─ EventSourceResponse (SSE)             │
│ └─ 新增：                                 │
│   ├─ GET /api/execution/{id}             │
│   ├─ GET /api/agents/topology            │
│   └─ GET /api/execution/{id}/tools       │
└──────────┬──────────────────────────────┘
           │
┌──────────▼──────────────────────────────┐
│ LangGraph Workflow (state 管理)          │
│ ├─ DistributedState (execution_plan)    │
│ ├─ execution_timeline (新增)             │
│ └─ Event 发布 (demo_bus)                │
└──────────────────────────────────────────┘
```

### 内存优化建议

- **execution_plan 长度**：建议限制单个工作流最多 100-200 个任务
- **执行历史保留**：最多保留最近 10 个完成的工作流
- **日志队列大小**：限制 1000 条日志条目，超出滚动删除

---

## 总结与建议

### 三个场景的数据完整性评分

| 场景 | 现有完整度 | 关键缺失 | 优先级 |
|------|----------|--------|--------|
| **编排过程** | ⭐⭐⭐⭐⭐ (100%) | 无 | - |
| **拓扑图** | ⭐⭐⭐⭐☆ (80%) | 详细故障链路、恢复历史 | 中 |
| **执行监控** | ⭐⭐⭐☆☆ (60%) | 工具级记录、详细事件流、时间线 | 高 |

### 快速开发建议

**第 1 阶段（1-2 天）**：
1. 复用现有 `/api/chat/stream` SSE 流
2. 在 executor 节点添加 `execution_timeline` 记录
3. 前端用 Chart.js 画时间线

**第 2 阶段（2-3 天）**：
1. 新增 `GET /api/execution/{id}/topology` 端点
2. 前端用 Mermaid.js 渲染拓扑图（动态）
3. 添加跨主体高亮

**第 3 阶段（3-5 天）**：
1. 完善工具调用埋点（TaskAssignment.metadata）
2. 新增 `GET /api/execution/{id}/tools` 端点
3. 前端展示工具链路图

---

