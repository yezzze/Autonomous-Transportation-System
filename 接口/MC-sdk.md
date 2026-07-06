# MC SDK (SandboxMemory) 实现方案

## 1. 核心原则

```
Agent 沙箱不能直接访问 MC 全量存储
Agent 只能访问自己的 /sandbox/memory/ 目录
MC 负责：从存储取记忆 → 物化到临时目录
Agent 负责：读 input/ + local/ → 写 output/
MC 负责 sidecar 回收 output → 写回存储
```

---

## 2. 文件结构

```
src/
├── sdk/
│   ├── __init__.py
│   ├── memory_sdk.py         # Agent 侧 SandboxMemory SDK（只操作文件系统）
│   ├── memory_models.py      # Pydantic 数据模型
│   ├── mc_service.py         # MC 服务端：存储 ↔ 临时目录 的编排逻辑
│   └── mc_router.py          # FastAPI Router：MC REST API 端点
└── api/
    └── app.py                # 追加 from src.sdk.mc_router import mc_router
```

---

## 3. 数据模型 (`memory_models.py`)

```python
# ============================================================
# Bundle 模型
# ============================================================

class MemoryBundle(BaseModel):
    """记忆包：描述一个 Agent 实例的专属临时记忆目录"""
    bundle_id: str
    owner_device_id: str
    owner_user_id: str
    workflow_id: str
    task_id: str
    agent_id: str
    agent_instance_id: str
    mount_path: str = "/sandbox/memory"
    visibility: Literal["local", "delegated"]
    created_at: str
    expires_at: Optional[str]
    policy: MemoryPolicy

class MemoryPolicy(BaseModel):
    input_readonly: bool = True
    output_collect: bool = True
    allow_agent_read_local_memory: bool = True
    allow_agent_write_local_memory: bool = True
    allow_writeback_to_caller: bool = False
    allow_save_raw_input: bool = False
    allow_write_artifacts: bool = True

# ============================================================
# 沙箱目录中的文件模型
# ============================================================

class Manifest(BaseModel):
    """/sandbox/memory/manifest.json"""
    bundle_id: str
    workflow_id: str
    task_id: str
    agent_id: str
    agent_instance_id: str
    created_at: str
    expires_at: Optional[str]
    policy: MemoryPolicy

class CallerInfo(BaseModel):
    """input/caller_info.json"""
    caller_device_id: str
    caller_workflow_id: str
    caller_session_id: Optional[str] = None

class DelegatedContext(BaseModel):
    """input/delegated_context.json (跨设备)"""
    task_summary: str
    allowed_memories: list = []
    constraints: dict = {}

class WritebackEntry(BaseModel):
    """output/writeback_to_caller.jsonl / writeback_to_local.jsonl 的一行"""
    memory_type: str
    target_owner: Literal["caller", "local_agent"]
    content: str
    confidence: Optional[float] = None
    agent_id: Optional[str] = None
    caller_device_id: Optional[str] = None
    caller_workflow_id: Optional[str] = None
    metadata: dict = {}
    timestamp: str = ""

# ============================================================
# API 请求/响应模型 (MC 服务端 ↔ SDK/编排层)
# ============================================================

class CreateBundleRequest(BaseModel):
    task_id: str
    agent_id: str
    agent_instance_id: str
    workflow_id: str
    device_id: str
    user_id: str
    memory_policy: Optional[MemoryPolicy] = None
    caller_info: Optional[CallerInfo] = None

class CreateBundleResponse(BaseModel):
    bundle_id: str
    memory_mount_spec: dict  # ASD 部署时所需

class OutboxUploadRequest(BaseModel):
    bundle_id: str
    result: Optional[dict] = None
    execution_notes: Optional[str] = None
    writeback_to_caller: list[WritebackEntry] = []
    writeback_to_local: list[WritebackEntry] = []
    artifacts: dict[str, str] = {}  # filename → base64

class MemorySearchRequest(BaseModel):
    query: str
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    top_k: int = 10

class MemoryWriteRequest(BaseModel):
    content: str
    memory_type: str = "agent_experience"
    agent_id: str
    workflow_id: Optional[str] = None
    metadata: dict = {}
```

---

## 4. Agent 侧 SDK (`memory_sdk.py`)

