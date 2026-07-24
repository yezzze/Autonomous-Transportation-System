# Agent 使用 NATS 接入指南

本文档面向需要接入当前通信链路的 Agent 开发者，说明 Agent 如何配置 NATS、
管理连接、发送任务、消费任务和返回结果。

当前 Agent 通信统一使用 `runtime_api.NatsComm`。业务代码只连接本 Kubernetes
集群内的 NATS Service，不直接连接其他 Agent、其他 Kubernetes 集群或云端 NATS。

Agent 镜像需要包含本仓库的 `runtime_api`，并安装：

```text
nats-py==2.11.0
```

## 1. Agent 使用的两类消息

Agent 始终只连接本 Kubernetes 集群的 NATS Service：

```text
NATS_SERVERS=nats://nats:4222
```

同一个 `NatsComm`连接同时承载两类消息：

| 消息 | API | 传输方式 | 用途 |
|---|---|---|---|
| 工作流控制消息 | `send()`、`send_and_wait()`、`serve()` | JetStream + JSON | 需要持久化的任务 |
| 帧等大二进制数据 | `request_frame_bytes()`、`subscribe_frame_bytes()` | Core NATS + bytes | 在线请求响应 |

工作流控制消息使用云端 JetStream domain `hub`。二进制帧不进入 JetStream，而是由
Core NATS 根据 `local/global` subject 路由。

## 2. local/global 路由

发送方必须提供：

- `CLUSTER_ID`：发送 Agent 所属集群。
- `target_cluster`：接收 Agent 所属集群。
- `agent_id`：接收 Agent ID。

`request_frame_bytes()`内部执行以下判断：

```python
if target_cluster == CLUSTER_ID:
    subject = f"frame.local.{target_cluster}.{agent_id}.infer"
else:
    subject = f"frame.global.{target_cluster}.{agent_id}.infer"
```

同集群链路：

```text
发送 Agent
  -> 本集群 NATS
  -> frame.local.<cluster>.<agent>.infer
  -> 本集群接收 Agent
```

跨集群链路：

```text
发送 Agent
  -> 发送方本地 NATS
  -> frame.global.<target-cluster>.<agent>.infer
  -> LeafNode
  -> 云端 NATS Hub
  -> 目标集群 LeafNode/NATS
  -> 接收 Agent
```

边缘 NATS 的 LeafNode 配置同时拒绝：

```yaml
deny_imports:
  - "frame.local.>"
deny_exports:
  - "frame.local.>"
```

因此 `frame.local.>`不会传播到云端；`frame.global.>`可以通过 LeafNode 跨集群路由。
Agent 不切换 NATS 地址，只通过 `target_cluster`选择路由。

## 3. 连接生命周期

每个 Agent 进程必须：

1. 创建一个长期使用的 `NatsComm`实例。
2. 在进程启动时连接 NATS。
3. 所有控制任务和帧循环复用同一个实例。
4. 二进制接收端只调用一次`subscribe_frame_bytes()`。
5. JetStream 消费端只调用一次`serve()`。
6. 在进程退出时调用`close()`。

不要在单帧处理函数、模型推理函数或网络请求 handler 中重复执行`NatsComm()`，
也不要为每条消息执行一次`asyncio.run()`。

一个 Pod 如果运行多个 worker 进程，每个进程分别创建一个`NatsComm`。连接对象
不能跨进程或跨 asyncio 事件循环共享。

## 4. Agent 环境变量

推荐配置：

```yaml
env:
  - name: NATS_SERVERS
    value: "nats://nats:4222"
  - name: CLUSTER_ID
    value: "edge-a"
  - name: NATS_BINARY_MAX_BYTES
    value: "67108864"
  - name: NATS_PENDING_SIZE_BYTES
    value: "134217728"
  - name: NATS_BINARY_PENDING_BYTES
    value: "134217728"
  - name: NATS_BINARY_PENDING_MSGS
    value: "32"
  - name: NATS_MAX_INFLIGHT
    value: "1"
  - name: NATS_JETSTREAM_DOMAIN
    value: "hub"
  - name: NATS_STREAM
    value: "WORKFLOW"
  - name: NATS_STREAM_SUBJECTS
    value: "workflow.>"
  - name: NATS_CONTROL_MAX_BYTES
    value: "1048576"
  - name: NATS_ACK_PROGRESS_INTERVAL_SEC
    value: "10"
  - name: WORKFLOW_TIMEOUT_SEC
    value: "120"
  - name: IN_SUBJECT
    value: "workflow.edge-a.agent.detector.in"
  - name: DURABLE
    value: "edge-a-agent-detector"
```

