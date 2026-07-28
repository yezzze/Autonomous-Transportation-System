# Agent 实例 NATS 配置与编排

## 1. 编排器与边缘控制器负责的生命周期

一次 Agent 实例启动按以下顺序执行：

1. 外部编排器调用目标集群的边缘生命周期控制器。
2. 边缘控制器创建 Pod 并读取 `metadata.uid`。
3. 边缘控制器在本地 JetStream domain 创建 File 类型
   `WF_<pod-uid>` 和 Memory 类型 `FRAME_<pod-uid>`。
4. 两条 Stream 分别绑定该实例的 local/global 工作流和帧 subject。
5. 编排器将实例登记为 Ready，并把
   `cluster_id + agent_id + pod_uid` 写入工作流路由表。
6. 调用方按路由表发送到精确实例。

实例结束按以下顺序执行：

1. 从路由表移除实例，停止新任务进入。
2. 等待正在执行的任务完成或达到终止期限。
3. 调用边缘控制器的 DELETE API。
4. 控制器等待消息排空后删除 Pod、`WF_<pod-uid>` 和
   `FRAME_<pod-uid>`。

异常退出时，边缘控制器的定时 reconcile 会比较存量 `WF_<uid>`、
`FRAME_<uid>` 与当前 Pod UID；空的孤儿 Stream 超过保护期后自动删除，
非空孤儿 Stream 保留并告警。

控制器部署和 API 见
[`edge-lifecycle-controller.md`](edge-lifecycle-controller.md)。

## 2. Python 管理接口

编排器进程复用一个 `NatsComm`：

```python
from runtime_api import NatsComm

comm = NatsComm()

resource = await comm.provision_workflow_stream(
    target_cluster="edge-a",
    agent_id="detector",
    instance_id=pod_uid,
)
frame_resource = await comm.provision_memory_frame_stream(
    target_cluster="edge-a",
    agent_id="detector",
    instance_id=pod_uid,
)

deleted = await comm.delete_workflow_stream(
    target_cluster="edge-a",
    instance_id=pod_uid,
)
frame_deleted = await comm.delete_memory_frame_stream(
    target_cluster="edge-a",
    instance_id=pod_uid,
)
```

创建操作按 Stream 名幂等；重复创建会校正 subject 和容量策略。删除操作也
幂等，返回 `False` 表示 Stream 已不存在。

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

迁移顺序：

1. 暂停新工作流。
2. 等待旧任务处理完成并备份必要状态。
3. 删除或改名旧共享 Stream，使其只匹配 `legacy.workflow.>`。
4. 部署唯一 JetStream domain 和 LeafNode local 隔离配置。
5. 启动新实例并为每个 Pod UID 创建独立 Stream。
6. 恢复调度。

## 8. 给另一台机器 Codex 的执行提示词

```text
请更新 K8S_demo 并按仓库 docs/agent-nats-config.md 执行实例级
JetStream 验证。使用 conda 环境 k8s，不要使用系统 Python。

要求：
1. 检查边缘 NATS 的 JetStream domain 等于 CLUSTER_ID，云端 domain 为 hub。
2. 检查 LeafNode 双向拒绝 workflow.local.> 和 frame.local.>。
3. 不创建云端共享 workflow Stream。
4. 模拟编排器为两个不同 Pod UID 调用 provision_workflow_stream。
5. 验证每个 UID 得到独立 WF_<uid>，且只存在于目标边缘 domain。
6. 分别发送同集群 local 工作流和跨集群 global 工作流，确认精确实例收到并 ACK。
7. 删除实例 Stream，确认 Stream 和 consumer 一起消失；重复删除不得失败。
8. 同时验证 15MiB frame.local 和 frame.global 请求响应，发送方收到回复后再发下一帧。
9. 观察 cloud、edge-a、edge-b 日志和监控，记录吞吐、p95、超时、重连、
   slow consumer、pending bytes、JetStream 存储位置。
10. 不要修改或删除与本次任务无关的用户改动。完成后报告命令、结果和异常位置。
```
