# Agent NATS 使用规范

本文是 Agent 接入当前 NATS 通信层的实现规范。

## 1. 消息分类

| 类型 | Subject | NATS 模式 | 用途 |
| --- | --- | --- | --- |
| 工作流任务 | `workflow.local.*` / `workflow.global.*` | JetStream | 需要 ACK、重投和离线保留的 JSON 小消息 |
| 可靠帧数据 | `frame.local.*` / `frame.global.*` | JetStream Memory | 大帧 ACK、重投、处理回复 |
| 实时帧数据 | `frame.local.*` / `frame.global.*` | Core NATS bytes | 允许丢帧的低延迟兼容模式 |
| 工作流回复 | `_INBOX.*` | Core NATS JSON | 请求响应，不持久化 |

不要把 10MiB 至 15MiB 的帧放入 JSON 工作流消息。帧使用 Memory 二进制
接口、Core 二进制兼容接口或 gRPC，工作流消息只携带元数据或 `frame_ref`。

## 2. 实例级工作流 Stream

每个 Kubernetes Agent Pod 默认独享两个 Stream：

```text
Stream: WF_<pod-uid>
workflow.local.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
workflow.global.<cluster>.agent.<agent-id>.instance.<pod-uid>.>

Stream: FRAME_<pod-uid>
frame.local.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
frame.global.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
```

Pod UID 是实例身份。Deployment 名、Pod 名和 Agent 类型不能代替 Pod UID，
因为滚动更新后它们可能重复或变化。

`WF_<pod-uid>` 和 `FRAME_<pod-uid>` 都由该 Agent 进程中的同一个
`NatsComm` 创建和删除。编排器只负责 Pod 生命周期以及
`cluster_id + agent_id + pod_uid` 路由表。

## 3. Agent 环境变量

```yaml
- name: NATS_SERVERS
  value: "nats://nats:4222"
- name: NATS_JETSTREAM_DOMAIN
  value: "edge-a"
- name: NATS_WORKFLOW_STREAM_PREFIX
  value: "WF"
- name: NATS_FRAME_STREAM_PREFIX
  value: "FRAME"
- name: CLUSTER_ID
  value: "edge-a"
- name: AGENT_ID
  value: "detector"
- name: AGENT_INSTANCE_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.uid
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: NATS_STREAM_PROVISION_TIMEOUT_SEC
  value: "120"
```

通用限制：

```yaml
- name: NATS_STREAM_MAX_BYTES
  value: "512MiB"
- name: NATS_STREAM_DISCARD
  value: "new"
- name: NATS_STREAM_RETENTION
  value: "workqueue"
- name: NATS_STREAM_STORAGE
  value: "file"
- name: NATS_CONTROL_MAX_BYTES
  value: "1048576"
- name: NATS_BINARY_MAX_BYTES
  value: "67108864"
- name: NATS_PENDING_SIZE_BYTES
  value: "134217728"
- name: NATS_BINARY_PENDING_BYTES
  value: "134217728"
- name: NATS_FRAME_STREAM_MAX_BYTES
  value: "512MiB"
- name: NATS_FRAME_STREAM_MAX_AGE_SEC
  value: "120"
- name: NATS_FRAME_ACK_WAIT_SEC
  value: "60"
- name: NATS_FRAME_MAX_DELIVER
  value: "3"
```

Agent 未设置 `NATS_JETSTREAM_DOMAIN` 时会自动使用 `CLUSTER_ID`；如果显式设置，
它必须与 `CLUSTER_ID` 相等，否则启动失败。非 Agent 管理客户端才默认使用
`hub`。
Agent 不要设置 `NATS_STREAM` 或 `NATS_STREAM_SUBJECTS`。当
`AGENT_INSTANCE_ID` 存在时，`NatsComm()` 默认生成 `WF_<pod-uid>` 以及该实例
的 local/global Subjects；显式设置这两个兼容变量才会启用旧的自定义 Stream。
`CLUSTER_ID`、`AGENT_ID`、`AGENT_INSTANCE_ID` 必须同时存在，缺失时启动直接
报错，不会退回共享 `WORKFLOW`。

发送跨集群工作流时，subject 中的目标集群决定 global 路由，目标实例决定
`WF_<pod-uid>`。`send()` 和 `send_bytes()` 会强制校验目标 Stream；
`jetstream(domain=...)` 本身不负责数据 subject 的网络路由。云端 Hub 不得存在
匹配 `workflow.local.>` 或 `workflow.global.>` 的共享 Stream，只能保留
`legacy.workflow.>` 等与新 subject 不重叠的兼容 Stream。

