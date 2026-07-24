# Kubernetes Demo（编排器资源分配 + Pod 间 NATS 通信）

## 1. 功能说明
当前工程已支持：
- 资源分配在编排器创建容器时完成：`cpu/memory/gpu` 写入 Pod/Deployment spec
- 扩容由编排器修改 `replicas` 和资源 spec 完成，例如 `1Gi -> 2Gi`
- 通信路径支持：
  - Pod 间（已实现）
  - 不同 Node 间（通过 Service + 调度策略）
  - 不同集群间（通过 NATS Gateway 示例清单）
- 提供容器化应用可复用的 NATS 通信 API：`runtime_api.NatsComm`
- 大帧通过 gRPC streaming 分块传输，NATS 只携带帧引用和工作流控制信息

## 2. 目录说明
- `agent_gRPC/`: gRPC 接口服务，接收请求并通过 `runtime_api.NatsComm` 发布到 NATS
- `agent_b/`: NATS 消费者，处理后回传结果
- `runtime_api/`: 给其他容器化应用复用的 NATS 通信 API
- `examples/`: 外部应用调用 NATS API 的参考示例
- `control_api/`: 早期 HTTP 管理原型，不作为应用间通信推荐路径
- `k8s/`: K8s 清单
  - `agent-grpc-deploy.yaml`
  - `agent-b-deploy.yaml`
  - `agent-grpc-svc.yaml`
  - `nats.yaml`
  - `hpa.yaml`
  - `orchestrated-app-template.yaml`
  - `multicluster/` 跨集群 NATS 示例

## 3. 启动环境

本项目支持三种部署模式，根据你的场景选择一种即可。

---

### 3.1 本地单集群模式（minikube + kubectl apply）

在同一台机器的 minikube 内启动 NATS 和所有服务，适合单机开发测试。

```bash
# 1. 确保已配置 cluster.env（边缘环境变量）
cp -n scripts/local/cluster.env.example scripts/local/cluster.env
# 编辑 LOCAL_CLUSTER、CLUSTER_A_HOST、CLUSTER_B_HOST、CLUSTER_C_HOST、NATS_CLOUD_PASSWORD

# 2. 启动 minikube（使用阿里云镜像加速，避免 registry.k8s.io 拉不到）
minikube start --driver=docker \
  --image-repository=registry.cn-hangzhou.aliyuncs.com/google_containers

# 3. 部署本地 NATS（单节点，无 leafnode）
kubectl apply -f k8s/nats-a.yaml
kubectl rollout status deployment/nats-a --timeout=180s

# 4. 部署应用 Pod
kubectl apply -f k8s/agent-grpc-deploy.yaml
kubectl apply -f k8s/agent-grpc-svc.yaml
kubectl apply -f k8s/agent-b-deploy.yaml
kubectl apply -f k8s/agent-c-deploy.yaml
kubectl apply -f k8s/hpa.yaml
```

验证：

```bash
kubectl get pods -o wide
kubectl get svc
kubectl get hpa
```

---

### 3.2 云端 NATS Hub（minikube cloud profile + Helm）

在独立用户 `k8s_cloud` 下启动一个专用的 minikube 集群，通过 Helm Chart 部署 3 副本 NATS 集群，作为边缘 leafnode 的 Hub。详细文档见 [docs/nats-helm-cloud-edge.md](docs/nats-helm-cloud-edge.md)。

#### 3.2.1 前置条件（首次执行）

```bash
# 创建 k8s_cloud 系统用户，创建项目软链接，复制 helm
sudo bash scripts/bootstrap_k8s_cloud_user.sh
```

#### 3.2.2 配置 cluster.env

```bash
cp -n scripts/local/cluster.env.example scripts/local/cluster.env
```

编辑 `scripts/local/cluster.env`，主要字段：

