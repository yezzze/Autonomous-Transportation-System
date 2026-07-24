# Agent 使用 NATS 接入指南

本文档面向需要接入当前通信链路的 Agent 开发者，说明 Agent 如何配置 NATS、
管理连接、发送任务、消费任务和返回结果。

当前 Agent 通信统一使用 `runtime_api.NatsComm`。业务代码只连接本 Kubernetes
集群内的 NATS Service，不直接连接其他 Agent、其他 Kubernetes 集群或云端 NATS。

Agent 镜像需要包含本仓库的 `runtime_api`，并安装：

```text
nats-py==2.11.0
```

## 1. Agent 通信链路

Agent 始终连接本集群 Service：

```text
Agent -> nats://nats:4222
```

当前 Helm 环境的 JetStream domain 为云端 `hub`。Agent 通过本地 NATS 连接访问
`hub`中的 `WORKFLOW` Stream，不直接连接云端地址：

```text
Agent
  -> 本地 NATS
  -> JetStream domain: hub
```

目标 Agent 位于其他集群时，NATS 通过 LeafNode 完成跨集群路由：

```text
发送 Agent
  -> 发送方本地 NATS
  -> LeafNode
  -> 云端 NATS Hub
  -> LeafNode
  -> 接收方本地 NATS
  -> 接收 Agent
```

Agent 代码不需要判断目标是否在同一个集群，不需要选择云端地址，也不需要根据目标
位置切换 `NATS_SERVERS`。发送方只需使用目标 Agent 的完整 subject。

## 2. 必须遵守的使用规则

每个 Agent 进程必须：

1. 创建一个长期使用的 `NatsComm` 实例。
2. 在进程启动时连接 NATS。
3. 所有任务和帧循环复用同一个实例。
4. 一个常驻消费者只调用一次 `serve()`。
5. 在进程退出时调用 `close()`。

不要在单帧处理函数、模型推理函数或网络请求 handler 中重复执行 `NatsComm()`，
也不要为每条消息执行一次 `asyncio.run()`。

一个 Pod 如果运行多个 worker 进程，每个进程分别创建一个 `NatsComm`。连接对象
不能跨进程或跨 asyncio 事件循环共享。

## 3. Agent 环境变量

推荐配置：

```yaml
env:
  - name: NATS_SERVERS
    value: "nats://nats:4222"
  - name: NATS_JETSTREAM_DOMAIN
    value: "hub"
  - name: NATS_STREAM
    value: "WORKFLOW"
  - name: NATS_STREAM_SUBJECTS
    value: "workflow.>"
  - name: NATS_CONTROL_MAX_BYTES
    value: "1048576"
  - name: NATS_MAX_INFLIGHT
    value: "1"
  - name: NATS_ACK_PROGRESS_INTERVAL_SEC
    value: "10"
  - name: WORKFLOW_TIMEOUT_SEC
    value: "120"
  - name: CLUSTER_ID
    value: "edge-a"
  - name: IN_SUBJECT
    value: "workflow.edge-a.agent.detector.in"
  - name: DURABLE
    value: "edge-a-agent-detector"
```

变量含义：

| 变量 | 用途 |
|---|---|
| `NATS_SERVERS` | 当前 Agent 连接的本地 NATS 地址，多个地址使用逗号分隔 |
| `NATS_JETSTREAM_DOMAIN` | 当前统一使用的 JetStream domain，Helm 环境为 `hub` |
| `NATS_STREAM` | 持久任务所在 Stream，默认 `WORKFLOW` |
| `NATS_STREAM_SUBJECTS` | Stream 接收的 subject，默认 `workflow.>` |
| `NATS_CONTROL_MAX_BYTES` | `NatsComm` JSON 消息的最大字节数，默认 1 MiB |
| `NATS_MAX_INFLIGHT` | 单个 Agent 进程允许并发处理的任务数 |
| `NATS_ACK_PROGRESS_INTERVAL_SEC` | 长任务处理期间发送 ACK 进度的间隔 |
| `WORKFLOW_TIMEOUT_SEC` | 调用下游 Agent 时等待回复的时间 |
| `CLUSTER_ID` | Agent 所属集群标识 |
| `IN_SUBJECT` | Agent 接收任务的 subject |
| `DURABLE` | JetStream 持久消费者名称 |

