# 大帧通信改动与当前方案说明

更新时间：2026-07-24

## 1. 文档目的

本文记录本轮针对“大帧连续传输后逐渐变慢、最终请求超时”所做的排查、代码
修改、验证结果，以及最终确定的实际 Agent 通信方向。

需要明确区分两种状态：

- **仓库已经实现并验证的能力**：gRPC 大帧数据面、NATS 控制面、NATS 连接与
  pull subscription 复用。
- **实际 Agent 后续采用的目标方案**：帧数据继续使用 NATS Core
  request/reply 传输；同集群走本地 NATS，跨集群才经过 LeafNode 和云端 NATS。

当前仓库保留 gRPC 帧传输能力作为可选方案，但它不是实际 Agent 必须经过的链路。

## 2. 原问题与排查结论

原始现象：

- 单帧数据约 10 MiB 以上。
- 发送方发出一帧后等待处理结果，再发送下一帧。
- 运行一段时间后延迟突然增加，最终出现 HTTP、gRPC 或 NATS 等待超时。

因此，“发送速度长期高于消费速度”不是唯一原因。代码排查发现以下问题：

### 2.1 NATS pull subscription 重复创建

旧版 `NatsComm.receive()` 每调用一次都会执行一次 `pull_subscribe()`。
`serve()` 又会周期性调用 `receive()`，导致长时间运行的 Agent 不断创建新的
Core NATS inbox subscription。

Agent B 等待每个 workflow 的临时回复时，也会创建新的临时 JetStream
consumer。即使严格一发一回，订阅和 consumer 资源仍可能随运行时间增长。

### 2.2 JetStream ACK 不等于删除 Stream 消息

当前 WORKFLOW Stream 使用 Limits 保留策略。消息 ACK 只更新 consumer 状态，
不会立即从 Stream 删除消息。

如果大帧直接进入 JetStream，即使每次只发送一帧，Stream 文件仍会持续增长。
接近大小限制后，旧消息淘汰、文件删除和磁盘 I/O 可能造成延迟抖动。

### 2.3 JSON/Base64 和日志放大

10 MiB 原始二进制经过 Base64 后约为 13.3 MiB，再加 JSON 字段和序列化复制，
会进一步增加网络、CPU和内存开销。

旧代码如果打印完整 payload，还会把十几 MiB 的内容重复写入容器标准输出，
造成 kubelet 日志轮转和磁盘压力。

### 2.4 `max_payload` 不是当前 Helm 环境的主要问题

Helm 云端和边端 NATS 均已配置：

```text
max_payload: 67108864
```

仓库内普通 YAML 和多集群示例也已经统一为 64 MiB。因此，当前部署中不存在
8 MiB 配置导致 10 MiB 帧直接被拒绝的问题。

64 MiB 只表示消息可以被接收，不代表大消息经过 JetStream 持久化后一定稳定。

## 3. 已完成的代码修改

### 3.1 可选 gRPC 大帧数据面

提交：

```text
4196e1b feat: 增加基于 gRPC 的大帧传输数据面
```

实现内容：

- 新增 gRPC `UploadFrame`、`DownloadFrame` 和 `DeleteFrame`。
- 大帧按固定 chunk 传输并校验大小、顺序和 SHA-256。
- NATS 只发送 `frame_ref`、workflow字段和结果字段。
- 增加临时帧目录、容量上限、TTL和并发上传限制。
- 增加单帧smoke test和固定速率压力测试。

该能力仍保留，但实际 Agent 如果已经确定由 NATS 传输帧，不需要同时使用它。

### 3.2 所有 NATS 清单统一为 64 MiB

提交：

```text
ad755c1 chore: 统一所有 NATS 清单的最大消息大小为 64 MiB
```

以下配置均已统一为 `67108864`：

- Helm云端values。
- Helm边端values。
- `k8s/nats.yaml`。
- `k8s/nats-a.yaml`。
- `k8s/nats-b.yaml`。
- 多集群A/B示例。

### 3.3 NATS 连接和订阅复用

提交：

```text
9584e70 fix: 复用 Agent 的 NATS 连接与 JetStream 订阅
```

