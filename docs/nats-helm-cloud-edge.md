# NATS Helm 云边部署

## 1. 拓扑

```text
edge-a NATS (domain=edge-a)
        |
        | LeafNode
        v
cloud Hub NATS (domain=hub)
        ^
        | LeafNode
        |
edge-b NATS (domain=edge-b)
```

每个边缘 Kubernetes 集群内的 Agent 连接本地 `nats://nats:4222`。同集群
消息只在本地流动；跨集群消息经 LeafNode 到云端 Hub，再到目标边缘集群。

## 2. 路由边界

LeafNode 同时配置：

```yaml
denyImports:
  - "workflow.local.>"
  - "frame.local.>"
denyExports:
  - "workflow.local.>"
  - "frame.local.>"
```

因此：

```text
workflow.local.*  只在当前边缘集群
frame.local.*     只在当前边缘集群
workflow.global.* 可经过云端
frame.global.*    可经过云端
```

## 3. JetStream domain

每套 NATS 的 domain 必须唯一：

```text
cloud:  hub
edge-a: edge-a
edge-b: edge-b
```

运行时使用目标 `cluster_id` 作为 JetStream domain。云端 NATS 只转发 global
消息，不创建匹配 `workflow.>` 的共享 Stream。

每个 Agent Pod 的 Stream 位于其所属边缘 domain：

```text
WF_<pod-uid>     File，工作流任务
FRAME_<pod-uid>  Memory，二进制帧
```

不要 deny `$JS.>` 或 `_INBOX.>`。跨边缘 Memory 帧需要通过 `$JS` Domain API
写入目标 Stream，并通过 `_INBOX.*` 返回处理结果。

边缘 JetStream Memory Store 总上限为 `1Gi`，由该边缘 NATS 上所有
`FRAME_<pod-uid>` 共享。每实例 `512MiB` 是上限而非预分配，实际内存按当前
未 ACK 帧占用。

## 4. 64MiB 传输配置

所有云端和边缘 NATS 保持一致：

```yaml
config:
  merge:
    max_payload: 67108864
```

客户端：

```text
NATS_BINARY_MAX_BYTES=67108864
NATS_PENDING_SIZE_BYTES=134217728
NATS_BINARY_PENDING_BYTES=134217728
```

`max_payload=64MiB` 只允许单条消息达到该大小，不等于系统能无限并发大消息。
仍需限制帧并发并复用连接。

## 5. 云端安装

```bash
conda activate k8s
./scripts/setup_cloud_minikube_nats_helm.sh
```

默认：

- namespace：`nats-system`
- release：`nats`
- domain：`hub`
- `max_payload`：64MiB
- 不创建共享 workflow Stream

如旧环境显式设置过 `NATS_CREATE_CLOUD_WORKFLOW_STREAM=true`，升级时必须取消。

## 6. 边缘安装

每台边缘机器设置不同集群 ID：

```bash
export EDGE_CLUSTER_ID=edge-a
export NATS_CLOUD_HOST=<cloud-address>
export NATS_CLOUD_PASSWORD=<leaf-password>

conda activate k8s
./scripts/setup_edge_nats_helm.sh
```

另一集群使用 `EDGE_CLUSTER_ID=edge-b`。脚本会：

1. 将 Helm values 中的 domain 渲染为 `EDGE_CLUSTER_ID`。
2. 使用 `EDGE_CLUSTER_ID` 作为 NATS `server_name` 前缀，生成
   `edge-a-nats-0` 这类唯一身份，避免不同边缘都以 `nats-0` 接入 Hub 后抑制
   东西向 LeafNode interest。
3. 配置 LeafNode 到云端。
4. 应用 local subject 双向隔离。
5. 创建 `edge-cluster-config` ConfigMap。

## 7. 检查

检查配置：

```bash
kubectl -n nats-system get pods,svc,pvc
kubectl -n nats-system get configmap edge-cluster-config -o yaml
kubectl -n nats-system logs statefulset/nats | \
  grep -Ei "leaf|jetstream|slow consumer|error"
```

检查 domain：

```bash
nats --server nats://nats:4222 account info
```

检查 NATS Server 身份。每个边缘必须显示不同名称，例如 `edge-a-nats-0`、
`edge-b-nats-0`，不能全部显示 `nats-0`：

```bash
kubectl -n default port-forward service/nats 18222:8222
curl -s http://127.0.0.1:18222/varz | \
  jq '{server_name,jetstream_domain:.jetstream.config.domain}'
```

检查实例 Stream：

```bash
nats --server nats://nats:4222 \
  --js-domain edge-a stream list

nats --server nats://nats:4222 \
  --js-domain edge-a stream info WF_<pod-uid>
```

云端 `hub` domain 中不应出现 `WF_<pod-uid>`。

## 8. 升级注意事项

1. 所有云边 NATS 的 `max_payload` 都要是 67108864。
2. 每个边缘 domain 必须唯一，不能继续使用 `hub`。
3. 云端不能有匹配新 workflow subject 的共享 Stream。
4. Agent 镜像必须包含实例级 subject 运行时。
5. Pod 必须注入 `AGENT_INSTANCE_ID=metadata.uid`。
6. Agent 必须在 readiness 成功前启动 `serve_workflow()` /
   `serve_memory_frames()` 创建 Stream，并在实例结束时调用 `close()` 删除。
7. 示例清单中的目标实例 UID 占位符必须由编排器替换。

### 8.1 StatefulSet 不可变字段

从未启用 JetStream PVC 的旧 release 升级时，新增
`volumeClaimTemplates` 会被 Kubernetes 拒绝：

```text
UPGRADE FAILED: cannot patch "nats" with kind StatefulSet
updates to statefulset spec ... are forbidden
```

先停止新任务并等待 WF/FRAME 消息排空，再执行一次显式迁移：

```bash
NATS_RECREATE_STATEFULSET_ON_IMMUTABLE=true \
  ./scripts/setup_edge_nats_helm.sh
```

脚本第一次检测到该错误后会以 `--cascade=orphan` 删除旧 StatefulSet 控制器，
保留已有 PVC，再由 Helm 创建新 StatefulSet。随后脚本删除旧的 orphan NATS
Pod，让新 StatefulSet 按最新 PVC 模板重新创建 Pod。File Store PVC 不会被
脚本删除；Memory Stream 在 NATS Pod 重启后不会保留。

旧版脚本如果停在 `Waiting for partitioned roll out to finish`，手动执行：

```bash
kubectl delete pod nats-0 -n default --wait=true
kubectl rollout status statefulset/nats -n default --timeout=300s
```
