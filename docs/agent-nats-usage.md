# Agent NATS 使用规范

本文是 Agent 接入当前 NATS 通信层的实现规范。

## 1. 消息分类

| 类型 | Subject | NATS 模式 | 用途 |
| --- | --- | --- | --- |
| 工作流任务 | `workflow.local.*` / `workflow.global.*` | JetStream | 需要 ACK、重投和离线保留的 JSON 小消息 |
| 帧数据 | `frame.local.*` / `frame.global.*` | Core NATS bytes | 实时大帧，不持久化 |
| 工作流回复 | `_INBOX.*` | Core NATS JSON | 请求响应，不持久化 |

不要把 10MiB 至 15MiB 的帧放入 JSON 工作流消息。帧使用二进制接口或
gRPC 帧传输，工作流消息只携带元数据或 `frame_ref`。

需要 JetStream 可靠暂存时也可以使用 Object Store，但当前 nats-py 对
10MiB 对象的顺序 put/get 实测只有约 1.4 FPS，不适合作为 10 FPS 实时路径。
测试方法和选择建议见
[`nats-object-store-frame-test.md`](nats-object-store-frame-test.md)。

## 2. 实例级工作流 Stream

每个 Kubernetes Agent Pod 独享一个 Stream：

```text
Stream: WF_<pod-uid>

workflow.local.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
workflow.global.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
```

Pod UID 是实例身份。Deployment 名、Pod 名和 Agent 类型不能代替 Pod UID，
因为滚动更新后它们可能重复或变化。

Agent 不创建或删除 Stream。编排器在 Pod 创建后根据 Pod UID 分配 Stream，
Agent 启动后只等待该 Stream 就绪。

## 3. Agent 环境变量

```yaml
- name: NATS_SERVERS
  value: "nats://nats:4222"
- name: NATS_JETSTREAM_DOMAIN
  value: "edge-a"
- name: NATS_WORKFLOW_STREAM_PREFIX
  value: "WF"
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

## 6. 发送实时帧

帧不使用 JetStream：

```python
reply_bytes = await comm.request_frame_bytes(
    target_cluster="edge-b",
    agent_id="detector",
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
    await message.respond(result)

await comm.serve_frame_bytes(
    agent_id=os.environ["AGENT_ID"],
    handler=infer,
    local_cluster=os.environ["CLUSTER_ID"],
)
```

同集群自动使用 `frame.local.*`，跨集群自动使用 `frame.global.*`。

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
4. 进程退出时调用 `close()`。
5. 不要自行调用 `provision_workflow_stream()` 或
   `delete_workflow_stream()`；这两个接口属于编排器。

## 9. 失败语义

- Stream 未创建：Agent 最多等待
  `NATS_STREAM_PROVISION_TIMEOUT_SEC`，超时后启动失败。
- Stream 满：`discard=new` 使发布方明确收到错误，不静默删除旧任务。
- handler 异常：任务 NACK，后续重新投递。
- 长任务：`serve_workflow()` 定期发送 ACK progress，避免处理中重复投递。
- 实例结束：编排器先停止向该 UID 路由，再删除 `WF_<pod-uid>`。
