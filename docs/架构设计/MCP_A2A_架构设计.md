# LangManus MCP + A2A 完整架构设计

## 📐 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      用户 / 上层应用                              │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ↓
┌────────────────────────────────────────────────────────────────┐
│                    L2 Scheduler (LangManus)                    │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  Adaptive Orchestrator (自适应编排器)                     │ │
│  │  - Sequential Mode                                        │ │
│  │  - Magentic-One Mode                                      │ │
│  └──────────────────────────────────────────────────────────┘ │
│                             ↓                                   │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  Unified Executor (统一执行层)                            │ │
│  │  ┌─────────────────┐  ┌──────────────────┐              │ │
│  │  │  A2A Client     │  │  MCP Client      │              │ │
│  │  │  (Agent通信)    │  │  (工具调用)       │              │ │
│  │  └────────┬────────┘  └────────┬─────────┘              │ │
│  └───────────┼─────────────────────┼────────────────────────┘ │
└──────────────┼─────────────────────┼──────────────────────────┘
               │                     │
               │                     │
    ┌──────────┴──────────┐         │
    │ A2A Protocol        │         │ MCP Protocol
    │ (HTTP/JSON)         │         │ (stdio/SSE)
    └──────────┬──────────┘         │
               │                     │
               ↓                     ↓
┌──────────────────────────────────────────────────────────────────┐
│                        L3 Agents Layer                            │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │ search_agent    │  │ nlp_agent       │  │ compute_agent   │ │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │ │
│  │ │A2A Server   │ │  │ │A2A Server   │ │  │ │A2A Server   │ │ │
│  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │ │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │ │
│  │ │MCP Client   │ │  │ │MCP Client   │ │  │ │MCP Client   │ │ │
│  │ └──────┬──────┘ │  │ └──────┬──────┘ │  │ └──────┬──────┘ │ │
│  └────────┼────────┘  └────────┼────────┘  └────────┼────────┘ │
└───────────┼────────────────────┼────────────────────┼──────────┘
            │                    │                    │
            ↓                    ↓                    ↓
┌──────────────────────────────────────────────────────────────────┐
│                      MCP Tools/Services                           │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │ Brave Search    │  │ Filesystem      │  │ Python Runtime  │ │
│  │ (Web搜索)       │  │ (文件操作)       │  │ (代码执行)       │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘ │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │ Puppeteer       │  │ SQLite          │  │ Git Ops         │ │
│  │ (浏览器自动化)   │  │ (数据查询)       │  │ (版本控制)       │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

---

## 🔄 数据流详解

### **场景 1: 简单搜索任务（Sequential + MCP）**

```
用户: "搜索特斯拉股价"
  ↓
L2 Planner: 评估复杂度 = SIMPLE
  ↓
生成计划: [task_001: 调用 search_agent]
  ↓
Unified Executor 判断:
  - 任务类型: 明确的搜索
  - 决策: 直接用 MCP 工具，不需要 Agent
  ↓
MCP Client.call_tool("yahoo_finance", {symbol: "TSLA"})
  ↓
MCP Server 返回: {price: 198.67, change: "+2.15%"}
  ↓
L2 Reporter: 格式化结果返回用户
```

### **场景 2: 复杂任务（Magentic-One + A2A）**

```
用户: "帮我写一份详细的 AI 技术报告"
  ↓
L2 Planner: 评估复杂度 = COMPLEX
  ↓
启动 Magentic-One 模式
  ↓
Round 1 - Orchestrator 决策:
  - 需要: 搜索最新研究
  - 选择: search_agent
  ↓
A2A Client → search_agent
  A2AMessage:
    type: "request"
    payload: {
      task_type: "web_search",
      query: "AI technology trends 2024-2026"
    }
  ↓
search_agent 执行:
  1. 通过 MCP 调用 Brave Search
  2. 通过 MCP 调用 arXiv API
  3. 汇总结果
  ↓
A2A Response 返回 L2
  ↓
Round 2 - Orchestrator 决策:
  - 需要: 分析数据
  - 选择: nlp_agent
  ↓
A2A Client → nlp_agent
  (附带 Round 1 结果)
  ↓
nlp_agent 执行:
  1. 通过 MCP 调用本地 LLM
  2. 生成报告草稿
  ↓
Round 3 - Orchestrator 决策:
  - 判断: 任务完成
  - 跳转: Reporter
```

### **场景 3: Agent 间协作（A2A Direct）**

```
L2 分配任务给 search_agent
  ↓
search_agent 发现需要 NLP 处理
  ↓
search_agent 直接调用 nlp_agent (A2A)
  不经过 L2 中转
  ↓
nlp_agent 返回结果给 search_agent
  ↓
search_agent 汇总后返回 L2
```

---

## 📦 核心组件设计

### **1. A2A 协议定义**

