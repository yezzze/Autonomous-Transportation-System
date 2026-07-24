# NATS Helm Cloud/Edge 部署方案

本文档说明如何使用官方 `nats/nats` Helm Chart 搭建一套“云端 NATS Hub + 多个边缘 Kubernetes 集群”的通信拓扑。

目标：

- 云端统一管理 NATS JetStream，JetStream domain 固定为 `hub`。
- 每个边缘集群只在本集群内暴露 `nats://nats:4222`。
- 边缘 NATS 通过 leafnode 主动连接云端 Hub 的 `7422/TCP`。
- 业务只通过 subject 路由，不依赖对端 Pod IP、Node IP 或 Service IP。

## 拓扑

```text
cloud 集群 / 当前机器 cloud profile
  nats-hub
    JetStream domain: hub
    leafnode listener: 7422
    client port: 4222

edge-a 集群
  Agent -> nats://nats:4222
  local nats -> leafnode -> cloud:7422

edge-b 集群
  Agent -> nats://nats:4222
  local nats -> leafnode -> cloud:7422

edge-c 集群
  Agent -> nats://nats:4222
  local nats -> leafnode -> cloud:7422
```

业务 subject 约定：

```text
workflow.<cluster-id>.<agent-id>.in
```

示例：

```text
workflow.edge-a.agent.b.in
workflow.edge-b.agent.c.in
workflow.edge-c.agent.d.in
```

任务回复由`NatsComm.send_and_wait()`自动生成`_INBOX.*`，不使用
`workflow.*.reply.*`，避免回复进入WORKFLOW Stream。

## 前置条件

云端机器：

- `kubectl`
- `minikube`
- `helm`
- Docker 可用
- 边缘机器可以访问云端机器的 `7422/TCP`

边缘机器：

- `kubectl` 已指向边缘 Kubernetes 集群
- `helm`
- 可以访问云端 Hub 的 `7422/TCP`

如果国内网络拉 Docker Hub 不稳定，当前 values 已将官方 Chart 镜像 registry 改为：

```text
docker.m.daocloud.io
```

## 云端部署

如果需要在当前机器上创建独立 Linux 用户和独立 minikube profile：

```bash
sudo bash /home/czl/Project/K8S_demo/scripts/bootstrap_k8s_cloud_user.sh
sudo -iu k8s_cloud
```

部署云端 NATS Hub：

```bash
bash ~/Project/K8S_demo/scripts/setup_cloud_minikube_nats_helm.sh
```

该脚本会：

- 启动 `minikube -p cloud`
- 映射宿主机端口：
  - `4222 -> NodePort 30422`
  - `7422 -> NodePort 30472`
  - `8222 -> NodePort 30482`
- 在 `nats-cloud` namespace 安装 Helm release `nats-hub`
- 使用 [k8s/helm/nats-cloud-values.yaml](../k8s/helm/nats-cloud-values.yaml)

云端检查：

```bash
kubectl get pods,svc,pvc -n nats-cloud
```

期望：

```text
nats-hub-0   3/3   Running
nats-hub-1   3/3   Running
nats-hub-2   3/3   Running
nats-hub-box 1/1   Running
```

云端 Hub 对边缘暴露的 leafnode 地址为：

```text
<cloud-host>:7422
```

其中 `<cloud-host>` 必须是边缘机器可访问的云端机器 IP 或域名。

## 边缘集群部署

在边缘机器上，确保当前 kube context 指向边缘集群：

```bash
kubectl config current-context
```

部署边缘 NATS leafnode：

```bash
bash scripts/setup_edge_nats_helm.sh
```

每台机器复制本地配置（不提交 git）：

```bash
cp scripts/local/cluster.env.example scripts/local/cluster.env
# 编辑 LOCAL_CLUSTER=a|b|c、CLUSTER_A_HOST、CLUSTER_B_HOST、CLUSTER_C_HOST、NATS_CLOUD_PASSWORD
bash scripts/setup_edge_nats_helm.sh
```

临时覆盖配置文件路径：`CLUSTER_ENV_FILE=/path/to/cluster.env bash scripts/setup_edge_nats_helm.sh`

变量说明：

- `NATS_CLOUD_HOST`：云端 Hub 对当前边缘机器可达的 IP 或域名，不包含端口。
- `LOCAL_CLUSTER`：本机是 `a`、`b` 还是 `c`（自动选用 `CLUSTER_*_EDGE_ID`）。
- `CLUSTER_A_HOST` / `CLUSTER_B_HOST` / `CLUSTER_C_HOST`：三台宿主机 IP。
- `NATS_CLOUD_PASSWORD`：须与云端 leafnode 密码一致。

脚本会：

- 安装 Helm release `nats`
- 使用 [k8s/helm/nats-edge-values.yaml](../k8s/helm/nats-edge-values.yaml)
- 创建 `ConfigMap/edge-cluster-config`
- 等待 `statefulset/nats` Ready

