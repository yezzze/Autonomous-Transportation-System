# Agent 实例 NATS 配置与编排

## 1. 编排器、Pod 与 Agent 生命周期

一次 Agent 实例启动按以下顺序执行：

1. 编排器直接创建目标集群中的 Pod。
2. Kubernetes 通过 Downward API 注入 `AGENT_INSTANCE_ID=metadata.uid`。
3. Agent 调用 `await NatsComm.create()` 创建一个进程级客户端并等待
   `WF_<pod-uid>` 就绪。
4. `serve_workflow()` 创建 File 类型 `WF_<pod-uid>`；
   `serve_memory_frames()` 创建 Memory 类型 `FRAME_<pod-uid>`。
5. Agent 建立 local/global Consumer 并通过 readiness 表示可接收任务。
6. 编排器将实例登记为 Ready，并把
   `cluster_id + agent_id + pod_uid` 写入工作流路由表。
7. 调用方按路由表发送到精确实例。

实例结束按以下顺序执行：

1. 从路由表移除实例，停止新任务进入。
2. 等待正在执行的任务完成或达到终止期限。
3. 编排器正常终止 Pod。
4. Agent 在 `finally`/`async with` 中调用 `NatsComm.close()`。
5. `close()` 删除 `WF_<pod-uid>`、`FRAME_<pod-uid>` 和 Consumer。

`SIGKILL` 或节点掉电不会执行 Agent 清理代码。编排器必须保存被删除 Pod 的
UID，并在确认 Pod 不存在后直接调用 NATS 删除接口兜底清理残留 Stream。

Agent 模式不要设置 `NATS_STREAM` 或 `NATS_STREAM_SUBJECTS`。`NatsComm()` 从
`CLUSTER_ID`、`AGENT_ID` 和 `AGENT_INSTANCE_ID` 生成默认的实例级 Workflow
Stream；三个变量必须同时注入。只有旧兼容调用才显式配置自定义 Stream。
Agent 未显式设置 `NATS_JETSTREAM_DOMAIN` 时默认使用 `CLUSTER_ID`；如果设置为
其他 domain（例如误设为 `hub`），构造客户端时直接报错，避免把实例 Stream
创建到云端。

`__init__()` 负责读取身份并生成 Stream 名称和 Subjects；NATS 服务端创建是
异步网络操作，由 `await NatsComm.create()` 完成。该方法返回时 Stream 已经
存在。只需要发送消息、不需要在构造阶段创建本实例 Stream 时，仍可使用
`NatsComm()` 的延迟连接方式。

## 2. Agent 自主管理接口

```python
from runtime_api import NatsComm

comm = await NatsComm.create()
try:
    await asyncio.gather(
        comm.serve_workflow(
            agent_id=agent_id,
            durable=instance_id,
            local_cluster=cluster_id,
            handler=workflow_handler,
        ),
        comm.serve_memory_frames(
            agent_id=agent_id,
            local_cluster=cluster_id,
            handler=frame_handler,
        ),
    )
finally:
    await comm.close()
```

两个 `serve_*` 方法都会从 `AGENT_INSTANCE_ID` 读取实例 ID。只使用一种消息
类型时，只启动对应服务。`close()` 只删除当前 `NatsComm` 实际登记管理的
Stream。

编排器处理异常退出的兜底清理：

```python
cleanup = NatsComm()
try:
    await cleanup.delete_workflow_stream(
        target_cluster=cluster_id,
        instance_id=pod_uid,
    )
    await cleanup.delete_memory_frame_stream(
        target_cluster=cluster_id,
        instance_id=pod_uid,
    )
finally:
    await cleanup.close()
```

创建和删除均幂等。正常生命周期不需要显式调用这些底层方法。

旧控制器兼容模式才传入：

```python
await comm.serve_workflow(
    agent_id=agent_id,
    durable=instance_id,
    local_cluster=cluster_id,
    handler=workflow_handler,
    manage_stream_lifecycle=False,
)
await comm.serve_memory_frames(
        agent_id=agent_id,
        local_cluster=cluster_id,
        handler=frame_handler,
        manage_stream_lifecycle=False,
)
```