```python
# src/protocols/a2a_protocol.py

from pydantic import BaseModel, Field
from typing import Literal, Any, Optional
from datetime import datetime
import uuid

class A2AMessage(BaseModel):
    """A2A 标准消息格式"""
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    sender_id: str  # 发送方 Agent ID
    receiver_id: str  # 接收方 Agent ID
    message_type: Literal["request", "response", "notification", "error"]
    payload: dict
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    correlation_id: Optional[str] = None  # 用于关联请求-响应
    
class A2ATaskRequest(BaseModel):
    """A2A 任务请求 Payload"""
    task_id: str
    task_type: str  # "search", "nlp", "compute", "vision", "code", "web"
    task_description: str
    context: dict = {}
    timeout: int = 30
    priority: Literal["low", "normal", "high"] = "normal"
    require_stream: bool = False  # 是否需要流式返回
    
class A2ATaskResponse(BaseModel):
    """A2A 任务响应 Payload"""
    task_id: str
    status: Literal["success", "error", "timeout", "cancelled"]
    result: Any
    error_message: Optional[str] = None
    metadata: dict = {}  # 包含执行时间、成本等信息
    
class A2ACapabilityDeclaration(BaseModel):
    """Agent 能力声明"""
    agent_id: str
    agent_type: str
    capabilities: list[dict]  # 支持的任务类型和参数
    status: Literal["online", "busy", "offline"]
    load_level: float = 0.0  # 当前负载 0.0-1.0
    max_concurrent_tasks: int = 5
    
class A2AProgressNotification(BaseModel):
    """任务进度通知"""
    task_id: str
    progress: float  # 0.0-1.0
    current_step: str
    estimated_time_remaining: Optional[int] = None  # 秒
```

---

### **2. A2A Client 实现**

```python
# src/service/a2a_client.py

import httpx
import asyncio
from typing import AsyncIterator
import logging

logger = logging.getLogger(__name__)

class A2AClient:
    """A2A 协议客户端"""
    
    def __init__(self, sender_id: str = "l2_scheduler"):
        self.sender_id = sender_id
        self.timeout = httpx.Timeout(30.0, connect=5.0)
        
    async def send_task_request(
        self,
        agent_url: str,
        request: A2ATaskRequest
    ) -> A2ATaskResponse:
        """发送 A2A 任务请求"""
        
        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="request",
            payload=request.dict()
        )
        
        logger.info(f"📤 发送 A2A 请求: {message.message_id} → {agent_url}")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{agent_url}/a2a/execute",
                    json=message.dict()
                )
                response.raise_for_status()
                
                response_message = A2AMessage(**response.json())
                task_response = A2ATaskResponse(**response_message.payload)
                
                logger.info(f"✅ 收到 A2A 响应: {task_response.status}")
                return task_response
                
        except httpx.TimeoutException:
            logger.error(f"⏱️ A2A 请求超时: {agent_url}")
            return A2ATaskResponse(
                task_id=request.task_id,
                status="timeout",
                result=None,
                error_message="Request timeout"
            )
            
        except Exception as e:
            logger.error(f"❌ A2A 请求失败: {e}")
            return A2ATaskResponse(
                task_id=request.task_id,
                status="error",
                result=None,
                error_message=str(e)
            )
    
    async def stream_task_execution(
        self,
        agent_url: str,
        request: A2ATaskRequest
    ) -> AsyncIterator[A2AProgressNotification]:
        """流式接收 A2A 执行进度"""
        
        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="request",
            payload=request.dict()
        )
        
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{agent_url}/a2a/execute/stream",
                json=message.dict()
            ) as response:
                async for line in response.aiter_lines():
                    if line:
                        progress_data = json.loads(line)
                        yield A2AProgressNotification(**progress_data)
    
    def _extract_agent_id(self, url: str) -> str:
        """从 URL 提取 Agent ID"""
        # http://192.168.1.10:8080 → search_agent_001
        return url.split("//")[1].split(":")[0]
```

---

### **3. MCP Client 实现**