| 字段 | 说明 |
|---|---|
| `LOCAL_CLUSTER=a` | 当前边缘编号，可为 `a`、`b` 或 `c`；云端默认是集群 A |
| `CLUSTER_A_HOST=<云端IP>` | 云端宿主机 IP（边缘可访问） |
| `CLUSTER_B_HOST=<边缘IP>` | 边缘宿主机 IP |
| `CLUSTER_C_HOST=<边缘IP>` | 第三个边缘宿主机 IP |
| `NATS_CLOUD_PASSWORD=change-me-leaf-password` | leafnode 密码，须与 values 一致 |

#### 3.2.3 启动云端

```bash
# 切换到 k8s_cloud 用户
sudo -iu k8s_cloud

# 执行一键部署脚本
bash ~/Project/K8S_demo/scripts/setup_cloud_minikube_nats_helm.sh
```

该脚本自动完成：

1. `minikube start -p cloud --driver=docker` 启动 `cloud` profile
2. 映射端口：`4222→30422`（client）、`7422→30472`（leafnode）、`8222→30482`（monitor）
3. 创建 `nats-cloud` namespace
4. `helm install nats-hub` 部署 3 副本 NATS 集群（JetStream domain: `hub`）

验证：

```bash
kubectl get pods,svc,pvc -n nats-cloud
```

期望输出：

```
nats-hub-0   3/3   Running
nats-hub-1   3/3   Running
nats-hub-2   3/3   Running
nats-hub-box 1/1   Running
```

云端 Hub 对边缘暴露的 leafnode 地址为：

```
<云端IP>:7422
```

> **注意：** 国内网络拉 `registry.k8s.io` 可能超时，脚本已默认使用阿里云镜像 `registry.cn-hangzhou.aliyuncs.com/google_containers`，可通过环境变量 `MINIKUBE_IMAGE_REPOSITORY` 覆盖。

---

### 3.3 边缘 NATS leafnode（Helm）

在边缘 Kubernetes 集群上部署 NATS leafnode，通过 `7422/TCP` 主动连接云端 Hub。详细文档见 [docs/nats-helm-cloud-edge.md](docs/nats-helm-cloud-edge.md)。

#### 3.3.1 前提

- 当前 kube context 指向边缘集群
- 边缘机器可访问云端 Hub 的 `7422/TCP`
- `scripts/local/cluster.env` 已正确配置

#### 3.3.2 部署

```bash
bash scripts/setup_edge_nats_helm.sh
```

该脚本：

1. 从 `cluster.env` 读取 `LOCAL_CLUSTER`、`NATS_CLOUD_HOST`、`NATS_CLOUD_PASSWORD`
2. 在 `default` namespace 部署 Helm release `nats`（1 副本）
3. 创建 `ConfigMap/edge-cluster-config` 供业务使用
4. 等待 `statefulset/nats` Ready

验证：

```bash
kubectl get pods,svc
kubectl get configmap edge-cluster-config -o yaml
```

期望输出：

```
nats-0    2/2   Running
nats-box  1/1   Running
```

#### 3.3.3 边缘 Agent 连接配置

所有 Agent 连接本地 NATS，通过 leafnode 路由到云端 JetStream domain `hub`：

```
NATS_SERVERS=nats://nats:4222
NATS_JETSTREAM_DOMAIN=hub
CLUSTER_ID=<edge-cluster-id>
```

生成某个集群的 Agent subject 环境变量：

```bash
bash scripts/render_agent_subject_env.sh
```

#### 3.3.4 验证跨集群消息

在 edge-a 订阅：

```bash
kubectl exec -it deploy/nats-box -- \
  nats sub 'workflow.>' --server nats://nats:4222
```

在 edge-b 发布：

```bash
kubectl exec deploy/nats-box -- \
  nats pub workflow.edge-a.test 'hello from edge-b' --server nats://nats:4222
```

如果 edge-a 收到消息，说明两个边缘集群已通过云端 Hub 互通。

---

## 4. 构建镜像
```bash
docker build -f agent_gRPC/Dockerfile -t agent-grpc:v1 .
docker build -f agent_b/Dockerfile -t agent-b-worker:v5 .
docker build -f agent_c/Dockerfile -t agent-c-worker:v3 .
```