主要修改：

- 每个 Agent 进程长期持有一个 `NatsComm` 或 `FrameComm` 实例。
- `connect()`增加并发保护，避免并发请求重复建立连接。
- 持久 pull subscription 按 `subject + durable` 缓存并复用。
- 临时 pull subscription 在成功或超时后主动注销。
- 临时 consumer 配置自动清理时间。
- 增加NATS断连、重连和关闭日志。
- 删除回复链路中的完整payload日志。
- 同步gRPC服务使用后台asyncio事件循环持有长期NATS连接，不再每帧执行
  `asyncio.run()`并创建新连接。

### 3.4 JetStream任务与Core NATS回复分离

新增接口：

```python
await comm.send_and_wait(...)
await comm.publish_core(...)
```

当前语义：

- 任务消息写入JetStream，保证任务可被消费和重投。
- 请求方在发布任务前创建Core NATS `_INBOX`订阅。
- 处理方通过`publish_core()`回复请求payload中的`reply_subject`。
- 成功或超时后自动注销临时回复订阅。
- `_INBOX`不匹配`workflow.>`，因此回复不会写入WORKFLOW Stream。
- 不再为每个workflow创建JetStream回复consumer。

## 4. 当前仓库已实现的数据链路

当前Demo默认链路：

```text
发送端
  -> gRPC上传帧到frame service
  -> JetStream发送frame_ref和任务字段
  -> Agent消费任务
  -> gRPC下载帧
  -> Core NATS _INBOX回复
```

该链路用于验证“大帧不进入NATS”的实现，适用于希望把NATS限制在控制面的场景。

实际 Agent 已明确以NATS作为帧数据通道，因此不要求迁移到该gRPC链路。

## 5. 实际 Agent 的纯NATS目标链路

### 5.1 同集群

```text
发送Agent
  -> 本集群NATS Service
  -> 本集群目标Agent
  -> Core NATS reply
```

帧不需要到达云端，也不需要经过gRPC frame service。

推荐subject：

```text
frame.local.<cluster-id>.<agent-id>.infer
```

例如：

```text
frame.local.edge-a.perception.infer
```

`frame.local.>`应限制在本地NATS网络中，不通过LeafNode导出，确保本地帧不会被
云端订阅或Stream捕获。

### 5.2 跨集群

```text
源Agent
  -> 源集群本地NATS
  -> LeafNode
  -> 云端NATS Hub
  -> 目标集群LeafNode/NATS
  -> 目标Agent
  -> Core NATS reply原路返回
```

推荐subject：

```text
frame.global.<target-cluster-id>.<agent-id>.infer
```

例如：

```text
frame.global.edge-b.perception.infer
```

云端NATS在这条链路中只承担Core NATS消息路由，不要求把帧写入云端JetStream。

### 5.3 路由选择

发送方必须知道目标Agent所属集群：

```python
if target_cluster == local_cluster:
    subject = f"frame.local.{target_cluster}.{agent_id}.infer"
else:
    subject = f"frame.global.{target_cluster}.{agent_id}.infer"
```

不要仅依赖`NATS_JETSTREAM_DOMAIN`判断帧是否走云端。Core NATS帧传输不使用
JetStream domain，路由由subject、订阅兴趣和LeafNode权限共同决定。

## 6. NATS帧消息格式

NATS payload本身是二进制，不需要把帧转为Base64。

推荐使用Protobuf封装：

```proto
message FrameRequest {
  string workflow_id = 1;
  string source_cluster = 2;
  string target_cluster = 3;
  string content_type = 4;
  bytes frame = 5;
}

message FrameResponse {
  string workflow_id = 1;
  string result = 2;
  string error = 3;
}
```

发送时：

```python
response = await nc.request(
    subject,
    frame_request.SerializeToString(),
    timeout=120,
)
```

接收端在进程启动时注册一次Core NATS订阅，并复用同一连接：