## 3. 命令行管理入口

在仓库根目录并激活 `k8s` conda 环境：

```bash
conda activate k8s

python scripts/workflow_stream_admin.py \
  --server nats://nats:4222 \
  provision \
  --cluster edge-a \
  --agent detector \
  --instance 8ecbd76e-84db-4f92-9af7-870de8a62211
```

删除：

```bash
python scripts/workflow_stream_admin.py \
  --server nats://nats:4222 \
  delete \
  --cluster edge-a \
  --instance 8ecbd76e-84db-4f92-9af7-870de8a62211
```

命令连接当前可达的本地 NATS，再通过 JetStream domain 访问目标边缘
JetStream。`--cluster` 必须与该边缘 NATS 的 domain 一致。

## 4. Subject 与 Stream

```text
workflow.local.<cluster>.agent.<agent-id>.instance.<pod-uid>.<operation>
workflow.global.<cluster>.agent.<agent-id>.instance.<pod-uid>.<operation>
```

每个实例的 Stream：

```text
WF_<pod-uid>
FRAME_<pod-uid>
```

绑定：

```text
workflow.local.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
workflow.global.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
frame.local.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
frame.global.<cluster>.agent.<agent-id>.instance.<pod-uid>.>
```

一个 Agent 类型有多个副本时，每个 Pod UID 对应不同 Stream。工作流调度器
选择具体副本，不通过多个 Stream 竞争同一个 subject。

`jetstream(domain="edge-b")` 只选择 JetStream 管理 API 和 Consumer API 的
domain，不会让 `publish(subject)` 自动定向到 edge-b。工作流发布接口会同时：

1. 从 subject 中解析目标集群和目标实例。
2. 使用目标集群的 JetStream domain 上下文。
3. 携带 `Nats-Expected-Stream: WF_<目标实例>` 发布。
4. 校验 PubAck 返回的 Stream 名称。

因此目标边缘不可达、目标实例 Stream 尚未创建或 ACK 来自错误 Stream 时，发送
会明确失败，不会静默回退到 hub。domain 不能替代 Stream 约束，也不能替代
LeafNode 的 subject 隔离。

## 5. Kubernetes 注入

Agent 自身实例 ID：

```yaml
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
```

调用下游实例所需信息由编排器或工作流运行时提供：

```yaml
- name: TARGET_CLUSTER_ID
  value: "edge-b"
- name: TARGET_AGENT_ID
  value: "detector"
- name: TARGET_INSTANCE_ID
  value: "<target-pod-uid>"
```

示例 Deployment 中的 `__AGENT_B_INSTANCE_ID__`、
`__AGENT_C_INSTANCE_ID__` 是编排器占位符，不能原样部署。

## 6. Stream 容量策略

默认单实例工作流配置：

```text
max_bytes = 512MiB
discard = new
retention = workqueue
storage = file
```

工作流消息应小于 1MiB。512MiB 是等待消费和故障恢复的缓冲，不用于存帧。
ACK 后 WorkQueue 消息删除。Stream 删除时，其消费者和未处理任务一起删除。

默认单实例帧配置：

```text
max_bytes = 512MiB
max_age = 120s
max_msg_size = NATS_BINARY_MAX_BYTES + 64KiB
discard = new
retention = workqueue
storage = memory
```

Memory 容量由同一边缘 NATS 的所有 `FRAME_<uid>` 共享。正常串行消费时通常
只短暂保留一帧；NATS Pod 重启时未 ACK 的 Memory 帧会丢失，调用方需超时
重试并按 `request_id` 保证幂等。

边缘 JetStream PVC 容量必须覆盖同时运行实例的实际积压量，不能简单按
`实例数 * 512MiB` 全额预留，但必须监控：

```text
jetstream_storage_used
stream_bytes
stream_messages
consumer_num_pending
consumer_num_ack_pending
```