如果是 Minikube：
```bash
minikube image load agent-grpc:v1
minikube image load agent-b-worker:v5
minikube image load agent-c-worker:v3
minikube image load nats:2.10
```

## 5. 部署
```bash
kubectl apply -f k8s/nats.yaml
kubectl apply -f k8s/agent-grpc-deploy.yaml
kubectl apply -f k8s/agent-grpc-svc.yaml
kubectl apply -f k8s/agent-b-deploy.yaml
kubectl apply -f k8s/agent-c-deploy.yaml
kubectl apply -f k8s/hpa.yaml
```

查看状态：
```bash
kubectl get pods -o wide
kubectl get svc
kubectl get hpa
```

## 6. 业务调用（gRPC）

当前数据链路：

```text
调用方 --gRPC UploadFrame(1MiB chunks)--> agent-grpc 临时帧存储
调用方 --gRPC Infer(frame_ref)----------> agent-grpc
agent-grpc --NATS 控制消息--------------> Agent B --> Agent C
Agent C --gRPC DownloadFrame------------> agent-grpc
Agent C --NATS 结果----------------------> agent-grpc --> 调用方
```

NATS 控制消息默认限制为 1MiB，避免误把图片、张量或 Base64 数据写入
JetStream。帧默认上限 64MiB，按 1MiB 分块，并使用 SHA-256 做完整性校验。

本机转发后只测试文本：

```bash
kubectl port-forward service/agent-grpc 50051:50051
python client.py
```

传输一帧并执行工作流：

```bash
python client.py --frame /path/to/frame.bin --text "infer this frame"
```

预期输出：

```text
上传帧: id=..., bytes=..., sha256=...
返回结果: Agent B processed with Agent C: Agent C transformed: INFER THIS FRAME (frame_bytes=..., sha256=...)
```

关键配置：

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `FRAME_CHUNK_SIZE` | `1048576` | 单个 gRPC 消息的数据字节数 |
| `FRAME_MAX_BYTES` | `67108864` | 单帧最大大小 |
| `FRAME_STORE_MAX_BYTES` | `536870912` | 网关临时帧总容量 |
| `FRAME_TTL_SECONDS` | `300` | 未消费帧的保留时间 |
| `FRAME_PUBLIC_ADDR` | `agent-grpc:50051` | Agent C 可访问的下载地址 |
| `FRAME_UPLOAD_TARGET` | `agent-grpc:50051` | FrameComm 上传帧的服务地址 |
| `FRAME_ALLOWED_TARGETS` | `agent-grpc:50051` | Agent C 允许访问的帧服务地址 |
| `FRAME_RETRY_ATTEMPTS` | `3` | 上传遇到背压或断连时的尝试次数 |
| `NATS_CONTROL_MAX_BYTES` | `1048576` | NATS 控制消息大小上限 |
| `NATS_MAX_INFLIGHT` | `4` | 每个 worker 的并发消息数 |
| `NATS_ACK_PROGRESS_INTERVAL_SEC` | `10` | 长任务 ACK 进度上报间隔 |

跨 Kubernetes Node 时 Service 地址可以直接使用。跨集群时，
`FRAME_PUBLIC_ADDR` 必须配置为 Agent C 所在网络可访问的地址，并同步加入
`FRAME_ALLOWED_TARGETS`。当前帧存储使用 `emptyDir`，Pod 重启后不保留；
需要断点恢复或回放时应再接入 MinIO。

真实Agent必须按进程复用一个`FrameComm/NatsComm`实例，不能在单帧handler中
重复创建。持久任务等待回复时使用`send_and_wait()`，回复端使用
`publish_core()`。详见
[docs/agent-connection-reuse.md](docs/agent-connection-reuse.md)。

固定速率帧压测：

```bash
PYTHONPATH=.:agent_gRPC python tests/grpc_frame_load.py \
  --target localhost:50051 \
  --size-mib 10 \
  --fps 10 \
  --duration-sec 30
```

