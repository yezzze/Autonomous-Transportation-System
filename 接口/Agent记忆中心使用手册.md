# Agent 记忆中心使用手册

本文档面向运行在沙箱或 OpenSandbox/K8s Pod 内的业务 Agent，说明如何使用当前 SDK 访问记忆中心（MC）物化出的专属记忆目录，以及如何把执行结果和候选记忆保存回 MC。

对应代码：

- Agent 侧 SDK：`src/sdk/memory_sdk.py`
- 数据模型：`src/sdk/memory_models.py`
- MC 服务端：`src/sdk/mc_service.py`
- MC REST 路由：`src/sdk/mc_router.py`

## 1. 核心概念

Agent 不直接访问 MC 的全量长期记忆库。每次任务启动前，MC 会为当前 Agent 实例生成一个专属 MemoryBundle，并由编排层挂载到沙箱内：

```text
/sandbox/memory/
  manifest.json
  input/
  local/
  output/
```

Agent 只能读自己的 `/sandbox/memory`，不能读其他 Agent、其他 Workflow 或 MC 本体的 `data/memory-store`。

目录职责：

| 路径 | 读写 | 说明 |
|------|------|------|
| `manifest.json` | 只读 | 当前 bundle、workflow、task、agent、策略信息 |
| `input/` | 只读 | 当前任务、调用方信息、约束、委派上下文 |
| `local/` | 只读 | MC 为当前 Agent 摘取的本地经验和设备公开上下文 |
| `output/` | 可写 | Agent 写执行结果、候选记忆、工件；sidecar/MC 后续回收 |

## 2. Agent 侧最小用法

```python
import asyncio

from src.sdk import SandboxMemory, WritebackEntry


async def main():
    mem = SandboxMemory()

    inputs = await mem.load_all_inputs()
    task = inputs["task"]
    agent_profile = inputs["agent_profile"]
    memory_excerpt = inputs["agent_memory_excerpt"]

    # 在这里执行业务逻辑
    result = {
        "status": "ok",
        "answer": "任务已完成",
        "used_memory_count": (memory_excerpt or {}).get("memory_count", 0),
        "agent": (agent_profile or {}).get("agent_id"),
    }

    await mem.write_result(result)
    await mem.write_execution_notes("读取了 task、agent_profile 和 agent_memory_excerpt。")

    await mem.write_to_local(WritebackEntry(
        memory_type="agent_experience",
        target_owner="local_agent",
        content="本次任务中，当前 Agent 成功完成了指定工作。",
        confidence=0.8,
        agent_id=mem.agent_id,
        metadata={"source": "agent_output"},
    ))


asyncio.run(main())
```

如果任务运行在 K8s emptyDir + sidecar 模式下，`SandboxMemory.finalize()` 会在写入 `result`、`notes` 并可选通知 MC 后，自动写完成信号：

```text
/sandbox/memory/output/.done
```

默认内容为 `done`。如果某个场景不希望写 `.done`，可以调用 `await mem.finalize(result, notes, write_done=False)`。

## 3. 初始化 SDK

```python
from src.sdk import SandboxMemory

mem = SandboxMemory()
```

默认读取 `/sandbox/memory`。SDK 会尝试读取 `manifest.json`，自动发现：

- `agent_id`
- `agent_instance_id`
- `bundle_id`
- `workflow_id`
- `task_id`
- `policy`

自定义路径：

```python
mem = SandboxMemory(memory_root="/custom/memory")
```

启用 MC HTTP 通知：

```python
mem = SandboxMemory(
    memory_root="/sandbox/memory",
    mc_api_url="http://host.minikube.internal:8000",
)
```

使用完 HTTP 模式后可以关闭客户端：

```python
await mem.close()
```

## 4. 读取任务和记忆

推荐优先使用一次性加载：

```python
inputs = await mem.load_all_inputs()
```

返回结构：

```python
{
    "manifest": Manifest | None,
    "task": str | None,
    "policy": MemoryBundlePolicy | None,
    "caller_info": CallerInfo | None,
    "delegated_context": DelegatedContext | None,
    "constraints": dict | None,
    "agent_profile": dict | None,
    "agent_memory_excerpt": dict | None,
    "device_context": dict | None,
}
```

也可以按需读取：