需要在客户端创建调用返回时就确认 Stream 已存在，使用：

```python
comm = await NatsComm.create()
```

同步 `__init__()` 只生成 Stream 名称和 Subjects；`create()` 会继续连接 NATS
并等待服务端完成创建。不要在 `__init__()` 中启动未等待的后台网络任务。

## 4. 接收工作流任务

进程内只创建一个 `NatsComm`，并在所有请求之间复用：

```python
import asyncio
import os

from runtime_api import NatsComm


async def main():
    async def handle(payload):
        # 执行业务处理；成功返回后自动 ACK，异常时自动 NACK。
        print(payload["workflow_id"])

    async with NatsComm() as comm:
        await comm.serve_workflow(
            agent_id=os.environ["AGENT_ID"],
            local_cluster=os.environ["CLUSTER_ID"],
            durable=os.environ["AGENT_INSTANCE_ID"],
            handler=handle,
            max_inflight=1,
        )


asyncio.run(main())
```

`serve_workflow()` 从 `AGENT_INSTANCE_ID` 读取 Pod UID，启动时幂等创建
`WF_<pod-uid>`，并同时消费当前实例的 local/global subject。退出
`async with` 时，`close()` 删除该 Stream。

## 5. 调用目标 Agent

编排器必须向调用节点提供三个字段：

```text
target_cluster
target_agent_id
target_instance_id
```

发送方不用手写 subject：

```python
reply = await comm.send_workflow_and_wait(
    target_cluster=target_cluster,
    agent_id=target_agent_id,
    target_instance_id=target_instance_id,
    payload={
        "workflow_id": workflow_id,
        "input": input_data,
    },
    local_cluster=os.environ["CLUSTER_ID"],
    timeout_sec=120,
)
```

运行时自动选择：

```text
同集群:
workflow.local.<target-cluster>.agent.<agent-id>.instance.<pod-uid>.in

跨集群:
workflow.global.<target-cluster>.agent.<agent-id>.instance.<pod-uid>.in
```

只发送、不等待回复时使用 `send_workflow()`。

## 6. 发送 JetStream Memory 帧

发送端必须使用编排器提供的目标 Pod UID。方法返回时代表目标 Agent 已完成
处理并返回结果，不只是帧写入 Stream：

```python
reply_bytes = await comm.request_memory_frame(
    target_cluster="edge-b",
    agent_id="detector",
    target_instance_id=target_instance_id,
    payload=frame_bytes,
    operation="infer",
    local_cluster=os.environ["CLUSTER_ID"],
    timeout_sec=30,
)
```

接收端：

```python
async def infer(message):
    result = run_model(message.data)
    return result

async def main():
    async with NatsComm() as comm:
        await comm.serve_memory_frames(
            agent_id=os.environ["AGENT_ID"],
            handler=infer,
            local_cluster=os.environ["CLUSTER_ID"],
            max_inflight=1,
        )
```

默认情况下，`serve_memory_frames()` 会幂等创建 `FRAME_<instance-id>`，
将它登记为当前 `NatsComm` 的自主管理资源，并同时建立 local/global Pull
Consumer。应用必须关闭每个创建过的 `NatsComm`。推荐使用
`async with NatsComm()`，正常返回、异常或任务取消离开作用域时都会调用
`close()`；`close()` 会先删除该 Stream，再关闭 NATS 连接。

旧环境仍由边缘控制器管理 Stream 时，兼容调用为：

```python
await comm.serve_memory_frames(
    agent_id=os.environ["AGENT_ID"],
    handler=infer,
    local_cluster=os.environ["CLUSTER_ID"],
    max_inflight=1,
    manage_stream_lifecycle=False,
)
```

该模式只等待控制器创建 Stream，`comm.close()` 不会删除它。处理成功后先通过
Core Inbox 回复，再 ACK 并从 Memory Stream 删除输入帧；异常时 NAK，最多重投
`NATS_FRAME_MAX_DELIVER` 次。业务 handler 必须按 `message.request_id`
保证幂等。

`request_memory_frame()` / `serve_memory_frames()` 传输的 `payload` 和
`message.data` 已经是原始二进制，不经过 JSON 或 Base64。通用 JetStream
Subject 需要发送二进制时使用：

