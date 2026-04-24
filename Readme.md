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

## 3. 构建镜像
```bash
docker build -f agent_gRPC/Dockerfile -t agent-grpc:v1 .
docker build -f agent_b/Dockerfile -t agent-b-worker:v3 .
docker build -f agent_c/Dockerfile -t agent-c-worker:v1 .
```

如果是 Minikube：
```bash
minikube image load agent-grpc:v1
minikube image load agent-b-worker:v3
minikube image load agent-c-worker:v1
minikube image load nats:2.10
```

## 4. 部署
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

## 5. 业务调用（gRPC）
本机转发后测试：
```bash
kubectl port-forward service/agent-grpc 50051:50051
python client.py
```
预期输出：
```text
返回结果: Agent B processed with Agent C: Agent C transformed: HELLO FROM PYTHON CLIENT
```

## 6. 编排器侧资源分配
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
kubectl apply -f /home/t/K8S_demo/k8s/nats.yaml

cd /home/t/Projects/czl/Autonomous-Transportation-System
conda activate k8s
export PYTHONPATH=$PWD
export USE_LLM_SIMULATOR=true
export AGENT_DEPLOY_BACKEND=kubernetes
export K8S_NAMESPACE=default
export NATS_SERVERS=nats://nats:4222
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

## 7. 通信能力说明

### 7.1 不同 Node 间
- K8s Service 天生支持跨 Node 访问
- `agent_gRPC/agent-b` 的调度里加了 `podAntiAffinity`，会优先分散到不同 Node
- 查看 Pod 落点：
```bash
kubectl get pods -o wide
```

### 7.2 不同集群间
- 参考 `k8s/multicluster/nats-cluster-a.yaml` 和 `k8s/multicluster/nats-cluster-b.yaml`
- 核心思路：两边 NATS 用 `gateway` 互联，业务仍走同一套 subject
- 生产环境需要：
  - 跨集群 DNS 或固定可达地址
  - NetworkPolicy/防火墙放行 7222
  - 建议开启 TLS 与鉴权

### 7.3 K8s 就绪探针
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

## 8. 容器化应用可复用的 NATS API
其他应用不需要关心 NATS 底层连接、JetStream、ack、consumer 等细节，只需要在容器内调用 `runtime_api.NatsComm`。

接入方式：
- 把 `runtime_api/` 放进应用镜像
- 安装依赖 `nats-py`
- 编排器注入 `NATS_SERVERS`

编排器需要给应用容器注入：
```yaml
env:
  - name: NATS_SERVERS
    value: "nats://nats:4222"
```

### 8.1 发送消息
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

### 8.2 接收消息
`receive()` 默认不会自动 ack。业务处理成功后调用 `message.ack()`，失败时可调用 `message.nak()` 让 NATS 重新投递。

```python
import asyncio
from runtime_api import NatsComm

async def main():
    comm = NatsComm()
    try:
        messages = await comm.receive(
            subject="workflow.demo.agent.grpc.reply.*",
            durable="external-app-reply-consumer",
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

### 8.3 请求-响应
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

## 9. 常用排查
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
