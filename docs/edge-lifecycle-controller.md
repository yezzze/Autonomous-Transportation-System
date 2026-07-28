# 边缘实例生命周期控制器

> 兼容组件：当前主流程由编排器直接管理 Pod，Agent 的 `NatsComm` 自主管理
> `WF_<pod-uid>` 和 `FRAME_<pod-uid>`。新部署不需要启动本控制器。本文件仅
> 用于仍依赖旧 HTTP 生命周期 API 的环境。

## 1. 作用

控制器部署在每个边缘 Kubernetes 集群内，外部编排器不需要持有该集群的
kubeconfig，也不需要直接连接边缘 NATS。

```text
外部编排器
  -> HTTP Bearer Token
  -> edge-lifecycle-controller:30080
       -> Kubernetes API：创建、查询、删除 Agent Pod
       -> 本地 NATS：管理 WF_<pod-uid> 和 FRAME_<pod-uid>
```

一次创建请求对应一个直接管理的 Pod，以及可独立启用的工作流和帧 Stream。

## 2. 部署

先完成边缘 NATS Helm 部署，确保 `edge-cluster-config` 已存在。

构建镜像：

```bash
cd /home/czl/Project/K8S_demo
docker build \
  -f control_api/Dockerfile \
  -t k8s-demo-control-api:v2 \
  .
```

Minikube 使用本地镜像时：

```bash
minikube image load k8s-demo-control-api:v2
```

生成并保存访问令牌：

```bash
export CONTROL_API_TOKEN="$(openssl rand -hex 32)"
export CONTROL_API_IMAGE="k8s-demo-control-api:v2"
./scripts/setup_edge_controller.sh
```

控制器默认部署在 `default` namespace，NodePort 为 `30080`。

外部编排器配置：

```bash
export EDGE_CONTROLLER_URL="http://<edge-node-ip>:30080"
export EDGE_CONTROLLER_TOKEN="<CONTROL_API_TOKEN>"
```

生产环境应通过防火墙、专用网络或 TLS Ingress 限制 `30080`，不能只依赖
Bearer Token 暴露到公网。

## 3. 创建实例

HTTP：

```http
POST /v1/instances
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "name": "detector-01",
  "namespace": "default",
  "agent_id": "detector",
  "image": "registry.example.com/detector:v3",
  "env": {
    "MODEL_PATH": "/models/v3"
  },
  "node_selector": {
    "accelerator": "nvidia"
  },
  "resources": {
    "requests": {
      "cpu": "1",
      "memory": "2Gi",
      "nvidia.com/gpu": "1"
    },
    "limits": {
      "cpu": "2",
      "memory": "4Gi",
      "nvidia.com/gpu": "1"
    }
  },
  "workflow_stream": true,
  "frame_stream": true,
  "wait_ready_timeout_sec": 120
}
```

控制器按顺序执行：

1. 创建 Pod。
2. 读取 Kubernetes 返回的 Pod UID。
3. 创建 File 类型 `WF_<pod-uid>`。
4. 创建 Memory 类型 `FRAME_<pod-uid>`。
5. 返回 Pod、两条 Stream 和健康状态。
6. 任一 Stream 创建失败时回滚已创建的资源和本次新建的 Pod。

响应中的路由身份：

```json
{
  "cluster_id": "edge-a",
  "agent_id": "detector",
  "instance_id": "d22fda58-7f47-4a0c-9d44-54d079514bd7",
  "stream": {
    "stream": "WF_d22fda58-7f47-4a0c-9d44-54d079514bd7",
    "domain": "edge-a"
  },
  "frame_stream": {
    "stream": "FRAME_d22fda58-7f47-4a0c-9d44-54d079514bd7",
    "domain": "edge-a",
    "storage": "memory"
  }
}
```

编排器必须把这三个字段写入实例路由表。

命令行：

```bash
python scripts/edge_controller_cli.py create \
  --name detector-01 \
  --agent-id detector \
  --image registry.example.com/detector:v3 \
  --cpu-request 1 \
  --memory-request 2Gi \
  --gpu 1 \
  --wait-ready-timeout-sec 120
```

相同 `name + agent_id + image + workflow_stream + frame_stream` 的重复创建会
返回同一个 Pod UID，并幂等校正两条 Stream。参数不一致时返回 `409`。