```python
await comm.send_bytes("state.detector", frame_bytes)

messages = await comm.receive_bytes(
    "state.detector",
    durable="detector-state-reader",
    batch=1,
)
for message in messages:
    process(message.data)
    await message.ack()
```

这里的 `send_bytes()` / `receive_bytes()` 使用 JetStream 和 PubAck；
`publish_bytes()` / `request_bytes()` / `subscribe_bytes()` 使用 Core NATS，
两组接口不能混淆。

读取调用时已保存的最后一条消息：

```python
message = await comm.receive_latest_bytes("state.detector", ack=True)
if message is not None:
    process(message.data)
```

JSON 消息对应 `receive_latest()`。这两个方法每次创建临时
`DeliverPolicy.LAST` Consumer，避免复用 durable 的历史消费位置。根据
JetStream 语义，`DeliverLast` 只决定 Consumer 创建时的起点；Consumer 建立
后仍会按顺序收到每条新消息，不会在消费者变慢时自动跳过中间消息。

`WF_*` 和 `FRAME_*` 使用 `WorkQueuePolicy`，要求可靠处理每条任务，不应使用
latest 方法跳过消息。真正的“状态快照只保留最新值”应使用独立的
`LimitsPolicy + DiscardOld + max_msgs_per_subject=1` Stream，不要修改实例任务
Stream 的保留策略。

允许丢帧且不需要重投时，可继续使用旧接口：

```python
reply = await comm.request_frame_bytes(...)
await comm.subscribe_frame_bytes(...)
```

## 7. gRPC 帧引用

需要断点、分块、校验或暂存时使用 `FrameComm`。一个 `FrameComm` 实例同时
复用 NATS 连接和 gRPC channel：

```python
from runtime_api import FrameComm

comm = FrameComm()
reply = await comm.send_workflow_and_wait(
    target_cluster=target_cluster,
    agent_id=target_agent_id,
    target_instance_id=target_instance_id,
    payload={"workflow_id": workflow_id, "frame_ref": frame_ref},
    local_cluster=local_cluster,
)
```

`FrameComm.serve_workflow()` 支持在调用业务 handler 前下载 `frame_ref`：

```python
await comm.serve_workflow(
    agent_id=agent_id,
    instance_id=instance_id,
    local_cluster=cluster_id,
    durable=instance_id,
    handler=handle,
    download_frames=True,
    delete_remote_frame=True,
)
```

## 8. 连接复用要求

1. 每个 Agent 进程只创建一个 `NatsComm` 或 `FrameComm`。
2. 不要在每帧、每次推理或每个 HTTP 请求中重新创建对象。
3. 常驻消费者只启动一次。
4. 每个 `NatsComm` 都必须使用 `async with`，或在 `finally` 中调用
   `await comm.close()`。
5. `serve_workflow()` 和 `serve_memory_frames()` 都使用默认的 Agent 自主
   Stream 生命周期。
6. 编排器删除 Pod 前必须先停止向该 UID 路由，并使用正常 `SIGTERM` 和足够的
   `terminationGracePeriodSeconds` 让 Agent 执行 `close()`。
7. `SIGKILL`、节点掉电无法执行 Python 清理逻辑。编排器必须记录 Pod UID，
   在确认 Pod 消失后使用 `delete_workflow_stream()` 和
   `delete_memory_frame_stream()` 兜底删除残留。
8. 已建立连接的 `NatsComm` 未调用 `close()` 就被回收时会记录错误日志：
   `NatsComm 在未调用 close() 的情况下被回收`。

## 9. 失败语义

- 自动管理模式创建 Stream 失败：Agent 立即启动失败，通常是 NATS 权限、
  domain 或容量配置错误。
- Stream 满：`discard=new` 使发布方明确收到错误，不静默删除旧任务。
- handler 异常：任务 NACK，后续重新投递。
- 长任务：`serve_workflow()` 和 `serve_memory_frames()` 定期发送 ACK
  progress，避免处理中重复投递。
- Memory 帧超过 `NATS_FRAME_STREAM_MAX_AGE_SEC` 尚未完成时自动过期。
- NATS Pod 重启会丢失尚未消费的 Memory 帧，调用方必须允许超时重试。
- 实例结束：编排器先停止向该 UID 路由，再终止 Pod；Agent 的
  `comm.close()` 自动删除 `WF_<pod-uid>` 和 `FRAME_<pod-uid>`。