### 核心类：`SandboxMemory`

**作用**：Agent 实例在沙箱内通过此 SDK 操作 `/sandbox/memory/` 目录。

```
SandboxMemory 不做"从存储取记忆"这件事——那是 MC 服务端的事。
SandboxMemory 只做：读已物化到文件系统的目录，写 output/ 让 sidecar 回收。
```

### 初始化

```python
class SandboxMemory:
    """
    Agent 沙箱记忆 SDK
    
    Agent 只能访问自己的 /sandbox/memory/ 目录。
    所有记忆数据已经由 MC 物化到文件系统，SDK 只负责读写文件和调用 API。
    """
    
    def __init__(
        self,
        memory_root: str = "/sandbox/memory",   # 默认沙箱挂载点
        mc_api_url: Optional[str] = None,        # MC 服务地址（可选，用于上报存量等）
        agent_id: Optional[str] = None,           # 当前 Agent ID
        agent_instance_id: Optional[str] = None,  # 当前实例 ID
        auto_discover: bool = True,              # 自动从 manifest.json 发现身份
    ):
```

### 文件系统方法（核心）

| 方法 | 文件路径 | 说明 |
|------|----------|------|
| `get_memory_root() -> Path` | — | 获取内存根目录路径 |
| `read_manifest() -> Manifest` | `manifest.json` | 读取清单 |
| `read_task_description() -> str` | `input/task.md` | 读取任务描述 |
| `read_caller_info() -> CallerInfo` | `input/caller_info.json` | 读取调用方信息 |
| `read_policy() -> MemoryPolicy` | `input/policy.json` | 读取记忆策略 |
| `read_delegated_context() -> DelegatedContext` | `input/delegated_context.json` | 读取委派上下文 |
| `read_constraints() -> dict` | `input/constraints.json` | 读取约束 |
| `read_agent_profile() -> dict` | `local/agent_profile.json` | 读取 Agent 身份 |
| `read_agent_memory_excerpt() -> dict` | `local/agent_memory_excerpt.json` | 读取 Agent 经验摘录 |
| `read_device_context() -> dict` | `local/device_public_context.json` | 读取设备上下文 |
| `write_result(result: dict)` | `output/result.json` | 写入执行结果 |
| `write_execution_notes(notes: str)` | `output/execution_notes.md` | 写入执行笔记 |
| `write_to_caller(entry: WritebackEntry)` | `output/writeback_to_caller.jsonl` | 追加写回调调用方的记忆 |
| `write_to_local(entry: WritebackEntry)` | `output/writeback_to_local.jsonl` | 追加写回本地的经验 |
| `write_artifact(filename: str, data: bytes)` | `output/artifacts/{filename}` | 写入工件文件 |
| `list_input_files() -> list[Path]` | `input/` | 列举输入文件 |
| `list_output_files() -> list[Path]` | `output/` | 列举输出文件 |
| `has_output(key: str) -> bool` | 检查某项 output 是否存在 |
| `append_to_file(sub_path: str, content: str)` | 通用追加写入（用于自定义 output） |

### API 调用方法（可选，用于获取路径/上报）

| 方法 | 说明 |
|------|------|
| `get_memory_path_remote() -> dict` | 通过 API 向 MC 查询当前实例的 memory 路径 |
| `notify_task_started() -> bool` | 通知 MC 任务已开始 |
| `notify_task_completed() -> bool` | 通知 MC 任务已完成 |

### 便捷组合方法

```python
async def load_all_inputs(self) -> dict:
    """一次加载所有输入：manifest + task + policy + caller_info + local信息"""

async def finalize(self, result: dict, notes: str = ""):
    """快捷收尾：写 result + 写 notes + 通知 MC + 写 output/.done"""
```

### 使用示例（Agent 内部）

```python
from src.sdk import SandboxMemory

async def agent_main():
    mem = SandboxMemory()  # 默认 /sandbox/memory
    
    # 读取任务描述
    task = await mem.read_task_description()
    print(f"任务: {task}")
    
    # 读取本地经验
    excerpt = await mem.read_agent_memory_excerpt()
    print(f"历史经验: {excerpt}")
    
    # 执行任务...
    result = execute_task(task, excerpt)
    
    # 写回结果
    await mem.write_result({"status": "success", "data": result})
    await mem.write_execution_notes("任务执行完成，处理时间2.3s")
    
    # 写回记忆（供 MC 回收后入库）
    await mem.write_to_local(WritebackEntry(
        memory_type="agent_experience",
        target_owner="local_agent",
        content="视觉识别任务完成，准确率95%",
        confidence=0.95,
    ))
    
    # 通知 MC 任务完成（如果配置了 mc_api_url）
    await mem.notify_task_completed()
```