压测输出包含成功率、上传延迟、端到端延迟、实际完成 FPS 和 gRPC 错误分类。
跨机器部署和压测步骤见
[docs/frame-transport-handoff.md](docs/frame-transport-handoff.md)。

## 7. 编排器侧资源分配
本仓库对应的编排器链路是：
```text
ResourceConfig(cpu_cores, memory_mb, gpu_count, node_id)
  -> AgentLifecycleManager.deploy_agent()
  -> AgentScheduler.deploy_agent()
  -> Kubernetes Deployment/Pod spec
```

`AgentScheduler` 现在支持两种部署后端：
```text
AGENT_DEPLOY_BACKEND=subprocess   # 默认，本机调试：启动 agent_server.py 子进程
AGENT_DEPLOY_BACKEND=kubernetes   # 真实 K8s：创建 Deployment + Service
```

本机调试模式：
```bash
cd /home/t/Projects/gs/Autonomous-Transportation-System
conda activate k8s
export PYTHONPATH=$PWD
export USE_LLM_SIMULATOR=true
export AGENT_DEPLOY_BACKEND=subprocess
python server.py
```

Kubernetes 部署模式：
```bash
minikube start
kubectl apply -f /home/czl/Project/K8S_demo/k8s/nats-a.yaml

cd /home/t/Projects/czl/Autonomous-Transportation-System
conda activate k8s
export PYTHONPATH=$PWD
export USE_LLM_SIMULATOR=true
export AGENT_DEPLOY_BACKEND=kubernetes
export K8S_NAMESPACE=default
export NATS_DEPLOYMENT_NAME=nats-a
export NATS_SERVICE_NAME=nats-a
export NATS_APP_LABEL=nats-a
export NATS_SERVERS=nats://nats-a:4222
export AGENT_CONTAINER_PORT=8000
export AGENT_ENABLE_HEALTH_PROBE=false
python server.py
```

注意：Kubernetes 模式下，`image_id` 必须是 K8s 能拉取/本地可用的真实镜像名，例如 `my-agent:latest`。如果部署的是 `agent_server.py` 这类 HTTP Agent，可以设置 `AGENT_ENABLE_HEALTH_PROBE=true`；如果部署的是 `agent-b/agent-c` 这种后台 worker，应保持 `false`。

如果使用 Minikube 本地镜像，需要先执行：
```bash
minikube image load my-agent:latest
```

正常启动流程应由编排器 API 发起：
```text
POST /api/apps/install
  -> APPM 安装应用，保存 GuidanceFile

POST /api/apps/{app_id}/start
  body.resource_config 带初始资源
  -> APPM.start(app_id, resource_config)
  -> ALRE.start_app(app_id, resource_config)
  -> ALCM.deploy_agent(resource_config)
  -> ASD.deploy_agent(cpu_cores, memory_mb, gpu_count, node_id)
```

注意：`start` 只负责启动应用实例/容器，不会立即执行 `task_description` 工作流。真正执行工作流应通过应用接口显式触发：
```bash
curl -X POST http://localhost:8000/api/apps/app_xxx/interface \
  -H 'Content-Type: application/json' \
  -d '{"query": "需要应用处理的实际任务"}'
```

启动调用示例：
```bash
curl -X POST http://localhost:8000/api/apps/app_xxx/start \
  -H 'Content-Type: application/json' \
  -d '{
    "resource_config": {
      "cpu_cores": 1.0,
      "memory_mb": 1024,
      "gpu_count": 0,
      "node_id": "localhost"
    }
  }'
```

Kubernetes 模式启动后检查：
```bash
kubectl get deploy,svc,pods
curl http://localhost:8001/api/agents/deployments
```