| 方法 | 文件 | 用途 |
|------|------|------|
| `await mem.read_manifest()` | `manifest.json` | 当前记忆包元信息 |
| `await mem.read_task_description()` | `input/task.md` | 当前任务描述 |
| `await mem.read_caller_info()` | `input/caller_info.json` | 调用方设备、Workflow、Task 信息 |
| `await mem.read_policy()` | `input/policy.json` | 当前读写策略 |
| `await mem.read_delegated_context()` | `input/delegated_context.json` | 跨设备委派上下文 |
| `await mem.read_constraints()` | `input/constraints.json` | 任务约束 |
| `await mem.read_agent_profile()` | `local/agent_profile.json` | 当前 Agent 身份、能力、状态 |
| `await mem.read_agent_memory_excerpt()` | `local/agent_memory_excerpt.json` | MC 摘取的本地经验 |
| `await mem.read_device_context()` | `local/device_public_context.json` | 设备公开上下文 |

辅助方法：

```python
mem.file_exists("local/agent_memory_excerpt.json")
files = mem.list_dir("local")
root = mem.memory_root
manifest = mem.manifest
agent_id = mem.agent_id
agent_instance_id = mem.agent_instance_id
```

## 5. 写入执行结果

任务结果写到 `output/result.json`：

```python
await mem.write_result({
    "status": "ok",
    "summary": "任务完成",
    "data": {"value": 123},
})
```

执行笔记写到 `output/execution_notes.md`：

```python
await mem.write_execution_notes("""
# 执行笔记

- 使用了本地经验摘录
- 生成了 result.json
""")
```

快捷收尾：

```python
await mem.finalize(
    result={"status": "ok", "summary": "done"},
    notes="任务已完成。",
)
```

注意：`finalize()` 会写 `result`、`notes`、可选通知 MC，并默认写 `output/.done`，供 K8s sidecar 回收 `output`。

## 6. 保存本地 Agent 经验

写回本地 Agent 长期记忆时，写入 `output/writeback_to_local.jsonl`：

```python
from src.sdk import WritebackEntry

await mem.write_to_local(WritebackEntry(
    memory_type="agent_experience",
    target_owner="local_agent",
    content="处理订单类任务时，应先校验输入字段，再调用后续工具。",
    confidence=0.9,
    agent_id=mem.agent_id,
    metadata={
        "task_type": "order_processing",
        "source": "runtime_observation",
    },
))
```

MC commit 后会把这些候选记忆写入：

```text
data/memory-store/agent_memory/{agent_id}/
```

适合写入本地记忆的内容：

- Agent 可复用的执行经验
- 工具调用注意事项
- 失败原因和规避方式
- 对当前 Agent 后续任务有价值的偏好或策略

不建议写入：

- 大段原始输入
- 未授权的调用方私有上下文
- 临时日志噪声
- 凭据、token、密钥

## 7. 写回调用方

跨设备委派或远端调用时，如果策略允许把结果写回调用方，写入 `output/writeback_to_caller.jsonl`：

```python
from src.sdk import WritebackEntry

caller = await mem.read_caller_info()

await mem.write_to_caller(WritebackEntry(
    memory_type="remote_collaboration_experience",
    target_owner="caller",
    content="远端 Agent 已完成图像分析，发现 3 个异常区域。该协作经验归调用方保存，后续调用其他设备执行同类任务时可复用。",
    confidence=0.85,
    agent_id=mem.agent_id,
    caller_device_id=caller.caller_device_id if caller else None,
    caller_workflow_id=caller.caller_workflow_id if caller else None,
    metadata={"result_type": "analysis_summary"},
))
```

写回调用方前应检查策略：

```python
policy = await mem.read_policy()

if policy and policy.allow_writeback_to_caller:
    await mem.write_to_caller(...)
```

如果策略不允许，不要写调用方私有上下文或原始输入。

## 8. 保存工件文件

二进制或文本工件写入 `output/artifacts/`：

```python
await mem.write_artifact("report.txt", b"hello\n")
await mem.write_artifact("chart.png", png_bytes)
```

sidecar/MC 会把 `output/artifacts/*` 作为工件回收。文件名应使用普通文件名，避免路径穿越：

```text
推荐: report.json, chart.png, result.csv
避免: ../../secret, /etc/passwd
```

## 9. 自定义输出日志

如果需要写自定义输出文件，可以追加到 `output/` 下：

```python
await mem.append_to_file("debug/events.log", "step 1 started")
await mem.append_to_file("debug/events.log", "step 1 completed")
```

