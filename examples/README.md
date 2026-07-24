# runtime_api 使用手册

本文档说明外部容器化应用如何使用 `runtime_api.NatsComm` 和
`runtime_api.FrameComm` 接入 K8S_demo。

## 1. 接入方式

应用镜像需要包含两部分：

- `runtime_api/` 目录
- Python 依赖 `nats-py`；传输大帧时还需要 `grpcio` 和 `protobuf`

推荐 Dockerfile 写法：

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY examples/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runtime_api ./runtime_api
COPY examples/external_app_send.py .

CMD ["python", "external_app_send.py"]
```

Kubernetes Pod 内需要注入 NATS 地址：

```yaml
env:
  - name: NATS_SERVERS
    value: "nats://nats:4222"
```

本地直接运行示例时，需要从 `K8S_demo` 目录启动，或设置 `PYTHONPATH`：

```bash
cd /home/t/Projects/czl/K8S_demo
pip install -r examples/requirements.txt
export PYTHONPATH=$PWD
python examples/external_app_send.py
```

## 2. API 概览

`NatsComm` 提供两类通信：

- JetStream 持久消息：`send()`、`receive()`、`serve()`
- Core NATS 瞬时请求响应：`request()`、`respond()`

`FrameComm` 在 `NatsComm` 上增加 gRPC 帧数据面：

- `send(..., frame_path=...)`：先上传帧，再向 NATS 写入 `frame_ref`
- `serve(..., download_frames=True)`：自动下载、校验并清理临时帧
- 不带帧调用 `FrameComm.send()` 时，行为与 `NatsComm.send()` 一致

发送帧：

```python
comm = FrameComm()
await comm.send(
    "workflow.demo.frame.in",
    {"workflow_id": "task-1"},
    frame_path="/data/frame.bin",
)
```

消费帧：

```python
async def handler(data):
    model.infer(data["frame_path"])

await comm.serve(
    subject="workflow.demo.frame.in",
    durable="frame-worker",
    handler=handler,
    download_frames=True,
    delete_remote_frame=True,
)
```

`frame_path` 只在 handler 执行期间有效。完整示例见
`external_app_send_frame.py` 和 `external_app_receive_frame.py`。

`send()/receive()/serve()` 默认使用 `WORKFLOW` stream，默认 subjects 为 `workflow.demo.>`。如果要使用其他 subject 前缀，需要创建 `NatsComm(stream_subjects=[...])`。

## 3. 函数区别与使用场景

| 函数 | 通信类型 | 是否持久化 | 是否等待对方回复 | 典型使用场景 |
| --- | --- | --- | --- | --- |
| `send()` | JetStream 发布 | 是 | 否 | 把任务、事件、工作流消息投递给下游，要求消息可追踪、可重放、不因消费者短暂离线而丢失。 |
| `receive()` | JetStream 拉取消费 | 是 | 否 | 脚本或定时任务一次性拉取一批消息，自己决定何时 `ack()`、`nak()`、`term()`。 |
| `serve()` | JetStream 常驻消费 | 是 | 否 | 长期运行的 worker 持续处理任务；handler 成功后自动 `ack()`，失败时尝试 `nak()`。 |
| `send_and_wait()` | JetStream任务 + Core NATS回复 | 仅任务持久化 | 是 | 任务需要重投，但调用方也需要等待本次处理结果。 |
| `publish_core()` | Core NATS 发布 | 否（使用`_INBOX`时） | 否 | 向请求payload携带的临时reply subject发送短生命周期回复。 |
| `request()` | Core NATS 请求 | 否 | 是 | 在线查询、健康检查、控制命令等需要立即拿到返回值的同步调用。 |
| `respond()` | Core NATS 响应 | 否 | 被动回复 | 和 `request()` 配套，启动一个在线 responder，收到请求后返回结果。 |

选择建议：

- 需要“任务一定被处理、消费者可以晚点上线、失败后可重试”时，使用 `send()` + `receive()` 或 `send()` + `serve()`。
- 需要“调用方马上拿到结果，超时就算失败”时，使用 `request()` + `respond()`。
- 只处理一批已有消息、处理逻辑由脚本控制时，用 `receive()`。
- 要部署成 Kubernetes 中的常驻业务 worker 时，用 `serve()`。
- 持久任务需要同步等待结果时使用`send_and_wait()`；它会自动生成`_INBOX`并在结束后注销回复订阅。
- 下游使用`publish_core(payload["reply_subject"], reply)`回包，不要手工创建`workflow.*.reply.*`。
- `request()/respond()` 不进入 JetStream stream，responder 不在线或超时会直接失败，不适合需要离线堆积和重放的任务。

常见搭配：

```text
异步任务：send("workflow.demo.agent.b.in", payload) -> serve("workflow.demo.agent.b.in", ...)
批量消费：send(...) -> receive(..., batch=10) -> message.ack()
同步查询：request("workflow.demo.request.status", payload) -> respond("workflow.demo.request.status", handler)
持久任务回包：send_and_wait("workflow.demo.agent.b.in", payload) -> publish_core(reply_subject, reply)
```

## 4. 发送消息

```python
import asyncio
from runtime_api import NatsComm