## 7. 从旧共享 Stream 迁移

升级前检查云端和边缘是否存在匹配 `workflow.>` 的共享 Stream。新架构中：

- 云端 Hub 不得创建匹配 `workflow.local.>` 或 `workflow.global.>` 的 Stream。
- 边缘不得保留覆盖所有 Agent 的共享 `WORKFLOW` Stream。
- 旧 Stream 如继续匹配新 subject，会造成重复存储或发布歧义。
- 即使客户端携带目标 Stream 约束，Hub 的重叠 Stream 仍可能抢先返回错误 ACK，
  所以必须删除重叠配置，不能依赖客户端忽略它。

迁移顺序：

1. 暂停新工作流。
2. 等待旧任务处理完成并备份必要状态。
3. 删除或改名旧共享 Stream，使其只匹配 `legacy.workflow.>`。
4. 部署唯一 JetStream domain 和 LeafNode local 隔离配置。
5. 启动新实例，由 Agent 按每个 Pod UID 创建独立 Stream。
6. 恢复调度。

每个边缘 NATS 的 `server_name` 必须唯一。Helm 安装脚本使用 `CLUSTER_ID` 作为
前缀，生成 `edge-a-nats-0`、`edge-b-nats-0` 等名称；不要让所有边缘都以默认
Pod 名 `nats-0` 接入 Hub，否则跨 LeafNode 的 JetStream API 和 global
workflow interest 可能无法传播到其他边缘。

## 8. 给另一台机器 Codex 的执行提示词

```text
请更新 K8S_demo 并按仓库 docs/agent-nats-config.md 执行实例级
JetStream 验证。使用 conda 环境 k8s，不要使用系统 Python。

要求：
1. 检查边缘 NATS 的 JetStream domain 等于 CLUSTER_ID，云端 domain 为 hub。
2. 检查 LeafNode 双向拒绝 workflow.local.> 和 frame.local.>。
3. 不创建云端共享 workflow Stream。
4. 模拟两个 Agent 使用不同 Pod UID 调用 start_workflow_stream。
5. 验证每个 UID 得到独立 WF_<uid>，且只存在于目标边缘 domain。
6. 分别发送同集群 local 工作流和跨集群 global 工作流，确认精确实例收到并 ACK。
7. 关闭两个 Agent 的 NatsComm，确认 Stream 和 consumer 一起消失；
   重复 close 不得失败。
8. 同时验证 15MiB frame.local 和 frame.global 请求响应，发送方收到回复后再发下一帧。
9. 观察 cloud、edge-a、edge-b 日志和监控，记录吞吐、p95、超时、重连、
   slow consumer、pending bytes、JetStream 存储位置。
10. 不要修改或删除与本次任务无关的用户改动。完成后报告命令、结果和异常位置。
```

## 9. 单 Agent 全生命周期测试

使用仓库内三节点 NATS 拓扑：

```bash
conda activate k8s
docker compose -f tests/nats-topology-compose.yaml up -d --wait

python tests/nats_agent_lifecycle_integration.py

docker compose -f tests/nats-topology-compose.yaml down -v
```

脚本默认连接 `nats://127.0.0.1:24223`，模拟 `edge-a` 中一个 Agent：

1. 生成唯一 `AGENT_INSTANCE_ID`。
2. 启动 `serve_workflow()` 和 `serve_memory_frames()`。
3. 验证创建 `WF_<instance-id>` 和 `FRAME_<instance-id>` 及各两个 Consumer。
4. 发送一条 local 工作流和一帧 15MiB 数据。
5. 验证消息已 ACK、两条 Stream 消息数均为 0。
6. 调用 `NatsComm.close()`。
7. 验证两条 Stream 都已删除。

指定已有 NATS：

```bash
python tests/nats_agent_lifecycle_integration.py \
  --nats-url nats://127.0.0.1:4222 \
  --cluster edge-c \
  --agent-id lifecycle-agent \
  --instance-id manual-edge-c-001 \
  --frame-size-bytes 15728640
```