| 变量 | 用途 |
|---|---|
| `NATS_SERVERS` | 当前集群的 NATS Service，Helm 环境统一为`nats://nats:4222` |
| `CLUSTER_ID` | 当前 Agent 所属集群，是 local/global 判断依据 |
| `NATS_BINARY_MAX_BYTES` | 单条 Core NATS 二进制消息上限，默认 64 MiB |
| `NATS_PENDING_SIZE_BYTES` | nats-py 出站 pending buffer，默认 128 MiB |
| `NATS_BINARY_PENDING_BYTES` | 单个二进制订阅的待处理字节上限，默认 128 MiB |
| `NATS_BINARY_PENDING_MSGS` | 单个二进制订阅的待处理消息数，默认 32 |
| `NATS_MAX_INFLIGHT` | 一个 Agent 进程允许同时处理的任务或帧数量 |
| `NATS_JETSTREAM_DOMAIN` | 工作流控制消息使用的 JetStream domain |
| `NATS_STREAM` | 工作流控制消息所在 Stream |
| `NATS_STREAM_SUBJECTS` | Stream 接收的 subject |
| `NATS_CONTROL_MAX_BYTES` | JSON 控制消息上限，默认 1 MiB |
| `NATS_ACK_PROGRESS_INTERVAL_SEC` | JetStream 长任务 ACK 进度间隔 |
| `WORKFLOW_TIMEOUT_SEC` | 调用下游 Agent 的等待时间 |
| `IN_SUBJECT` | Agent 接收 JSON 控制任务的 subject |
| `DURABLE` | JetStream 持久消费者名称 |

NATS Server 的`max_payload`为 64 MiB。Protobuf 等协议封装后的完整 bytes 必须不超过
`NATS_BINARY_MAX_BYTES`和服务端`max_payload`。

## 5. Subject 命名

工作流控制消息：

```text
workflow.<cluster-id>.<agent-id>.in
```

二进制帧：

```text
frame.local.<target-cluster-id>.<agent-id>.<operation>
frame.global.<target-cluster-id>.<agent-id>.<operation>
```

例如：

```text
workflow.edge-a.agent.detector.in
frame.local.edge-a.detector.infer
frame.global.edge-b.detector.infer
```

`cluster-id`、`agent-id`和`operation`必须是单个 NATS subject token，不能包含点号、
空白、`*`或`>`。

业务代码不需要手工拼接 frame subject。发送端使用`request_frame_bytes()`，接收端
使用`subscribe_frame_bytes()`。

## 6. 接收二进制帧

每个接收 Agent 在进程启动时注册一次。该方法使用同一个连接同时订阅本集群对应的
local 和 global subject：

```python
import asyncio
import os

from runtime_api import NatsBinaryMessage, NatsComm


CLUSTER_ID = os.environ["CLUSTER_ID"]
AGENT_ID = os.environ.get("AGENT_ID", "detector")
MAX_INFLIGHT = int(os.environ.get("NATS_MAX_INFLIGHT", "1"))


async def main():
    comm = NatsComm()

    async def infer(message: NatsBinaryMessage) -> bytes:
        request = FrameRequest.FromString(message.data)
        result = await run_model(request.frame)
        return FrameResponse(
            workflow_id=request.workflow_id,
            result=result,
        ).SerializeToString()

    try:
        await comm.connect(ensure_stream=False)
        await comm.subscribe_frame_bytes(
            agent_id=AGENT_ID,
            handler=infer,
            operation="infer",
            local_cluster=CLUSTER_ID,
            queue=f"{CLUSTER_ID}-{AGENT_ID}",
            max_inflight=MAX_INFLIGHT,
        )
        await asyncio.Event().wait()
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
```

handler 接收`NatsBinaryMessage`，其中：

- `message.data`是发送方的原始 bytes。
- `message.subject`是实际命中的 local/global subject。
- `message.reply_subject`是 Core NATS 自动生成的回复地址。
- handler 返回 bytes 时，`subscribe_frame_bytes()`自动回复。
- handler 也可以调用`await message.respond(response_bytes)`后返回`None`。