`NATS_SERVERS` 必须写本地 Service。使用当前 Helm 部署时，各集群内统一为：

```text
nats://nats:4222
```

## 4. Subject 命名

Agent 任务 subject 使用：

```text
workflow.<cluster-id>.<agent-id>.in
```

示例：

```text
workflow.edge-a.agent.detector.in
workflow.edge-a.agent.tracker.in
workflow.edge-b.agent.planner.in
```

要求：

- `cluster-id` 使用目标 Agent 所属集群 ID。
- `agent-id` 在目标集群内保持唯一。
- subject 通过环境变量注入，不在公共 Agent 代码中写死。
- 接收端 `IN_SUBJECT` 与发送端使用的目标 subject 必须完全一致。

回复 subject 不需要业务代码生成。发送方调用 `send_and_wait()` 时会自动创建
`_INBOX.*`，并把它放入请求的 `reply_subject` 字段。

## 5. 消息结构

当前 `NatsComm` 接收 Python `dict`，并将其编码为 JSON。

推荐的任务结构：

```python
{
    "workflow_id": "workflow-001",
    "request_id": "request-001",
    "timestamp_ms": 1784860800000,
    "payload": {
        "camera_id": "camera-01",
        "operation": "detect",
    },
}
```

接收方返回：

```python
{
    "workflow_id": "workflow-001",
    "request_id": "request-001",
    "result": {
        "objects": [],
    },
}
```

`send_and_wait()`会自动增加：

```python
{
    "reply_subject": "_INBOX.<generated-token>"
}
```

处理方必须读取请求中的 `reply_subject`，并原样交给 `publish_core()`。不要自行拼接
回复 subject。

当前 `NatsComm` 用于 JSON 消息，消息大小受 `NATS_CONTROL_MAX_BYTES` 限制。二进制
内容不能放入 JSON 或转换成 Base64 后发送；任务消息中应只传业务字段和外部数据
引用。

## 6. 接收任务并回复

下面是一个完整的异步 Agent worker：

```python
import asyncio
import os

from runtime_api import NatsComm


IN_SUBJECT = os.environ["IN_SUBJECT"]
DURABLE = os.environ["DURABLE"]
MAX_INFLIGHT = int(os.environ.get("NATS_MAX_INFLIGHT", "1"))
ACK_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("NATS_ACK_PROGRESS_INTERVAL_SEC", "10")
)


async def run_model(payload):
    return {"status": "ok", "value": payload.get("value")}


async def main():
    comm = NatsComm()

    async def handler(message):
        workflow_id = message["workflow_id"]
        reply_subject = message["reply_subject"]

        result = await run_model(message.get("payload", {}))

        await comm.publish_core(
            reply_subject,
            {
                "workflow_id": workflow_id,
                "request_id": message.get("request_id"),
                "result": result,
            },
        )

    try:
        await comm.connect()
        await comm.serve(
            subject=IN_SUBJECT,
            durable=DURABLE,
            handler=handler,
            max_inflight=MAX_INFLIGHT,
            ack_progress_interval_sec=ACK_PROGRESS_INTERVAL_SEC,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
```

`serve()`会持续从 JetStream 拉取任务：

- `handler`正常返回后自动 ACK。
- `handler`抛出异常时自动 NACK。
- 长任务处理期间按配置调用 `in_progress()`。
- 同一个 `NatsComm` 实例会复用 durable pull subscription。

`serve()`是常驻方法。每个进程、每个 `(subject, durable)` 组合只启动一次。

## 7. 发送任务并等待回复