检查：

```bash
kubectl get pods,svc
kubectl get configmap edge-cluster-config -o yaml
```

期望：

```text
nats-0    2/2   Running
nats-box  1/1   Running
```

## 验证云端 JetStream

在任意边缘集群执行：

```bash
kubectl exec deploy/nats-box -- \
  nats req '$JS.hub.API.INFO' '{}' \
  --server nats://nats:4222
```

返回中应包含：

```json
"domain":"hub"
```

查看统一 JetStream stream：

```bash
kubectl exec deploy/nats-box -- \
  nats stream info WORKFLOW \
  --server nats://nats:4222 \
  --js-domain hub
```

如果还没有 stream，可以创建（**Helm 不能设置 stream 的 max-bytes/discard，需 CLI 或应用自动建流**）：

```bash
kubectl exec deploy/nats-box -- \
  nats stream add WORKFLOW \
  --subjects 'workflow.>' \
  --storage file \
  --retention limits \
  --max-bytes 5GB \
  --discard old \
  --server nats://nats:4222 \
  --js-domain hub
```

应用侧（`runtime_api.NatsComm`、`/api/comm/nats/publish`）在首次 JetStream 调用时会自动 `ensure` 同名 stream，并读取环境变量：

```text
NATS_STREAM=WORKFLOW
NATS_STREAM_SUBJECTS=workflow.>
NATS_STREAM_MAX_BYTES=5GB
NATS_STREAM_DISCARD=old
NATS_STREAM_RETENTION=limits
NATS_STREAM_STORAGE=file
```

若 `WORKFLOW` 已存在且未带限制，需一次性迁移：

```bash
kubectl exec deploy/nats-box -- \
  nats stream edit WORKFLOW \
  --max-bytes 5GB \
  --discard old \
  --retention limits \
  --server nats://nats:4222 \
  --js-domain hub
```

验证：

```bash
kubectl exec deploy/nats-box -- \
  nats stream info WORKFLOW --json \
  --server nats://nats:4222 \
  --js-domain hub | jq '.config.max_bytes, .config.discard'
```

## 验证跨集群消息

在 edge-a 订阅：

```bash
kubectl exec -it deploy/nats-box -- \
  nats sub 'workflow.>' \
  --server nats://nats:4222
```

在 edge-b 发布：

```bash
kubectl exec deploy/nats-box -- \
  nats pub workflow.edge-a.test 'hello from edge-b' \
  --server nats://nats:4222
```

如果 edge-a 收到消息，说明两个边缘集群已通过云端 Hub 互通。

## Agent 配置

所有 Agent 保持连接本地 NATS：

```text
NATS_SERVERS=nats://nats:4222
NATS_JETSTREAM_DOMAIN=hub
NATS_STREAM_SUBJECTS=workflow.>
NATS_STREAM_MAX_BYTES=5GB
NATS_STREAM_DISCARD=old
CLUSTER_ID=<edge-cluster-id>
```

生成某个集群的 Agent subject env：

```bash
bash scripts/render_agent_subject_env.sh
```

示例输出：

```text
REQ_SUBJECT=workflow.edge-b.agent.b.in
IN_SUBJECT=workflow.edge-b.agent.b.in
C_IN_SUBJECT=workflow.edge-b.agent.c.in
```

## 常见问题

### Helm 下载 Chart 超时

如果另一台机器访问 GitHub release 超时，可以先在网络较好的机器下载 chart：

```bash
helm pull nats/nats --version 2.14.0 --destination charts
```

然后用本地 chart 包部署：

```bash
helm upgrade --install nats charts/nats-2.14.0.tgz \
  --namespace default \
  -f <rendered-edge-values.yaml>
```

### Pod 卡在 ImagePullBackOff

确认 Helm 渲染出的镜像已经使用 DaoCloud：

```bash
helm template nats nats/nats \
  --version 2.14.0 \
  -f k8s/helm/nats-edge-values.yaml | grep image:
```

期望镜像前缀：

```text
docker.m.daocloud.io
```

### Leafnode 反复连接又断开

先确认云端 Hub 已全部 Ready：

```bash
kubectl get pods -n nats-cloud
```

再检查边缘日志：

```bash
kubectl logs nats-0 -c nats --tail=120
```

看到类似下面内容，说明边缘已经识别到云端 JetStream domain：

```text
JetStream using domains: local "", remote "hub"
```

## 安全注意事项

当前示例使用明文用户名密码：

```text
leaf / change-me-leaf-password
```

正式部署必须至少完成：

- 修改 leafnode 密码。
- 限制云端 `7422/TCP` 访问来源。
- 不向公网裸露 `8222/TCP` 监控端口。
- 后续补 TLS 或 NKey。
