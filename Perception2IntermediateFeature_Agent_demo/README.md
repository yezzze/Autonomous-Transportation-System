# Perception2IntermediateFeature Agent Demo

该项目提供一个仅输出型协同感知智能体。智能体通过 A2A JSON-RPC 接收执行请求，从 MCP 服务读取点云，调用 PointPillar Where2comm 模型生成中间特征，再通过 NATS JetStream workflow 路由将结果发布给指定的下游智能体。

## 数据流

```text
A2A 请求
  → MCP 获取点云
  → PointPillar Where2comm 推理
  → NumPy 数据编码
  → NATS workflow 输出
  → A2A Task artifact 返回执行状态和 QoS
```

本智能体不会从 NATS 消费业务输入。点云输入来自 MCP，NATS 仅用于向下游智能体发送推理结果。

## 服务端点

服务默认监听 `9031`：

- `GET /.well-known/agent-card.json`：Agent Card
- `POST /`：A2A JSON-RPC
- `GET /metrics/`：Prometheus 指标

项目不再提供旧版 `GET /model/forward` 接口。

## 环境变量

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `A2A_AGENT_URL` | 否 | `http://localhost:9031` | Agent Card 中公布的 A2A JSON-RPC 地址 |
| `NATS_SERVER_URL` | 否 | `nats://nats:4222` | NATS 服务地址 |
| `CLUSTER_ID` | 是 | 无 | 当前智能体所在的 NATS 集群标识，用于判断 local/global 输出路由 |
| `AGENT_ID` | 部署规范要求 | 无 | 当前智能体的逻辑标识 |
| `AGENT_INSTANCE_ID` | 部署规范要求 | 无 | 当前智能体实例标识；启用实例级 workflow Stream |
| `MCP_SERVER_HOST` | 否 | `127.0.0.1` | 点云 MCP 服务地址 |
| `MCP_SERVER_PORT` | 否 | `8123` | 点云 MCP 服务端口 |
| `MODEL_CHECKPOINT_PATH` | 否 | `checkpoints/point_pillar_where2comm/` | 模型配置和权重目录 |
| `RESOURCE_URI_PREFIX` | 否 | `perception://2021_09_03_09_32_17/302` | MCP 点云资源 URI 前缀 |
| `AGENT_MAX_CONCURRENT_TASKS` | 否 | `1` | 单实例最大并发任务数 |

调用方还必须在每次 A2A 请求的 `parameters` 中提供下游 NATS 路由，详见“A2A 调用参数”。

## 模型文件

默认模型目录为：

```text
checkpoints/point_pillar_where2comm/
├── config.yaml
└── net_epoch50.pth
```

也可以通过 `MODEL_CHECKPOINT_PATH` 指向其他目录。目录中需要包含 `config.yaml` 和对应模型权重。

## 构建镜像

前置条件：

- Docker 24+
- NVIDIA Container Toolkit
- 支持 CUDA 12.8 的 NVIDIA 驱动
- 可访问配置的 Python 软件源和 PyTorch cu128 软件源

构建：

```bash
docker build -t perception2intermediatefeature-agent:0.2.0 .
```

镜像基于 CUDA 12.8，并安装 PyTorch cu128 依赖。

## 运行容器

以下示例假设 NATS 和 MCP 运行在宿主机：

```bash
docker run --rm \
  --gpus all \
  -p 9031:9031 \
  --add-host host.docker.internal:host-gateway \
  -e A2A_AGENT_URL=http://localhost:9031 \
  -e NATS_SERVER_URL=nats://host.docker.internal:4222 \
  -e CLUSTER_ID=edge-c \
  -e AGENT_ID=perception2intermediatefeature-agent \
  -e AGENT_INSTANCE_ID=perception2intermediatefeature-agent-1 \
  -e MCP_SERVER_HOST=host.docker.internal \
  -e MCP_SERVER_PORT=8123 \
  -e MODEL_CHECKPOINT_PATH=/app/checkpoints/point_pillar_where2comm/ \
  --name perception2intermediatefeature-agent \
  perception2intermediatefeature-agent:0.2.0
```

如果模型文件位于镜像外，可以额外挂载模型目录：

```bash
-v "$(pwd)/models/point_pillar_where2comm:/app/checkpoints/point_pillar_where2comm:ro"
```

## A2A 调用参数

推荐使用 `application/json` data Part。为兼容旧调用方，也支持内容为 JSON 对象的 text Part。

请求 payload 示例：

```json
{
  "task_description": "Generate intermediate feature",
  "parameters": {
    "target_cluster": "edge-b",
    "target_agent_id": "downstream-agent",
    "target_instance_id": "downstream-instance",
    "operation": "in"
  },
  "metadata": {}
}
```

输出路由字段：

- `target_cluster`：下游智能体所在集群
- `target_agent_id`：下游智能体逻辑标识
- `target_instance_id`：下游智能体实例标识
- `operation`：可选，workflow 操作名，默认 `in`

前三个字段必须是非空字符串。普通文本请求不携带输出路由，因此会以缺少 NATS 输出参数失败。

A2A artifact 只返回轻量执行结果，例如：

```json
{
  "status": "success",
  "resource_uri": "perception://2021_09_03_09_32_17/302/0"
}
```

完整业务结果通过 NATS 发送，包含：

- `status`
- `resource_uri`
- `intermediate_feature`
- `pcd`

其中 NumPy 数组会编码为带有 `data`、`shape` 和 `dtype` 字段的 JSON 对象。

## Kubernetes 部署

部署清单位于：

```text
k8s/perception2intermediatefeature-agent.yaml
```

部署前应根据实际环境检查：

- 镜像地址和版本
- `A2A_AGENT_URL`
- `NATS_SERVER_URL` 或集群内默认 NATS Service
- `CLUSTER_ID`
- MCP 服务地址
- GPU 资源和节点运行时

`AGENT_ID` 与 `AGENT_INSTANCE_ID` 是实例级 workflow Stream 的规范身份配置；Kubernetes 清单使用 Pod UID 作为实例标识。

## 检查服务

```bash
curl http://127.0.0.1:9031/.well-known/agent-card.json
curl http://127.0.0.1:9031/metrics/
```

服务启动时会连接 NATS 并加载模型。如果 NATS 不可用或模型加载失败，应用会终止启动。

## 常见问题

- `Model config not found`
  - 检查 `MODEL_CHECKPOINT_PATH` 是否指向包含 `config.yaml` 的实际目录。
- 无法连接 NATS
  - 检查 `NATS_SERVER_URL`、网络连通性、JetStream domain 和目标实例 Stream。
- `Missing local environment variables for NATS output: CLUSTER_ID`
  - 设置当前智能体所在集群的 `CLUSTER_ID`。
- `Missing A2A parameters for NATS output`
  - 在 A2A payload 的 `parameters` 中提供三个必需目标路由字段。
- 容器无法访问宿主机 MCP 或 NATS
  - Linux Docker 运行时添加 `--add-host host.docker.internal:host-gateway`。
- CUDA 或扩展加载失败
  - 检查宿主机驱动是否支持 CUDA 12.8，并确认容器已使用 `--gpus all` 启动。