发送方使用 `send_and_wait()`：

```python
import asyncio
import os
import uuid

from runtime_api import NatsComm


TARGET_SUBJECT = os.environ["TARGET_SUBJECT"]
WORKFLOW_TIMEOUT_SEC = float(
    os.environ.get("WORKFLOW_TIMEOUT_SEC", "120")
)


async def main():
    comm = NatsComm()
    try:
        await comm.connect()

        for index in range(100):
            workflow_id = str(uuid.uuid4())
            reply = await comm.send_and_wait(
                subject=TARGET_SUBJECT,
                payload={
                    "workflow_id": workflow_id,
                    "request_id": f"request-{index}",
                    "payload": {
                        "value": index,
                    },
                },
                timeout_sec=WORKFLOW_TIMEOUT_SEC,
            )
            print(reply)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
```

同一个 `comm` 必须在整个循环中复用。`send_and_wait()`会：

1. 先创建 Core NATS `_INBOX` 回复订阅。
2. 将包含 `reply_subject` 的任务发布到 JetStream。
3. 等待接收端通过 `publish_core()`返回一条结果。
4. 收到结果或超时后注销本次 `_INBOX` 订阅。

## 8. Agent 调用下游 Agent

一个 Agent 同时消费上游任务并调用下游 Agent 时，继续复用同一个 `NatsComm`：

```python
async def handler(message):
    upstream_reply_subject = message["reply_subject"]

    downstream_reply = await comm.send_and_wait(
        subject=DOWNSTREAM_SUBJECT,
        payload={
            "workflow_id": message["workflow_id"],
            "request_id": message.get("request_id"),
            "payload": message.get("payload", {}),
        },
        timeout_sec=WORKFLOW_TIMEOUT_SEC,
    )

    await comm.publish_core(
        upstream_reply_subject,
        {
            "workflow_id": message["workflow_id"],
            "request_id": message.get("request_id"),
            "result": downstream_reply["result"],
        },
    )
```

这里有两个不同的回复地址：

- `upstream_reply_subject`：上游发送方等待的地址。
- 下游回复地址：由本次 `send_and_wait()`自动创建和管理。

不要把上游的 `reply_subject`直接传给下游。

## 9. 单向任务

只需要将任务写入 JetStream、不等待回复时，使用 `send()`：

```python
await comm.send(
    "workflow.edge-a.agent.recorder.in",
    {
        "workflow_id": "workflow-001",
        "payload": {"event": "start"},
    },
)
```

`send()`返回 Stream 和序列号：

```python
{
    "subject": "workflow.edge-a.agent.recorder.in",
    "stream": "WORKFLOW",
    "seq": 123,
}
```

接收端仍使用 `serve()`或`receive()`消费该消息。

## 10. 手动拉取消息

常驻 Agent 优先使用 `serve()`。需要自行控制批次和 ACK 时可以使用 `receive()`：

```python
messages = await comm.receive(
    subject=IN_SUBJECT,
    durable=DURABLE,
    batch=10,
    timeout_sec=5,
    ack=False,
)

for message in messages:
    try:
        await process(message.payload)
        await message.ack()
    except TimeoutError:
        await message.nak(delay=5)
    except ValueError:
        await message.term()
```

可用的确认操作：

| 方法 | 含义 |
|---|---|
| `ack()` | 处理成功 |
| `nak()` | 处理失败，允许重新投递 |
| `nak(delay=5)` | 延迟指定秒数后重新投递 |
| `in_progress()` | 任务仍在处理，延长 ACK 等待时间 |
| `term()` | 终止该消息，不再投递 |

持久消费必须提供稳定且唯一的 `durable`。同一个队列中的多个副本需要共享任务时，
各副本使用同一个 durable；不同业务消费者需要各自消费一份时，使用不同 durable。

## 11. Core NATS 请求响应

只需要在线实时调用、不需要 JetStream 持久化时，可以使用
`request()`和`respond()`：

服务端：