不想每次手写 HTTP 请求，可以使用封装好的 CLI：
```bash
cd /home/t/K8S_demo
conda activate k8s

# 安装并启动一个 demo 应用
python scripts/orchestrator_cli.py run-demo --cpu 1 --memory-mb 1024 --gpu 0

# 通过编排器安装并启动 K8S_demo 的 agent-b/agent-c worker
python scripts/orchestrator_cli.py k8s-demo --cpu 1 --memory-mb 1024 --gpu 0

# 简写：把第一个参数当作 agents/name，自动安装并启动
python scripts/orchestrator_cli.py agent-b-worker --cpu 1 --memory-mb 1024 --gpu 0

# 查看应用、运行中应用、部署记录
python scripts/orchestrator_cli.py apps
python scripts/orchestrator_cli.py running
python scripts/orchestrator_cli.py deployments

# 如果已经有 app_id，直接启动
python scripts/orchestrator_cli.py start app_xxx --cpu 2 --memory-mb 2048 --gpu 0

# 停止应用
python scripts/orchestrator_cli.py stop app_xxx
```

对应关系：
```text
memory_mb=1024 -> Kubernetes memory: 1Gi
cpu_cores=1.0 -> Kubernetes cpu: "1"
gpu_count=1 -> Kubernetes nvidia.com/gpu: "1"
node_id -> Kubernetes nodeSelector / 调度目标
```

K8s 模板参考：
```bash
cat k8s/orchestrated-app-template.yaml
```

关键字段：
```yaml
env:
  - name: NATS_SERVERS
    value: "nats://nats:4222"
resources:
  requests:
    cpu: "500m"
    memory: "512Mi"
  limits:
    cpu: "1"
    memory: "1Gi"
    nvidia.com/gpu: "0"
```

如果要把内存从 `1Gi` 扩到 `2Gi`，编排器应该更新 Deployment spec：
```bash
kubectl patch deployment agent-grpc --type merge -p '{
  "spec": {
    "replicas": 2,
    "template": {
      "spec": {
        "containers": [{
          "name": "agent-grpc",
          "resources": {
            "requests": {"cpu": "1", "memory": "1Gi"},
            "limits": {"cpu": "2", "memory": "2Gi"}
          }
        }]
      }
    }
  }
}'
```

GPU 前提：节点必须安装 GPU 驱动与 `nvidia-device-plugin`，并且 `nvidia.com/gpu` 大于 0 才会真正占用 GPU。

## 8. 通信能力说明

### 8.1 不同 Node 间
- K8s Service 天生支持跨 Node 访问
- `agent_gRPC/agent-b` 的调度里加了 `podAntiAffinity`，会优先分散到不同 Node
- 查看 Pod 落点：
```bash
kubectl get pods -o wide
```

### 8.2 不同集群间
当前推荐使用 **官方 Helm Chart + 云端 NATS Hub + 边缘 leafnode** 方案。部署文档见：

```text
docs/nats-helm-cloud-edge.md
```

实际 Agent 的 NATS 连接生命周期、环境变量、subject、任务发送、常驻消费和回复
方式见：

```text
docs/agent-nats-usage.md
```

本轮大帧通信排查、已完成修改、验证结果，以及“同集群本地NATS、跨集群云端
NATS”的纯NATS目标方案见：

```text
docs/transport-change-summary.md
```

跨集群不把多台机器 join 成同一个 Kubernetes 集群。每个集群独立调度自己的 Pod，跨集群只打通两条通道：
- 数据/业务消息通道：各集群本地 NATS，业务代码统一通过 `NATS_SERVERS` 访问本集群消息总线
- 编排/控制通道：`K8S-Autonomous` AOE HTTP peer，负责 registry sync 和远端任务分发

这里要明确区分两种 NATS 角色：

- **本地 NATS**：服务当前集群内的 Agent / AOE，重点是稳定、简单、单节点可用
- **跨集群 Gateway NATS**：在本地 NATS 基础上额外打开 `7222/TCP`，让 A / B 两边的 NATS 做 gateway federation

集群内 Pod 只连接本集群 NATS：
```text
集群 A: NATS_SERVERS=nats://nats-a:4222
集群 B: NATS_SERVERS=nats://nats-b:4222
```