`max_inflight`在 local/global 两个订阅之间共享。模型必须逐帧处理时设置为 1。

## 7. 发送二进制帧

发送方只传目标集群和目标 Agent，不自行决定 local/global：

```python
import asyncio
import os

from runtime_api import NatsComm


CLUSTER_ID = os.environ["CLUSTER_ID"]
TARGET_CLUSTER = os.environ["TARGET_CLUSTER"]
TARGET_AGENT_ID = os.environ.get("TARGET_AGENT_ID", "detector")


async def main():
    comm = NatsComm()
    try:
        await comm.connect(ensure_stream=False)

        for frame in frame_source():
            request = FrameRequest(
                workflow_id=frame.workflow_id,
                source_cluster=CLUSTER_ID,
                target_cluster=TARGET_CLUSTER,
                frame=frame.data,
            ).SerializeToString()

            response_bytes = await comm.request_frame_bytes(
                target_cluster=TARGET_CLUSTER,
                agent_id=TARGET_AGENT_ID,
                payload=request,
                operation="infer",
                local_cluster=CLUSTER_ID,
                timeout_sec=120,
            )
            response = FrameResponse.FromString(response_bytes)
            await handle_result(response)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
```

路由结果可以提前查看：

```python
subject = comm.frame_subject(
    target_cluster=TARGET_CLUSTER,
    agent_id=TARGET_AGENT_ID,
    local_cluster=CLUSTER_ID,
)
```

结果为：

```text
目标集群等于 CLUSTER_ID    -> frame.local.<target>.<agent>.infer
目标集群不等于 CLUSTER_ID  -> frame.global.<target>.<agent>.infer
```

`request_frame_bytes()`是 Core NATS 在线请求响应，不写入 JetStream。目标 Agent 必须
已经在线并完成订阅；调用超时由发送 Agent 决定是否使用同一个`workflow_id`重试。

## 8. 通用二进制接口

不使用 frame 路由规则时，可以直接使用：

| 方法 | 用途 |
|---|---|
| `request_bytes(subject, payload, timeout_sec)` | 发送 bytes 并等待 bytes 回复 |
| `publish_bytes(subject, payload)` | 发布一条 Core NATS bytes 消息 |
| `respond_bytes(reply_subject, payload)` | 向指定回复地址发布 bytes |
| `subscribe_bytes(subject, handler, queue)` | 注册一个长期复用的 bytes 订阅 |

这些方法不执行 JSON 编码，不受`NATS_CONTROL_MAX_BYTES`限制，也不创建 JetStream
consumer。

## 9. JSON 工作流消息结构

JSON 工作流消息由`NatsComm`编码为 JSON，推荐结构：

```python
{
    "workflow_id": "workflow-001",
    "request_id": "request-001",
    "payload": {
        "camera_id": "camera-01",
        "operation": "detect",
    },
}
```

`send_and_wait()`会自动增加`reply_subject`。处理方读取该字段并原样交给
`publish_core()`，不要自行拼接回复 subject。

JSON 消息受`NATS_CONTROL_MAX_BYTES`限制。二进制帧使用第 6、7 节的 bytes API，
不要转换成 Base64 放入 JSON。

## 10. 接收 JSON 任务并回复

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

## 11. 发送 JSON 任务并等待回复

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

## 12. Agent 调用下游 Agent

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

## 13. 单向 JSON 任务

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

## 14. 手动拉取 JSON 消息

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

## 15. Core NATS JSON 请求响应

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

## 16. 方法选择

| 业务场景 | 发送端 | 接收端 | 消息是否持久化 |
|---|---|---|---|
| local/global 二进制帧 | `request_frame_bytes()` | `subscribe_frame_bytes()` | 不持久化 |
| 自定义 Core NATS bytes | `request_bytes()` | `subscribe_bytes()` | 不持久化 |
| 发送任务并等待结果 | `send_and_wait()` | `serve()` + `publish_core()` | 任务持久化，回复不持久化 |
| 只发送任务 | `send()` | `serve()`或`receive()` | 持久化 |
| 手动批量拉取 | `send()` | `receive()` | 持久化 |
| 在线实时 RPC | `request()` | `respond()` | 不持久化 |
| 向指定回复地址返回结果 | - | `publish_core()` | 不持久化 |