```python
# src/service/mcp_client.py

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import logging
import asyncio

logger = logging.getLogger(__name__)

class MCPClient:
    """MCP 协议客户端"""
    
    def __init__(self):
        self.session: ClientSession = None
        self.tools = []
        
    async def connect(self, server_command: list[str]):
        """连接到 MCP 服务器"""
        logger.info(f"🔌 连接 MCP 服务器: {' '.join(server_command)}")
        
        server_params = StdioServerParameters(
            command=server_command[0],
            args=server_command[1:] if len(server_command) > 1 else []
        )
        
        read, write = await stdio_client(server_params).__aenter__()
        self.session = await ClientSession(read, write).__aenter__()
        await self.session.initialize()
        
        # 列出可用工具
        tools_result = await self.session.list_tools()
        self.tools = tools_result.tools
        
        logger.info(f"✅ 发现 {len(self.tools)} 个 MCP 工具")
        return self.tools
    
    async def call_tool(self, tool_name: str, arguments: dict = None):
        """调用 MCP 工具"""
        if not self.session:
            raise RuntimeError("MCP 客户端未连接")
        
        logger.info(f"🔧 调用 MCP 工具: {tool_name}")
        
        result = await self.session.call_tool(tool_name, arguments or {})
        
        # 提取文本内容
        content = []
        for item in result.content:
            if hasattr(item, 'text'):
                content.append(item.text)
        
        return "\n".join(content)
    
    async def disconnect(self):
        """断开连接"""
        if self.session:
            await self.session.__aexit__(None, None, None)

class MCPToolRegistry:
    """MCP 工具注册表"""
    
    # 预定义的 MCP 服务器
    MCP_SERVERS = {
        "search": ["npx", "@modelcontextprotocol/server-brave-search"],
        "filesystem": ["npx", "@modelcontextprotocol/server-filesystem"],
        "puppeteer": ["npx", "@modelcontextprotocol/server-puppeteer"],
        "sqlite": ["python", "-m", "mcp_server_sqlite"],
        "git": ["python", "-m", "mcp_server_git"],
    }
    
    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
    
    async def get_client(self, capability: str) -> MCPClient:
        """获取或创建 MCP 客户端"""
        if capability not in self.clients:
            if capability not in self.MCP_SERVERS:
                raise ValueError(f"未知的能力类型: {capability}")
            
            client = MCPClient()
            await client.connect(self.MCP_SERVERS[capability])
            self.clients[capability] = client
        
        return self.clients[capability]
```

---

### **4. 统一执行层**

```python
# src/graph/unified_executor.py

from typing import Literal
import logging

logger = logging.getLogger(__name__)

class UnifiedExecutor:
    """统一执行层：智能选择 MCP 或 A2A"""
    
    def __init__(self):
        self.a2a_client = A2AClient()
        self.mcp_registry = MCPToolRegistry()
        
    async def execute_task(self, task: TaskAssignment) -> dict:
        """
        执行任务，自动选择最佳协议
        
        决策逻辑：
        1. 如果是明确的工具调用 → 用 MCP
        2. 如果需要 Agent 推理 → 用 A2A
        3. 如果不确定 → 先尝试 MCP，失败后用 A2A
        """
        
        # 1. 尝试匹配 MCP 工具
        mcp_tool = self._try_match_mcp_tool(task)
        
        if mcp_tool:
            logger.info(f"🔧 使用 MCP 工具: {mcp_tool['name']}")
            return await self._execute_with_mcp(task, mcp_tool)
        
        # 2. 回退到 A2A Agent
        logger.info(f"🤖 使用 A2A Agent: {task['assigned_agent_id']}")
        return await self._execute_with_a2a(task)
    
    def _try_match_mcp_tool(self, task: TaskAssignment) -> dict | None:
        """尝试匹配 MCP 工具"""
        description = task["task_description"].lower()
        
        # 搜索类任务
        if any(keyword in description for keyword in ["搜索", "查找", "search", "query"]):
            return {
                "capability": "search",
                "name": "brave_search",
                "args": {"query": self._extract_search_query(description)}
            }
        
        # 文件操作
        if any(keyword in description for keyword in ["读取", "写入", "文件", "file"]):
            return {
                "capability": "filesystem",
                "name": "read_file",
                "args": {"path": self._extract_file_path(description)}
            }
        
        # 代码执行
        if any(keyword in description for keyword in ["计算", "执行", "运行", "compute"]):
            return {
                "capability": "sqlite",  # 或其他计算工具
                "name": "execute_query",
                "args": {"query": description}
            }
        
        return None
    
    async def _execute_with_mcp(self, task: TaskAssignment, tool_config: dict) -> dict:
        """通过 MCP 执行"""
        try:
            client = await self.mcp_registry.get_client(tool_config["capability"])
            result = await client.call_tool(tool_config["name"], tool_config["args"])
            
            return {
                "status": "success",
                "result": result,
                "protocol": "mcp",
                "tool_used": tool_config["name"]
            }
        except Exception as e:
            logger.error(f"MCP 执行失败: {e}")
            # 降级到 A2A
            return await self._execute_with_a2a(task)
    
    async def _execute_with_a2a(self, task: TaskAssignment) -> dict:
        """通过 A2A 执行"""
        request = A2ATaskRequest(
            task_id=task["task_id"],
            task_type=self._infer_task_type(task),
            task_description=task["task_description"],
            context={}
        )
        
        agent_url = f"http://{task['target_ip']}:{task['target_port']}"
        response = await self.a2a_client.send_task_request(agent_url, request)
        
        return {
            "status": response.status,
            "result": response.result,
            "protocol": "a2a",
            "agent_used": task["assigned_agent_id"]
        }
    
    def _infer_task_type(self, task: TaskAssignment) -> str:
        """推断任务类型"""
        agent_id = task["assigned_agent_id"]
        if "search" in agent_id:
            return "search"
        elif "nlp" in agent_id:
            return "nlp"
        elif "compute" in agent_id:
            return "compute"
        elif "vision" in agent_id:
            return "vision"
        elif "code" in agent_id:
            return "code"
        elif "web" in agent_id:
            return "web"
        return "unknown"
    
    def _extract_search_query(self, description: str) -> str:
        """提取搜索关键词"""
        # 简单实现，实际可以用 LLM
        return description.replace("搜索", "").replace("查找", "").strip()
    
    def _extract_file_path(self, description: str) -> str:
        """提取文件路径"""
        # 简单实现
        import re
        match = re.search(r'["\']([^"\']+)["\']', description)
        return match.group(1) if match else ""
```