约定：

- 集群 A 可以把本地 NATS 部署为 `nats-a`
- 集群 B 可以把本地 NATS 部署为 `nats-b`
- 公共代码、Agent 逻辑、API fallback 不写死 `nats-a` / `nats-b`
- AOE 和 Agent 统一只读取启动环境里的 `NATS_SERVERS`

#### 8.2.1 本地 NATS

部署本地 NATS：
```bash
# 集群 A
kubectl apply -f k8s/nats-a.yaml
kubectl rollout status deployment/nats-a --timeout=180s

# 集群 B
kubectl apply -f k8s/nats-b.yaml
kubectl rollout status deployment/nats-b --timeout=180s
```

如果是当前仓库配套的多集群 AOE 场景，建议分别使用仓库中的启动脚本注入本地 NATS：

```bash
# 集群 A
./scripts/start_cluster_a_aoe.sh

# 集群 B
./scripts/start_cluster_b_aoe.sh
```

它们会分别注入：

```text
集群 A: NATS_SERVERS=nats://nats-a:4222
集群 B: NATS_SERVERS=nats://nats-b:4222
```

本地 NATS 的目标是：

- 让当前集群内 Pod 只依赖本集群的消息总线
- 让代码保持通用，不把 A/B 服务名写死到业务逻辑里
- 出问题时优先排查当前集群，而不是把跨集群网络问题混进来

#### 8.2.2 跨集群 Gateway NATS

只有在你明确需要 **NATS 层跨集群互通** 时，才部署 gateway 版本，而不是默认把所有集群都跑成 gateway 模式。

跨集群 gateway 清单位于：

```text
k8s/multicluster/nats-cluster-a.yaml
k8s/multicluster/nats-cluster-b.yaml
```

这两份文件和本地 NATS 的区别是：

- 保留 `gateway { ... }` 配置
- 额外暴露 `7222/TCP`
- 通过 gateway 名称 `gw-a` / `gw-b` 建立跨集群 NATS federation

在应用前，先把占位符替换成真实宿主机 IP：

```text
__CLUSTER_A_HOST__
__CLUSTER_B_HOST__
```

例如：

```bash
# 集群 A 机器上
# 推荐：从 scripts/local/cluster.env 渲染并 apply
bash scripts/apply_k8s_with_local_cluster.sh

# 或手动 sed（需自行替换 IP）：
sed 's/__CLUSTER_A_HOST__/<cluster-a-ip>/g; s/__CLUSTER_B_HOST__/<cluster-b-ip>/g' \
  k8s/multicluster/nats-cluster-a.yaml | kubectl apply -f -

# 集群 B 机器上
# 推荐：从 scripts/local/cluster.env 渲染并 apply
bash scripts/apply_k8s_with_local_cluster.sh

# 或手动 sed（需自行替换 IP）：
sed 's/__CLUSTER_A_HOST__/<cluster-a-ip>/g; s/__CLUSTER_B_HOST__/<cluster-b-ip>/g' \
  k8s/multicluster/nats-cluster-b.yaml | kubectl apply -f -
```

部署完成后，Gateway 侧的约定是：

```text
集群 A gateway name: gw-a
集群 B gateway name: gw-b
gateway 互联端口: 7222/TCP
```

注意：

- 本地 NATS 和 Gateway NATS 不要在同一集群里同时部署成两个同名 Deployment
- 如果你当前只需要 AOE 的跨集群调度，不需要 NATS 层互通，就不要启用 gateway 版本
- AOE HTTP 调度和 NATS gateway 是两条独立链路，不要混为一谈

AOE HTTP 端口需要双向互通，默认示例为 `8001/TCP`。

启动集群 A AOE：
```bash
cd /home/t/Projects/czl/Autonomous-Transportation-System
conda activate k8s
CLUSTER_A_HOST_IP=10.112.136.44 \
CLUSTER_B_AOE_URL=http://10.112.221.121:8001 \
PYTHON=/home/t/anaconda3/envs/k8s/bin/python \
./scripts/start_cluster_a_aoe.sh
```

