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

工作流 `WF_<pod-uid>` 由编排器创建和删除。Memory 帧 Stream 支持两种模式：
独立 Agent 默认由自身 `NatsComm` 创建和删除；控制器管理的 Agent 只等待
`FRAME_<pod-uid>` 就绪。

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

`NATS_JETSTREAM_DOMAIN` 与 `CLUSTER_ID` 必须等于当前边缘集群 ID。

## 4. 接收工作流任务

进程内只创建一个 `NatsComm`，并在所有请求之间复用：

```python
import asyncio
import os

from runtime_api import NatsComm


async def main():
    comm = NatsComm()

    async def handle(payload):
        # 执行业务处理；成功返回后自动 ACK，异常时自动 NACK。
        print(payload["workflow_id"])

    try:
        await comm.serve_workflow(
            agent_id=os.environ["AGENT_ID"],
            instance_id=os.environ["AGENT_INSTANCE_ID"],
            local_cluster=os.environ["CLUSTER_ID"],
            durable=os.environ["AGENT_INSTANCE_ID"],
            handler=handle,
            max_inflight=1,
        )
    finally:
        await comm.close()


asyncio.run(main())
```

`serve_workflow()` 会同时消费当前实例的 local 和 global subject。它会等待
编排器创建 `WF_<pod-uid>`，但不会在 Agent 侧创建 Stream。

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
            instance_id=os.environ["AGENT_INSTANCE_ID"],
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

如果 Stream 已由边缘控制器管理，则改为：

```python
await comm.serve_memory_frames(
    agent_id=os.environ["AGENT_ID"],
    instance_id=os.environ["AGENT_INSTANCE_ID"],
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
5. 独立 Agent 使用默认的 Memory Stream 自动生命周期。
6. 控制器管理的 Agent 必须设置 `manage_stream_lifecycle=False`，避免两边
   同时拥有 Stream 删除责任。
7. `SIGKILL`、机器掉电无法执行 Python 清理逻辑，由控制器 orphan reconcile
   兜底回收；Kubernetes 正常 `SIGTERM` 退出必须让主任务进入 `finally`。
8. 已建立连接的 `NatsComm` 未调用 `close()` 就被回收时会记录错误日志：
   `NatsComm 在未调用 close() 的情况下被回收`。

## 9. 失败语义

- 自动管理模式创建 Stream 失败：Agent 立即启动失败，通常是 NATS 权限、
  domain 或容量配置错误。
- 控制器管理模式 Stream 未创建：Agent 最多等待
  `NATS_STREAM_PROVISION_TIMEOUT_SEC`，超时后启动失败。
- Stream 满：`discard=new` 使发布方明确收到错误，不静默删除旧任务。
- handler 异常：任务 NACK，后续重新投递。
- 长任务：`serve_workflow()` 和 `serve_memory_frames()` 定期发送 ACK
  progress，避免处理中重复投递。
- Memory 帧超过 `NATS_FRAME_STREAM_MAX_AGE_SEC` 尚未完成时自动过期。
- NATS Pod 重启会丢失尚未消费的 Memory 帧，调用方必须允许超时重试。
- 独立实例结束：停止新请求并调用 `comm.close()`，自动删除
  `FRAME_<instance-id>`。
- 编排实例结束：编排器先停止向该 UID 路由，再由控制器删除
  `WF_<pod-uid>` 和 `FRAME_<pod-uid>`。