async def main():
    comm = NatsComm()
    try:
        reply = await comm.send_and_wait(
            subject="workflow.demo.agent.b.in",
            payload={
                "workflow_id": "external-app-1",
                "text": "hello from another container",
            },
            timeout_sec=120,
        )
        print("reply:", reply)
    finally:
        await comm.close()

asyncio.run(main())
```

参考文件：`external_app_send.py`

如果只投递任务、不等待结果，使用`send()`；它返回`subject`、`stream`和`seq`。

## 5. 接收消息

```python
import asyncio
from runtime_api import NatsComm

async def main():
    comm = NatsComm()
    try:
        messages = await comm.receive(
            subject="workflow.demo.events.result",
            durable="external-app-result-consumer",
            batch=10,
            timeout_sec=10,
        )
        for message in messages:
            print("received:", message.payload)
            await message.ack()
    finally:
        await comm.close()

asyncio.run(main())
```

参考文件：`external_app_receive.py`

注意：

- `receive()` 默认不会自动 ack。
- 业务处理成功后调用 `message.ack()`。
- 业务处理失败且希望重投递时调用 `message.nak()`。
- 如果只想拉取并立即确认，可传 `ack=True`。
- 多个消费者共享同一个 `durable` 时要谨慎，durable consumer 会保存消费进度。

## 6. 常驻 Worker

常驻 worker 推荐使用 `serve()`：

```python
import asyncio
from runtime_api import NatsComm

async def handle(payload):
    print("payload:", payload)

async def main():
    comm = NatsComm()
    try:
        await comm.serve(
            subject="workflow.demo.my_app.in",
            durable="my-app-consumer",
            handler=handle,
        )
    finally:
        await comm.close()

asyncio.run(main())
```

`serve()` 会循环拉取消息，handler 正常返回后 ack；handler 抛异常时会尝试 nak。

## 7. 请求-响应

`request()/respond()` 使用 Core NATS request/reply，适合在线查询，不进入 JetStream 持久队列。

Responder：

```python
import asyncio
from runtime_api import NatsComm

async def handle_request(payload):
    return {"status": "ok", "received": payload}

async def main():
    comm = NatsComm()
    try:
        await comm.respond(
            subject="workflow.demo.request.status",
            handler=handle_request,
            queue="external-app-responders",
        )
    finally:
        await comm.close()

asyncio.run(main())
```

Requester：

```python
import asyncio
from runtime_api import NatsComm

async def main():
    comm = NatsComm()
    try:
        reply = await comm.request(
            subject="workflow.demo.request.status",
            payload={"text": "hello from request api"},
            timeout_sec=30,
        )
        print("reply:", reply)
    finally:
        await comm.close()