## 17. 并发配置

模型必须逐帧处理时：

```text
NATS_MAX_INFLIGHT=1
```

模型可以并行处理时，根据单个 Agent 进程可承受的并发量设置：

```text
NATS_MAX_INFLIGHT=4
```

`max_inflight`同时用于`serve()`的 JSON handler 和`subscribe_frame_bytes()`的
二进制 handler。一个 Pod 有多个进程时，总并发量约为：

```text
进程数 * NATS_MAX_INFLIGHT
```

长时间运行的模型任务应保留：

```text
NATS_ACK_PROGRESS_INTERVAL_SEC=10
```

## 18. 同步框架接入

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

二进制帧接口同样不能在同步 handler 中临时创建：

```python
asyncio.run(NatsComm().request_frame_bytes(...))
```

## 19. 多副本和多进程

部署多个 Agent Pod 副本时：

- 所有副本订阅相同 `IN_SUBJECT`。
- 需要竞争消费同一批任务时，所有副本使用相同 `DURABLE`。
- 二进制接收副本使用相同`agent_id`、`operation`和`queue`。
- 每个进程创建并维护自己的 `NatsComm`。
- 每个进程退出时关闭自己的连接。

示例：

```text
Pod 1 / worker 1 -> NatsComm connection 1
Pod 1 / worker 2 -> NatsComm connection 2
Pod 2 / worker 1 -> NatsComm connection 3
Pod 2 / worker 2 -> NatsComm connection 4
```

## 20. 二进制压力测试

仓库提供`tests/nats_binary_load.py`，必须在 Conda `k8s`环境并从仓库根目录运行。

同集群 loopback：

```bash
conda activate k8s
PYTHONPATH=. python tests/nats_binary_load.py \
  --mode loopback \
  --servers nats://nats:4222 \
  --local-cluster edge-a \
  --target-cluster edge-a \
  --size-mib 10 \
  --fps 10 \
  --duration-sec 10
```

跨机器测试时，在目标集群先启动接收端：

```bash
conda activate k8s
PYTHONPATH=. python tests/nats_binary_load.py \
  --mode server \
  --servers nats://nats:4222 \
  --local-cluster edge-b \
  --agent-id binary-load
```

在源集群启动发送端：

```bash
conda activate k8s
PYTHONPATH=. python tests/nats_binary_load.py \
  --mode client \
  --servers nats://nats:4222 \
  --local-cluster edge-a \
  --target-cluster edge-b \
  --agent-id binary-load \
  --size-mib 10 \
  --fps 10 \
  --duration-sec 10
```

服务端输出应包含：

```text
frame.local.edge-b.binary-load.infer
frame.global.edge-b.binary-load.infer
```

客户端输出的`START subject`应为：

```text
frame.global.edge-b.binary-load.infer
```

成功标准为`completed`等于`sent`且`errors=0`。

完整的三节点 local/global 5 分钟稳定性测试、NATS 监控方法、结果和可交给 Codex
子 Agent 的测试 Prompt 见
[NATS 二进制帧 local/global 稳定性测试](nats-binary-soak-test.md)。

## 21. Agent 接入检查清单

- Agent 只连接本集群 `NATS_SERVERS`。
- 每个 Agent 正确设置本机`CLUSTER_ID`。
- 每个进程只创建一个长期使用的 `NatsComm`。
- 所有消息处理复用同一个实例和 asyncio 事件循环。
- 二进制发送使用`request_frame_bytes()`并传入目标集群。
- 二进制接收在启动时调用一次`subscribe_frame_bytes()`。
- 同集群生成`frame.local.*`，跨集群生成`frame.global.*`。
- Agent 不把帧编码成 Base64 或放入 JSON。
- 完整二进制消息不超过`NATS_BINARY_MAX_BYTES`。
- 常驻任务消费使用 `serve()`和稳定的 durable。
- 需要回复的任务使用 `send_and_wait()`发送。
- 接收端使用请求中的 `reply_subject`调用`publish_core()`。
- 模型串行推理时设置`NATS_MAX_INFLIGHT=1`。
- 长任务配置`NATS_ACK_PROGRESS_INTERVAL_SEC`。
- 进程退出时调用`close()`。
- Agent JSON 消息不超过`NATS_CONTROL_MAX_BYTES`。
