# Agent NATS 接入配置

本文档说明新的 Agent 如何接入当前的云边 NATS 通信方案。

当前通信拓扑：

```text
Agent Pod
  -> 本集群 Service/nats:4222
  -> 本集群 NATS leafnode
  -> 云端 NATS Hub
  -> JetStream domain: hub
```

Agent 不需要知道云端 IP，也不需要知道其他集群的 IP。跨集群路由只通过 NATS subject 完成。

## 必需环境变量

每个 Agent 都应配置：

```yaml
- name: NATS_SERVERS
  value: "nats://nats:4222"
- name: NATS_JETSTREAM_DOMAIN
  value: "hub"
- name: NATS_STREAM_SUBJECTS
  value: "workflow.>"
- name: CLUSTER_ID
  value: "<edge-cluster-id>"
- name: AGENT_ID
  value: "<agent-id>"
```

示例：

```yaml
- name: NATS_SERVERS
  value: "nats://nats:4222"
- name: NATS_JETSTREAM_DOMAIN
  value: "hub"
- name: NATS_STREAM_SUBJECTS
  value: "workflow.>"
- name: CLUSTER_ID
  value: "edge-a"
- name: AGENT_ID
  value: "agent-d"
```

## Subject 命名

统一格式：

```text
workflow.<cluster-id>.<agent-id>.in
```

示例：

```text
workflow.edge-a.agent.d.in
workflow.edge-b.agent.d.in
workflow.edge-a.perception.in
workflow.edge-b.planner.in
```

建议 Agent 将自己的输入 subject 配成环境变量：

```yaml
- name: IN_SUBJECT
  value: "workflow.edge-a.agent.d.in"
```

如果 Agent 需要把请求转发给其他 Agent，也把目标 subject 配成环境变量：

```yaml
- name: NEXT_SUBJECT
  value: "workflow.edge-a.agent.e.in"
```

## 回复约定

请求 payload 建议带：

```json
{
  "workflow_id": "task-001",
  "text": "hello",
  "reply_subject": "workflow.edge-b.reply.task-001"
}
```

处理完成后，Agent 将结果发布到 `reply_subject`：

```json
{
  "workflow_id": "task-001",
  "result": "done"
}
```

推荐回复 subject：

```text
workflow.<requester-cluster-id>.reply.<workflow-id>
```

示例：

```text
workflow.edge-b.reply.task-001
workflow.edge-a.reply.9f4a2c
```

这样可以做到：

```text
edge-b 发任务给 edge-a
edge-a 处理
edge-a 回复到 workflow.edge-b.reply.<workflow-id>
edge-b 接收结果
```

## Python Agent 示例

```python
import asyncio
import os

from runtime_api import NatsComm

IN_SUBJECT = os.environ["IN_SUBJECT"]
DURABLE = os.environ.get("DURABLE", f"{os.environ.get('AGENT_ID', 'agent')}-consumer")


async def main():
    comm = NatsComm()

    async def handler(data):
        workflow_id = data["workflow_id"]
        reply_subject = data.get("reply_subject")
        text = data.get("text", "")

        result = {
            "workflow_id": workflow_id,
            "result": f"processed: {text}",
        }

        if reply_subject:
            await comm.send(reply_subject, result)

    try:
        await comm.serve(
            subject=IN_SUBJECT,
            durable=DURABLE,
            handler=handler,
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
```

## Deployment 模板

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-d
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-d
  template:
    metadata:
      labels:
        app: agent-d
    spec:
      containers:
        - name: agent-d
          image: agent-d:latest
          imagePullPolicy: IfNotPresent
          env:
            - name: AGENT_ID
              value: "agent-d"
            - name: NATS_SERVERS
              value: "nats://nats:4222"
            - name: NATS_JETSTREAM_DOMAIN
              value: "hub"
            - name: NATS_STREAM_SUBJECTS
              value: "workflow.>"
            - name: CLUSTER_ID
              value: "edge-a"
            - name: IN_SUBJECT
              value: "workflow.edge-a.agent.d.in"
          resources:
            requests:
              cpu: "200m"
              memory: "256Mi"
            limits:
              cpu: "1"
              memory: "1Gi"
```

## 验证

确认本集群 NATS 能访问云端 JetStream：

```bash
kubectl exec deploy/nats-box -- \
  nats req '$JS.hub.API.INFO' '{}' \
  --server nats://nats:4222
```

返回中应包含：

```json
"domain":"hub"
```

向 Agent 发测试消息：

```bash
kubectl exec deploy/nats-box -- \
  nats pub workflow.edge-a.agent.d.in \
  '{"workflow_id":"test-001","text":"hello","reply_subject":"workflow.edge-a.reply.test-001"}' \
  --server nats://nats:4222
```

接收回复时，要先订阅再发送：

```bash
kubectl exec -it deploy/nats-box -- \
  nats sub 'workflow.edge-a.reply.>' \
  --server nats://nats:4222
```

如果消息已经发出，普通 `nats sub` 会错过历史消息。需要查历史时，应使用 JetStream consumer。

## 常见问题

### Agent 一直报 ServiceUnavailableError

通常是 Agent 没有设置：

```text
NATS_JETSTREAM_DOMAIN=hub
```

或者镜像里包含的 `runtime_api` 不是最新版。

处理方式：

```bash
docker build -f <agent>/Dockerfile -t <agent-image>:<new-tag> .
minikube image load <agent-image>:<new-tag>
kubectl set image deployment/<agent> <container>=<agent-image>:<new-tag>
```

### B 集群发给 A，B 没收到回复

先在 A 集群看目标 Agent 日志：

```bash
kubectl logs deploy/agent-b --tail=120
```

如果 A 已经发布到：

```text
workflow.edge-b.reply.<workflow-id>
```

但 B 没收到，通常是 B 订阅晚了。实时验证时必须：

```text
先在 B 订阅 workflow.edge-b.reply.>
再从 B 发布请求
```

### Service/nats 没有 endpoint

常见原因：集群已用 Helm 部署 NATS，但 AOE 在部署 Agent 时用 `app=nats` patch 了 `svc/nats`，导致 selector 与 Helm Pod 标签不匹配。

先清理并修复：

```bash
kubectl delete deployment nats --ignore-not-found
kubectl delete configmap nats-config --ignore-not-found
kubectl patch svc nats --type=json -p='[{"op":"remove","path":"/spec/selector/app"}]' || true
kubectl get endpoints nats -o wide
```

使用 Helm 时，启动 AOE 请设置 `NATS_MANAGED_EXTERNALLY=true`（`scripts/start_cluster_*_aoe.sh` 已默认开启），避免 AOE 再次 patch Service。

检查：

```bash
kubectl get endpointslice -l kubernetes.io/service-name=nats -o wide
```

如果仍为空，可能是旧手写 YAML 留下了 `app=nats` selector。重新执行：

```bash
bash scripts/setup_edge_nats_helm.sh
# 默认 cloud_host=10.112.136.44、edge_cluster_id=edge-a（见 scripts/edge-nats.defaults.env）
# 覆盖：NATS_CLOUD_HOST=... EDGE_CLUSTER_ID=edge-b bash scripts/setup_edge_nats_helm.sh
```

脚本会清理旧 selector。