```python
async def on_frame(message):
    request = FrameRequest.FromString(message.data)
    result = await run_model(request.frame)
    await message.respond(
        FrameResponse(
            workflow_id=request.workflow_id,
            result=result,
        ).SerializeToString()
    )

await nc.subscribe(subject, queue=agent_id, cb=on_frame)
```

禁止：

- 将帧Base64后放入JSON。
- 在每帧handler中创建新的NATS连接。
- 为每帧创建JetStream consumer。
- 打印完整帧payload。

## 7. 当前接口边界

当前`runtime_api.NatsComm.send()`是控制消息API：

- 参数类型为`Dict`。
- 会进行JSON序列化。
- 默认受`NATS_CONTROL_MAX_BYTES=1048576`保护。
- 不能直接用于发送10 MiB帧。

当前`FrameComm`实现的是gRPC帧传输，也不是纯NATS帧接口。

因此，实际Agent切换到纯NATS前，还需要增加独立的二进制接口，例如：

```text
request_bytes()
respond_bytes()
publish_bytes()
subscribe_bytes()
```

这些接口应直接调用Core NATS，不经过JSON编码、控制消息大小检查或JetStream。
在该接口完成前，不应删除现有gRPC能力，以免Demo和现有测试链路失效。

## 8. 可靠性与限制

Core NATS request/reply适合当前“一帧发送，等待回复后再发送下一帧”的在线调用模式。

需要接受以下语义：

- 目标Agent必须在线并已订阅。
- 目标离线时请求会超时，帧不会持久化。
- 网络断开后由调用方决定是否重试。
- 重试需要携带稳定的`workflow_id`，接收端应具备幂等处理能力。
- 全链路所有NATS Server的`max_payload`必须大于完整二进制消息。
- 10 MiB帧在64 MiB限制内，但仍会产生约10 MiB网络传输和内存复制。

如果未来要求目标离线后仍能恢复处理，应另行选择：

- JetStream分块与重组。
- NATS Object Store。
- MinIO对象存储加NATS引用。
- 已实现的gRPC帧服务加持久后端。

不要在没有离线需求时，把每个大帧都持久化到JetStream。

## 9. 已完成验证

### 9.1 单元测试

共14项测试通过，覆盖：

- 帧存储容量、完整性和删除。
- 持久pull subscription复用。
- 临时subscription成功和超时清理。
- `_INBOX`请求回复。
- `FrameComm.send_and_wait()`。

### 9.2 连接复用集成测试

连续200次请求结果：

```text
requests=200
requester_subscriptions=1
worker_subscriptions=2
consumers=['reuse-integration-worker']
stream_messages=200
```

订阅数没有随请求次数增长，也没有产生临时JetStream consumer。

### 9.3 10 MiB压力测试

参数：

```text
frame_size=10 MiB
rate=10 FPS
duration=10秒
requests=100
```

结果：

```text
success=100
failed=0
missing=0
upload P95=47.7ms
total P95=107.9ms
completed_fps=10.004
```

该结果验证的是当前gRPC帧数据面和NATS控制面链路，不代表尚未实现的纯NATS
二进制帧接口已经完成压力测试。

## 10. 实际Agent接入清单

已经可以执行：

- 升级到包含提交`9584e70`的`runtime_api`。
- 每个Agent进程创建一个长期NATS连接。
- 在进程启动时注册订阅，在进程退出时关闭连接。
- 多进程服务每个worker各维护一个连接。
- 禁止在单帧handler中创建连接或事件循环。

纯NATS帧接口完成后执行：

- 发送方改用`request_bytes()`。
- 接收方改用常驻`subscribe_bytes()`。
- 帧改为Protobuf原始bytes，不再使用Base64/JSON。
- 根据目标集群选择`frame.local.*`或`frame.global.*`。
- LeafNode只允许`frame.global.>`跨集群传播。
- 分别执行同集群和跨集群10 MiB连续压力测试。

## 11. 相关文档

- [实际Agent连接复用](agent-connection-reuse.md)
- [gRPC FrameComm跨机器部署与压测](frame-transport-handoff.md)
- [Agent NATS配置](agent-nats-config.md)
- [Helm云端Hub与边端LeafNode部署](nats-helm-cloud-edge.md)