集群 B 对应启动脚本是 `scripts/start_cluster_b_aoe.sh`，需要把 `CLUSTER_A_AOE_URL` 指向 A 的 AOE：
```bash
CLUSTER_A_AOE_URL=http://10.112.136.44:8001 ./scripts/start_cluster_b_aoe.sh
```

#### 8.2.3 两边连通性的完整验证流程

建议按下面顺序验证，不要一上来就直接看业务 Pod。这样可以把问题定位在“本地 NATS”、“Gateway NATS”还是“AOE HTTP”。

**Step 1：确认两边本地 NATS 都已经起来**

集群 A：

```bash
kubectl get deploy,svc,pods | grep nats-a
```

集群 B：

```bash
kubectl get deploy,svc,pods | grep nats-b
```

预期：

- `deployment/nats-a` 或 `deployment/nats-b` 为 `1/1`
- 对应 Pod 为 `Running`
- 对应 Service 至少暴露 `4222/TCP`

**Step 2：确认业务 Pod 使用的是本集群本地 NATS**

集群 A：

```bash
kubectl get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{range .env[*]}{.name}={.value}{" "}{end}{end}{"\n"}{end}' | grep NATS_SERVERS
```

集群 B 同样执行一次。

预期：

```text
集群 A: NATS_SERVERS=nats://nats-a:4222
集群 B: NATS_SERVERS=nats://nats-b:4222
```

如果这里就不对，先不要继续往下查 gateway。

**Step 3：如果启用了 Gateway NATS，确认 7222 端口两边都能到**

集群 A 机器上：

```bash
nc -vz 10.112.221.121 7222
```

集群 B 机器上：

```bash
nc -vz 10.112.136.44 7222
```

如果机器上没有 `nc`，可以用：

```bash
curl --connect-timeout 3 telnet://10.112.221.121:7222
```

这一步不要求返回业务数据，只要端口能连通即可。

**Step 4：确认 Gateway NATS 自己已经建立连接**

集群 A：

```bash
kubectl logs deploy/nats-a --tail=120
```

集群 B：

```bash
kubectl logs deploy/nats-b --tail=120
```

预期日志里应看到类似：

```text
gateway connected
outbound gateway connection established
```

如果没有看到这类日志，重点检查：

- `__CLUSTER_A_HOST__` / `__CLUSTER_B_HOST__` 是否替换成了真实宿主机 IP
- `7222/TCP` 是否被防火墙或安全组拦截
- `gw-a` / `gw-b` 名称是否和对端配置一致

**Step 5：确认两边 AOE HTTP 可以互相访问**

集群 A 机器上：

```bash
curl --noproxy '*' http://10.112.221.121:8001/docs
```

集群 B 机器上：

```bash
curl --noproxy '*' http://10.112.136.44:8001/docs
```

预期返回 Swagger/OpenAPI 页面内容。

如果这里不通，说明问题在 AOE HTTP 链路，不在 NATS。

**Step 6：验证 AOE 注册表同步**

在集群 A 机器上执行：

```bash
curl --noproxy '*' -X POST http://10.112.221.121:8001/registry/sync \
  -H 'Content-Type: application/json' \
  -d '{"source_url":"http://10.112.136.44:8001","agents":[]}'
```

然后在两边看日志：

```bash
tail -f logs/cluster-a-aoe.log
tail -f logs/cluster-b-aoe.log
```

预期看到：

```text
[ARDC Gossip] 推送到 ... 成功
```

**Step 7：最后再看业务 Pod**

如果前面 1 到 6 都通过，再检查业务 Pod：

```bash
kubectl get pods -o wide
kubectl describe pod <pod-name>
kubectl logs <pod-name>
```

这时如果业务 Pod 仍有问题，通常就是：

- 业务镜像没有加载到当前集群
- Pod 内业务代码报错
- 就绪探针 / 健康检查配置不匹配

排障速记：