---

## 5. MC 服务端 (`mc_service.py`)

负责：存储中心 ↔ 临时目录 ↔ sidecar 回收。

### 核心类：`MemoryCenterService`

```python
class MemoryCenterService:
    """
    记忆中心 (MC) 服务端
    
    职责：
    1. 从存储中心取出记忆 → 物化成临时目录
    2. 管理临时目录的生命周期
    3. 回收 Agent output → 写回存储中心
    
    存储中心设计：
    - 初期：使用本地文件系统 + JSON 文件（轻量）
    - 后续可替换为：向量数据库、关系数据库、对象存储
    """
    
    def __init__(
        self,
        store_root: str = "./data/memory-store",      # 长期记忆存储
        bundles_root: str = "./data/memory-bundles",   # 临时目录根
    )
```

### 存储目录结构

```
data/
├── memory-store/                      ← 长期记忆库（MC 管理）
│   ├── user_memory/
│   │   └── {user_id}/
│   │       └── {memory_id}.json
│   ├── agent_memory/
│   │   └── {agent_id}/
│   │       └── {memory_id}.json
│   └── workflow_summary/
│       └── {workflow_id}.json
│
└── memory-bundles/                    ← 临时目录（MC 创建，Pod 销毁后清理）
    └── {workflow_id}/
        └── {agent_instance_id}/       ← 这个目录被挂载到 Pod 的 /sandbox/memory
            ├── manifest.json
            ├── input/
            ├── local/
            └── output/                ← Agent 写入，sidecar 回收
```

### 方法

#### 5.1 记忆包创建与物化

```python
async def create_bundle(self, req: CreateBundleRequest) -> CreateBundleResponse:
    """
    1. 在 bundles_root 下创建临时目录
    2. 从 store 取出用户记忆 + Agent 经验
    3. 过滤、摘要、脱敏 → 写入 input/ + local/
    4. 生成 manifest.json
    5. 返回 bundle_id + mount_spec
    """

async def materialize_bundle(self, bundle_id: str):
    """将 Bundle 信息物化到文件系统目录"""
```

#### 5.2 output 回收

```python
async def collect_outbox(self, bundle_id: str) -> dict:
    """
    1. 读取 output/ 下的所有文件
    2. 按 target_owner 分流：
       - writeback_to_caller → 返回给调用方
       - writeback_to_local  → 写入本地存储
       - result.json         → 保存
       - execution_notes.md  → 保存
       - artifacts/          → 保存
    3. 返回整理后的数据
    """

async def commit_bundle(self, bundle_id: str):
    """
    1. collect_outbox
    2. 将候选记忆写入长期存储
    3. 标记 Bundle 为已提交
    """
```

#### 5.3 记忆查询与写入（存储层）

```python
async def search_memories(self, req: MemorySearchRequest) -> list[dict]:
    """从长期存储中搜索记忆"""
    
async def write_memory(self, entry: MemoryWriteRequest) -> str:
    """写入一条记忆到长期存储"""
    
async def batch_write_memories(self, entries: list) -> int:
    """批量写入"""
    
async def get_memory(self, memory_id: str) -> Optional[dict]:
    """获取单条记忆"""
    
async def delete_memory(self, memory_id: str) -> bool:
    """删除记忆"""
```

#### 5.4 目录与生命周期

```python
async def delete_bundle(self, bundle_id: str):
    """删除临时目录"""

async def close_workflow_session(self, workflow_id: str):
    """清理整个 workflow 的临时目录"""

async def get_bundle_info(self, bundle_id: str) -> Optional[MemoryBundle]:
    """获取 Bundle 信息"""
```

---

## 6. REST API (`mc_router.py`)

使用 FastAPI APIRouter，注册到 `src/api/app.py` 的 `app.include_router(mc_router)`。