如果帧 Stream 由 Agent 的 `NatsComm` 自主管理，创建请求设置
`"frame_stream": false`。此时控制器只管理 `WF_<pod-uid>`；Agent 默认在
`serve_memory_frames()` 启动时创建 `FRAME_<pod-uid>`，并在
`NatsComm.close()` 时删除。控制器管理模式保持 `"frame_stream": true`，
同时 Agent 必须传入 `manage_stream_lifecycle=False`。

## 4. 查询状态

查询单实例：

```bash
python scripts/edge_controller_cli.py get detector-01
```

返回：

- Pod phase、Ready、Pod IP、节点、容器重启次数。
- `healthy`、`starting`、`degraded`、`failed` 或 `terminating`。
- Stream 是否存在、消息数、字节数。
- Consumer 的 pending、ack pending 和重投数量。

列出实例：

```bash
python scripts/edge_controller_cli.py list
```

集群健康：

```bash
python scripts/edge_controller_cli.py health
python scripts/edge_controller_cli.py resources
```

探针：

```text
GET /healthz   只检查控制器进程存活
GET /readyz    检查 Kubernetes API 和本地 JetStream domain
```

## 5. 删除实例

编排器必须先从路由表移除实例，停止新任务进入，然后调用：

```bash
python scripts/edge_controller_cli.py delete detector-01 \
  --drain-timeout-sec 60
```

控制器等待两条 Stream 中的消息数都变为 0。超时后仍有消息时返回 `409`，
Pod 和 Stream 都保留。

明确接受消息丢失时才使用：

```bash
python scripts/edge_controller_cli.py delete detector-01 \
  --drain-timeout-sec 0 \
  --force
```

强制删除响应包含 `dropped_messages`。控制器先请求删除 Pod，再删除实例
两条 Stream；Stream 删除失败时会留下可回收的孤儿 Stream，不会重新创建 Pod。

## 6. 后台校正

控制器默认每 30 秒执行一次：

1. 列出由控制器管理的 Pod。
2. 为存活 Pod 幂等校正工作流和 Memory 帧 Stream。
3. 找出没有对应 Pod 的 `WF_<uid>` 和 `FRAME_<uid>`。
4. 只自动删除超过保护期且消息数为 0 的孤儿 Stream。
5. 非空孤儿 Stream 保留，并在健康信息中报告。

默认保护期为 300 秒：

```text
CONTROLLER_RECONCILE_INTERVAL_SEC=30
CONTROLLER_ORPHAN_GRACE_SEC=300
CONTROLLER_DELETE_EMPTY_ORPHAN_STREAMS=true
```

手动触发：

```bash
python scripts/edge_controller_cli.py reconcile
```

## 7. 多 Agent 编排

存在调用依赖时，按依赖反向创建：

```text
1. 创建 Agent C
2. 得到 C 的 instance_id
3. 创建 Agent B，并注入 TARGET_C_CLUSTER_ID、TARGET_C_AGENT_ID、
   TARGET_C_INSTANCE_ID
4. 得到 B 的 instance_id
5. 创建入口 Agent，并注入 B 的目标实例信息
```

销毁时按调用方向执行：

```text
入口 Agent -> Agent B -> Agent C
```

每一步都先从工作流路由表移除实例，再请求控制器排空和删除。

## 8. API 列表

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/v1/instances` | 创建 Pod 和实例 Stream |
| `GET` | `/v1/instances` | 列出控制器管理的实例 |
| `GET` | `/v1/instances/{namespace}/{name}` | 查询 Pod、Stream 和健康状态 |
| `DELETE` | `/v1/instances/{namespace}/{name}` | 排空并删除实例 |
| `GET` | `/v1/cluster/health` | 查询 Kubernetes、NATS 和 reconcile 状态 |
| `GET` | `/v1/nodes/resources` | 查询节点可分配资源 |
| `POST` | `/v1/reconcile` | 手动执行一次校正 |

除 `/healthz` 和 `/readyz` 外，所有接口都要求 Bearer Token。

## 9. Stream 生命周期集成测试

启动仓库测试拓扑后，通过真实控制器 HTTP 路由和真实 JetStream 验证：

```bash
conda activate k8s
docker compose -f tests/nats-topology-compose.yaml up -d --wait
python tests/edge_controller_stream_integration.py
docker compose -f tests/nats-topology-compose.yaml down -v
```

测试会验证创建 `WF_<uid>` 和 `FRAME_<uid>`、重复创建幂等复用、GET 状态查询、
DELETE 删除 Pod 和两条 Stream，以及删除后 Stream 确实不存在。