```python
async def handler(payload):
    return {"result": payload["x"] + payload["y"]}


await comm.respond(
    "rpc.edge-a.agent.calculator",
    handler=handler,
    queue="calculator-workers",
)
```

客户端：

```python
reply = await comm.request(
    "rpc.edge-a.agent.calculator",
    {"x": 1, "y": 2},
    timeout_sec=10,
)
```

同一个 `queue`中的多个 Agent 实例会负载均衡，每条请求只交给其中一个实例。

Agent 工作流任务默认使用 `send_and_wait()`和`serve()`。只有业务明确允许接收端不在线
时直接失败，才使用 `request()`和`respond()`。

## 12. 方法选择

| 业务场景 | 发送端 | 接收端 | 消息是否持久化 |
|---|---|---|---|
| 发送任务并等待结果 | `send_and_wait()` | `serve()` + `publish_core()` | 任务持久化，回复不持久化 |
| 只发送任务 | `send()` | `serve()`或`receive()` | 持久化 |
| 手动批量拉取 | `send()` | `receive()` | 持久化 |
| 在线实时 RPC | `request()` | `respond()` | 不持久化 |
| 向指定回复地址返回结果 | - | `publish_core()` | 不持久化 |

## 13. 并发配置

模型必须逐帧处理时：

```text
NATS_MAX_INFLIGHT=1
```

模型可以并行处理时，根据单个 Agent 进程可承受的并发量设置：

```text
NATS_MAX_INFLIGHT=4
```

`max_inflight`限制单个 `serve()`当前同时运行的 handler 数量。一个 Pod 有多个进程
时，总并发量约为：

```text
进程数 * NATS_MAX_INFLIGHT
```

长时间运行的模型任务应保留：

```text
NATS_ACK_PROGRESS_INTERVAL_SEC=10
```

## 14. 同步框架接入

`NatsComm`是异步客户端，必须始终在创建它的 asyncio 事件循环中使用。

FastAPI、aiohttp 等异步框架可以在应用生命周期中直接创建：

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from runtime_api import NatsComm


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.nats = NatsComm()
    await app.state.nats.connect()
    try:
        yield
    finally:
        await app.state.nats.close()


app = FastAPI(lifespan=lifespan)
```

同步网络服务框架需要创建一个专用后台 asyncio 事件循环，在该循环中创建唯一的
`NatsComm`，再使用 `asyncio.run_coroutine_threadsafe()`提交通信任务。

不要在同步 handler 内调用：

```python
asyncio.run(NatsComm().send_and_wait(...))
```

## 15. 多副本和多进程

部署多个 Agent Pod 副本时：

- 所有副本订阅相同 `IN_SUBJECT`。
- 需要竞争消费同一批任务时，所有副本使用相同 `DURABLE`。
- 每个进程创建并维护自己的 `NatsComm`。
- 每个进程退出时关闭自己的连接。

示例：

```text
Pod 1 / worker 1 -> NatsComm connection 1
Pod 1 / worker 2 -> NatsComm connection 2
Pod 2 / worker 1 -> NatsComm connection 3
Pod 2 / worker 2 -> NatsComm connection 4
```

## 16. Agent 接入检查清单

- Agent 只连接本集群 `NATS_SERVERS`。
- subject 通过环境变量注入。
- 每个进程只创建一个长期使用的 `NatsComm`。
- 所有消息处理复用同一个实例和 asyncio 事件循环。
- 常驻任务消费使用 `serve()`和稳定的 durable。
- 需要回复的任务使用 `send_and_wait()`发送。
- 接收端使用请求中的 `reply_subject`调用`publish_core()`。
- 模型串行推理时设置`NATS_MAX_INFLIGHT=1`。
- 长任务配置`NATS_ACK_PROGRESS_INTERVAL_SEC`。
- 进程退出时调用`close()`。
- Agent JSON 消息不超过`NATS_CONTROL_MAX_BYTES`。