这些文件是否被 MC 解析，取决于 sidecar/MC 的回收逻辑。标准写回仍应使用：

- `write_result()`
- `write_execution_notes()`
- `write_to_local()`
- `write_to_caller()`
- `write_artifact()`

## 10. 通知 MC

如果初始化时传了 `mc_api_url`，Agent 可以通知 MC 当前实例状态：

```python
mem = SandboxMemory(mc_api_url="http://host.minikube.internal:8000")

await mem.notify_task_started()

# 执行业务逻辑

await mem.notify_task_completed()
await mem.close()
```

也可以查询当前实例的 MC 侧路径信息：

```python
info = await mem.get_memory_path_remote()
```

对应接口：

```http
GET  /memory/session/{agent_instance_id}
POST /memory/session/{agent_instance_id}/notify_start
POST /memory/session/{agent_instance_id}/notify_complete
```

这些通知不等于写回长期记忆。长期记忆写回仍依赖 Agent 写 `output/`，然后由 sidecar/MC 上传并 commit。

## 11. 编排层和 MC REST API

以下接口由编排层、sidecar 或调试工具调用，不建议普通业务 Agent 直接调用。

### 11.1 创建 Workflow 记忆域

```http
POST /memory/scope/create
```

请求：

```json
{
  "device_id": "device-local",
  "user_id": "user-demo",
  "app_id": "app-demo",
  "workflow_id": "wf-001"
}
```

用途：AOE 在规划前让 MC 检索长期记忆，返回 `planner_memory_context`。

### 11.2 创建 Agent 记忆包

```http
POST /memory/bundles/create
```

请求：

```json
{
  "task_id": "task-a",
  "agent_id": "agent-a",
  "agent_instance_id": "agent-a-inst-001",
  "workflow_id": "wf-001",
  "device_id": "device-local",
  "user_id": "user-demo"
}
```

返回：

```json
{
  "code": 0,
  "message": "记忆包创建成功",
  "data": {
    "bundle_id": "mb_xxx",
    "memory_mount_spec": {
      "bundle_id": "mb_xxx",
      "mount_path": "/sandbox/memory",
      "input_mode": "readonly",
      "output_mode": "collect",
      "collector": "sidecar",
      "source_path": "data/memory-bundles/wf-001/agent-a-inst-001"
    }
  }
}
```

### 11.3 K8s initContainer 下载记忆包

```http
GET /memory/bundles/{bundle_id}/archive
```

返回 `tar.gz`，内容包括：

```text
manifest.json
input/
local/
```

不包含 `output/`。

### 11.4 Sidecar 上传 output

```http
POST /memory/bundles/{bundle_id}/upload_outbox
```

请求示例：

```json
{
  "result": {"status": "ok"},
  "execution_notes": "任务完成",
  "writeback_to_local": [
    {
      "memory_type": "agent_experience",
      "target_owner": "local_agent",
      "content": "可复用经验",
      "confidence": 0.8,
      "metadata": {}
    }
  ],
  "writeback_to_caller": [],
  "artifacts": {
    "report.txt": "base64..."
  }
}
```

### 11.5 提交写回

```http
POST /memory/bundles/{bundle_id}/commit
```

MC 会：

1. 回收 `output/`
2. 将 `writeback_to_local` 写入本地 Agent 记忆
3. 将 `writeback_to_caller` 写入调用方相关命名空间
4. 将 `result` 更新到 `workflow_summary`

### 11.6 查询长期记忆

```http
POST /memory/search
```

请求：

```json
{
  "query": "订单校验",
  "agent_id": "agent-a",
  "workflow_id": "wf-001",
  "user_id": "user-demo",
  "top_k": 10,
  "filters": {}
}
```

当前实现是轻量关键词匹配，后续可替换为向量检索。

### 11.7 直接写长期记忆

```http
POST /memory/write
```

请求：

```json
{
  "content": "这是一条长期记忆",
  "memory_type": "agent_experience",
  "agent_id": "agent-a",
  "workflow_id": "wf-001",
  "device_id": "device-local",
  "user_id": "user-demo",
  "metadata": {"source": "manual"}
}
```

普通沙箱 Agent 不建议直接调用该接口。优先写 `output/writeback_to_local.jsonl`，让 MC 在 commit 阶段统一处理权限和归属。

### 11.8 批量写长期记忆