### 6.1 编排层调用接口（AOE / ASD / AWM → MC）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/memory/bundles/create` | AOE 创建记忆包 |
| POST | `/memory/bundles/{bundle_id}/materialize` | 物化 Bundle |
| POST | `/memory/bundles/{bundle_id}/collect_outbox` | AWM 回收 output |
| POST | `/memory/bundles/{bundle_id}/commit` | 提交 Bundle |
| DELETE | `/memory/bundles/{bundle_id}` | 删除 Bundle |

### 6.2 SDK / Agent 调用接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/memory/session/{instance_id}` | Agent 查询自己记忆目录的挂载信息 |
| POST | `/memory/session/{instance_id}/notify_start` | Agent 通知任务开始 |
| POST | `/memory/session/{instance_id}/notify_complete` | Agent 通知任务完成 |

### 6.3 记忆查询（MC → 存储）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/memory/search` | 搜索记忆 |
| POST | `/memory/write` | 写入记忆 |
| POST | `/memory/batch_write` | 批量写入 |
| GET | `/memory/{memory_id}` | 获取单条 |
| DELETE | `/memory/{memory_id}` | 删除 |

### 6.4 跨设备委派

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/memory/delegations/create` | 创建委派包 |
| POST | `/memory/delegations/accept` | 接收委派包 |
| POST | `/memory/delegations/{id}/writeback` | 委派写回 |
| DELETE | `/memory/delegations/{id}` | 关闭委派 |

---

## 7. 完整流程

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 1: MC 创建记忆包（编排层 AOE 触发）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AOE → POST /memory/bundles/create
  │  {task_id, agent_id, workflow_id, device_id, user_id, memory_policy}
  │
  ▼
MC.create_bundle()
  1. 在 data/memory-bundles/{workflow_id}/{agent_instance_id}/ 创建目录
  2. 从 data/memory-store/ 中检索该 user 的相关记忆
  3. 从 data/memory-store/ 中检索该 agent 的历史经验
  4. 过滤、摘要、脱敏 → 写入 input/ + local/
  5. 生成 manifest.json
  6. 返回 {bundle_id, memory_mount_spec}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 2: ASD 部署 Agent（挂载临时目录）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ASD → 创建 Pod
  │  将 memory-bundles/{wf_id}/{inst_id} 挂载到 /sandbox/memory
  │  （K8s: emptyDir + initContainer 拉取）
  ▼
Agent Pod 启动
  └── /sandbox/memory/ 已包含 input/ + local/ + manifest.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 3: Agent 执行（使用 SandboxMemory SDK）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

mem = SandboxMemory()
  │  auto_discover=True → 读取 manifest.json 自动获取身份
  │
  ├─ mem.read_task_description()      → input/task.md
  ├─ mem.read_agent_memory_excerpt()  → local/agent_memory_excerpt.json
  ├─ mem.read_caller_info()           → input/caller_info.json
  │
  ├─ 执行任务...
  │
  ├─ mem.write_result({...})          → output/result.json
  ├─ mem.write_execution_notes(...)   → output/execution_notes.md
  ├─ mem.write_to_local(entry)        → output/writeback_to_local.jsonl (追加)
  ├─ mem.write_to_caller(entry)       → output/writeback_to_caller.jsonl (追加)
  ├─ mem.write_artifact("img.png", data) → output/artifacts/img.png
  │
  └─ mem.notify_task_completed()      → POST /memory/session/{id}/notify_complete

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 4: MC 回收 output（AWM 触发）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AWM → POST /memory/bundles/{bundle_id}/collect_outbox
  │
  ▼
MC.collect_outbox()
  1. 读取 output/result.json
  2. 读取 output/execution_notes.md
  3. 读取 output/writeback_to_caller.jsonl
  4. 读取 output/writeback_to_local.jsonl
  5. 读取 output/artifacts/
  │
  ├─ writeback_to_local  → 写入 data/memory-store/agent_memory/ (本地经验)
  ├─ writeback_to_caller → 返回给调用方
  ├─ result.json         → 写入 workflow_summary
  └─ 标记输出已回收

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 5: 清理（Pod 销毁后）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AWM → DELETE /memory/bundles/{bundle_id}
  │
  ▼
MC.delete_bundle() → 删除临时目录
```