- 本地 NATS 问题：先看 `NATS_SERVERS` 注入值和 `nats-a` / `nats-b` Pod 状态
- Gateway NATS 问题：先看 `7222/TCP` 连通性和 gateway 日志
- AOE HTTP 问题：先看 `8001/TCP`、`PEER_AOE_URLS` 和代理绕过配置
- 业务 Pod 问题：最后看镜像、容器日志和探针

注意：`node_id` 只会变成当前 kubeconfig 指向集群内的 `nodeSelector`，不能用它表达另一个 Kubernetes 集群。需要远端能力时，本地 AOE 通过 `/orchestration/dispatch` 把子任务发给远端 AOE。

### 8.3 K8s 就绪探针
编排器仓库的 `agent_server.py` 已支持在设置 `NATS_SERVERS` 时检查 NATS 连接状态。K8s 可以用 readiness probe 感知真实通信依赖：
```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 5
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 20
  periodSeconds: 10
```

## 9. 容器化应用可复用的 NATS API
其他应用不需要关心 NATS 底层连接、JetStream、ack、consumer 等细节，只需要在容器内调用 `runtime_api.NatsComm`。

通信实例必须在进程启动时创建并在所有请求间复用；每个进程一个实例。不要在
HTTP/gRPC handler或单帧推理函数中重复创建。多进程服务每个worker各自建立一条
连接。

接入方式：
- 把 `runtime_api/` 放进应用镜像
- 安装依赖 `nats-py`
- 编排器注入 `NATS_SERVERS`

编排器需要给应用容器注入：
```yaml
env:
  - name: NATS_SERVERS
    value: "nats://<local-nats-service>:4222"
```

### 9.1 发送消息
```python
import asyncio
from runtime_api import NatsComm

async def main():
    comm = NatsComm()
    try:
        await comm.send(
            subject="workflow.demo.agent.b.in",
            payload={
                "workflow_id": "external-app-1",
                "text": "hello from another container",
            },
        )
    finally:
        await comm.close()

asyncio.run(main())
```

参考文件：
```bash
python examples/external_app_send.py
```

### 9.2 接收消息
`receive()` 默认不会自动 ack。业务处理成功后调用 `message.ack()`，失败时可调用 `message.nak()` 让 NATS 重新投递。

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
            print(message.payload)
            await message.ack()
    finally:
        await comm.close()

asyncio.run(main())
```

参考文件：
```bash
python examples/external_app_receive.py
```

### 9.3 请求-响应
`request()` 使用 Core NATS 原生 request/reply，不会创建 JetStream durable consumer，适合瞬时查询类调用。被调用方需要用 `respond()` 订阅同一个 subject 并返回结果。

Responder 示例：
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

Requester 示例：
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
        print(reply)
    finally:
        await comm.close()

asyncio.run(main())
```

参考文件：
```bash
python examples/external_app_responder.py
python examples/external_app_request.py
```

## 10. 常用排查
如果当前 shell 设置了 `HTTP_PROXY/HTTPS_PROXY`，访问 Minikube apiserver 需要绕过代理：

```bash
export NO_PROXY="$(minikube ip),192.168.49.2,localhost,127.0.0.1"
```

```bash
kubectl logs deployment/agent-grpc -f
kubectl logs deployment/agent-b
kubectl logs deployment/agent-c
kubectl describe pod <pod-name>
```

如果是通过编排器动态启动，Deployment 名可能来自 agent_id，例如旧配置可能是 `agent-a-agent`，新配置通常会变成 `agent-grpc` 或以 `agent-grpc` 开头的安全 K8s 名。可以先用下面命令确认真实名字：

```bash
kubectl get deploy
kubectl logs deployment/<真实 deployment 名> -f
```

`agent_gRPC` 只有收到 gRPC 请求后才会打印 `received gRPC request`、`sending request`、`received reply payload` 这一类消息。如果只启动 Pod 而没有运行 `python client.py` 或其他 gRPC 调用，日志里通常只会看到启动和 subject 配置。