asyncio.run(main())
```

参考文件：`external_app_responder.py`、`external_app_request.py`

## 8. Subject 语言

NATS subjects 不是 POSIX/PCRE 正则表达式，而是点分 token + 通配符语言。

基本规则：

- subject 由 `.` 分隔，例如 `workflow.demo.agent.b.in`。
- 普通 token 按字面量匹配。
- `*` 匹配一个 token，例如 `workflow.demo.*.b.in` 可以匹配 `workflow.demo.agent.b.in`。
- `>` 匹配当前位置之后的所有剩余 token，并且只能放在最后，例如 `workflow.demo.>`。
- `>` 至少匹配一个 token，因此 `workflow.demo.>` 匹配 `workflow.demo.agent.b.in`，不匹配 `workflow.demo`。
- 不支持 `^`、`$`、`.*`、`[a-z]` 这类正则写法。

建议约定：

```text
workflow.demo.<app-or-agent>.<direction>
workflow.demo.agent.b.in
workflow.demo.agent.c.in
workflow.demo.request.status
```

如果需要按工作流隔离回包，使用`send_and_wait()`自动生成唯一`_INBOX`：

```python
reply = await comm.send_and_wait(
    "workflow.demo.agent.b.in",
    {"workflow_id": workflow_id, "text": text},
    timeout_sec=120,
)
```

下游处理完成后通过`publish_core()`发回payload中的`reply_subject`。

## 9. agent_gRPC 调用链路

当前 demo 的链路是：

```text
client.py
  -> agent_gRPC gRPC :50051
  -> NatsComm.send_and_wait("workflow.demo.agent.b.in")
  -> agent_b
  -> NatsComm.send_and_wait("workflow.demo.agent.c.in")
  -> agent_c
  -> agent_b
  -> NatsComm.publish_core(_INBOX)
  -> agent_gRPC 返回 gRPC response
```

构建和部署：

```bash
cd /home/t/Projects/czl/K8S_demo
docker build -f agent_gRPC/Dockerfile -t agent-grpc:v1 .
docker build -f agent_b/Dockerfile -t agent-b-worker:v3 .
docker build -f agent_c/Dockerfile -t agent-c-worker:v1 .

kubectl apply -f k8s/nats.yaml
kubectl apply -f k8s/agent-grpc-deploy.yaml
kubectl apply -f k8s/agent-grpc-svc.yaml
kubectl apply -f k8s/agent-b-deploy.yaml
kubectl apply -f k8s/agent-c-deploy.yaml
```

测试：

```bash
kubectl port-forward service/agent-grpc 50051:50051
AGENT_GRPC_ADDR=localhost:50051 python client.py
```

## 10. 日志排查

先确认真实 Deployment 名：

```bash
export NO_PROXY="$(minikube ip),192.168.49.2,localhost,127.0.0.1"
kubectl get deploy
```

静态清单部署时：

```bash
kubectl logs deployment/agent-grpc -f
kubectl logs deployment/agent-b -f
kubectl logs deployment/agent-c -f
```

编排器动态部署时，Deployment 名来自 agent_id 的安全化结果。旧配置可能是：

```bash
kubectl logs deployment/agent-a-agent -f
```

改名后优先找 `agent-grpc`：

```bash
kubectl get deploy | grep agent
kubectl logs deployment/<真实 deployment 名> -f
```

如果 `agent_gRPC` 日志只有启动信息，没有 `received gRPC request`，说明还没有 gRPC 请求进入。先运行：

```bash
AGENT_GRPC_ADDR=<node-ip>:30051 python client.py
```

如果 `agent_gRPC` 有 `sending request` 但没有 `received reply payload`，继续看：

```bash
kubectl logs deployment/agent-b -f
kubectl logs deployment/agent-c -f
```

常见原因：

- `agent_b` 或 `agent_c` 没启动。
- `NATS_SERVERS` 不可达。
- 镜像不是从 `K8S_demo` 根目录构建，导致缺少 `runtime_api`。
- 下游没有按 `reply_subject` 发回包。