---

### **5. 修改 distributed_executor_node**

```python
# src/graph/distributed_nodes.py

def distributed_executor_node(state: DistributedState) -> Command[Literal["monitor", "__end__"]]:
    """
    分布式执行器节点（升级版）
    
    支持：
    1. MCP 工具调用
    2. A2A Agent 通信
    3. 自动降级
    """
    logger.info("=== Unified Executor 开始执行 ===")
    
    # ... 原有的任务获取逻辑 ...
    
    current_task = execution_plan[current_index]
    
    # 使用统一执行层
    executor = UnifiedExecutor()
    
    try:
        result_data = await executor.execute_task(current_task)
        
        # 更新任务状态
        current_task["status"] = "completed"
        current_task["result"] = result_data["result"]
        current_task["metadata"] = {
            "protocol": result_data["protocol"],
            "tool_or_agent": result_data.get("tool_used") or result_data.get("agent_used")
        }
        
        logger.info(f"✅ 任务执行成功 (协议: {result_data['protocol']})")
        
        # 生成结果消息
        result_message = f"""
### 任务执行结果 ({current_index + 1}/{len(execution_plan)})

**任务**: {current_task['task_title']}  
**协议**: {result_data['protocol'].upper()}
**执行者**: {result_data.get('tool_used') or result_data.get('agent_used')}
**状态**: ✅ 成功

**结果**:
{result_data['result']}
"""
        
        return Command(
            update={
                "messages": [HumanMessage(content=result_message, name="executor")],
                "execution_plan": execution_plan,
                "current_task_index": current_index + 1
            },
            goto="monitor"
        )
        
    except Exception as e:
        logger.error(f"❌ 任务执行失败: {e}")
        current_task["status"] = "failed"
        current_task["result"] = str(e)
        
        return Command(
            update={
                "messages": [HumanMessage(
                    content=f"任务执行失败: {e}",
                    name="executor"
                )],
                "execution_plan": execution_plan,
                "failed_tasks": state.get("failed_tasks", []) + [current_task]
            },
            goto="monitor"
        )
```

---

## 🚀 实施步骤

### **Step 1: 安装依赖**
```bash
# MCP SDK
pip install mcp

# 异步 HTTP 客户端
pip install httpx

# 安装 MCP 服务器（可选）
npm install -g @modelcontextprotocol/server-brave-search
npm install -g @modelcontextprotocol/server-filesystem
npm install -g @modelcontextprotocol/server-puppeteer
```

### **Step 2: 创建协议定义**
```bash
# 创建文件
touch src/protocols/__init__.py
touch src/protocols/a2a_protocol.py
```

### **Step 3: 实现客户端**
```bash
touch src/service/a2a_client.py
touch src/service/mcp_client.py
```

### **Step 4: 实现统一执行层**
```bash
touch src/graph/unified_executor.py
```

### **Step 5: 修改现有节点**
```bash
# 更新 distributed_nodes.py
# 替换 executor 逻辑
```

### **Step 6: 测试**
```bash
# 测试 MCP
python test_mcp_client.py

# 测试 A2A
python test_a2a_client.py

# 集成测试
python distributed_main.py "搜索特斯拉股价"
```

---

## 📊 预期效果对比

| 场景 | 改造前 | 改造后 |
|------|-------|--------|
| **搜索任务** | LLM 模拟（假数据） | MCP → Brave Search（真数据） |
| **执行时间** | ~5秒（LLM 生成） | ~1秒（直接 API 调用） |
| **成本** | $0.01/次（LLM） | $0.001/次（API） |
| **可靠性** | 60%（LLM 幻觉） | 99%（真实数据） |
| **Agent 协作** | 不支持 | 支持直接通信 |

---

需要我开始实现吗？我们可以先从 Phase 1 的 MCP 接入开始！