```http
POST /memory/batch_write
```

请求：

```json
{
  "entries": [
    {
      "content": "经验 1",
      "memory_type": "agent_experience",
      "agent_id": "agent-a",
      "metadata": {}
    },
    {
      "content": "经验 2",
      "memory_type": "agent_experience",
      "agent_id": "agent-a",
      "metadata": {}
    }
  ]
}
```

### 11.9 单条记忆读删

```http
GET    /memory/{memory_id}
DELETE /memory/{memory_id}
```

## 12. 推荐 Agent 执行模板

```python
import asyncio
from pathlib import Path

from src.sdk import SandboxMemory, WritebackEntry


async def run_agent():
    mem = SandboxMemory(
        memory_root="/sandbox/memory",
        mc_api_url=None,  # 如需通知 MC，可填 MC 地址
    )

    await mem.notify_task_started()

    inputs = await mem.load_all_inputs()
    task = inputs["task"] or ""
    policy = inputs["policy"]
    caller = inputs["caller_info"]
    local_mem = inputs["agent_memory_excerpt"] or {}

    result = {
        "status": "ok",
        "task_excerpt": task[:200],
        "memory_count": local_mem.get("memory_count", 0),
    }

    if not policy or policy.allow_agent_write_local_memory:
        await mem.write_to_local(WritebackEntry(
            memory_type="agent_experience",
            target_owner="local_agent",
            content="本次任务执行成功，可复用当前处理流程。",
            confidence=0.8,
            agent_id=mem.agent_id,
            metadata={"task_id": mem.manifest.task_id if mem.manifest else None},
        ))

    if policy and policy.allow_writeback_to_caller and caller:
        await mem.write_to_caller(WritebackEntry(
            memory_type="remote_collaboration_experience",
            target_owner="caller",
            content="远端任务执行成功，该协作经验归调用方保存。",
            confidence=0.8,
            agent_id=mem.agent_id,
            caller_device_id=caller.caller_device_id,
            caller_workflow_id=caller.caller_workflow_id,
            metadata={},
        ))

    await mem.finalize(result, "Agent completed the task.")

    await mem.close()


if __name__ == "__main__":
    asyncio.run(run_agent())
```

## 13. 权限和安全要求

Agent 必须遵守以下规则：

1. 只读 `manifest.json`、`input/`、`local/`。
2. 只写 `output/`。
3. 不直接访问 MC 的 `data/memory-store/`。
4. 不读取其他 Agent 或其他 Workflow 的 memory bundle。
5. 不保存未授权的原始输入、调用方私有上下文、凭据或密钥。
6. 写回调用方前必须检查 `policy.allow_writeback_to_caller`。
7. 写本地长期经验前应检查 `policy.allow_agent_write_local_memory`。
8. 工件文件名不得包含绝对路径或 `..`。

## 14. 常见问题

### 14.1 `/sandbox/memory` 在不同 Pod 之间是否共享？

默认不共享。每个 Pod 的 `/sandbox/memory` 来自自己的 volume 或 emptyDir。只要每个 Agent 实例使用唯一 `agent_instance_id` 和 `bundle_id`，并发执行不会互相污染。

### 14.2 Agent 能不能直接搜索全部长期记忆？

普通沙箱 Agent 不应该直接搜索全量长期记忆。MC 会在任务启动前把允许读取的摘要物化到 `local/agent_memory_excerpt.json`。控制面调试或编排层可调用 `POST /memory/search`。

### 14.3 `write_result()` 和 `write_to_local()` 有什么区别？

`write_result()` 写任务结果，进入 workflow summary。

`write_to_local()` 写候选长期经验，commit 后进入 `agent_memory/{agent_id}`。

### 14.4 写了 output 后为什么长期记忆没变化？

写 `output/` 只是 Agent 本地输出。还需要 sidecar 或编排层调用：

```http
POST /memory/bundles/{bundle_id}/upload_outbox
POST /memory/bundles/{bundle_id}/commit
```

如果是共享文件挂载模式，也至少要调用 `commit`。

### 14.5 `notify_task_completed()` 会自动保存记忆吗？

不会。它只是状态通知。保存记忆依赖 `output/` 文件和 MC commit。

### 14.6 当前 SDK 支持同步方法吗？

当前 `SandboxMemory` 的读写接口是 async 方法。普通脚本用 `asyncio.run()` 包一层即可。
