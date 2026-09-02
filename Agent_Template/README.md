# Agent Template

一个基于 **FastAPI + a2a-python + NATS + Prometheus** 的智能体模板项目。A2A 负责标准化调用入口，NATS 负责大数据或中间结果传输，Prometheus 负责暴露服务端分阶段 QoS 指标。

## 写在最前面

通常只需要改 `fast_api/app.py` 中的两个位置：

- `lifespan()`：加载模型、初始化资源。
- `agent_function()`：实现核心业务逻辑。

Agent 的调用和数据流是分离的：调用通过标准 A2A JSON-RPC 触发，数据通过 NATS 消息队列传输。这是因为图像、特征张量等大数据不适合直接放在 HTTP 请求体中，而 NATS 更适合做异步数据传递。

NATS 的 IN 或 OUT subject 可随项目需要删减。比如不需要从上游 Agent 获取数据时，可以移除输入订阅逻辑；不需要发送给下游 Agent 时，可以移除输出发布逻辑。

## 项目架构

```text
外部调用方 ──A2A JSON-RPC──> POST /
                       │
                       ├─ Agent Card: GET /.well-known/agent-card.json
                       │
                       ├─ AgentTemplateExecutor 解析标准 A2A Message
                       │
                       ├─ 从 message data Part 的 parameters 获取 NATS 路由
                       │
                       ├─ agent_function()
                       │   ├─ 从实例级 local/global subject 拉取上游数据
                       │   ├─ decode_structured_numpy() 还原 numpy 数组
                       │   ├─ 执行业务逻辑 / 模型推理
                       │   ├─ encode_structured_numpy() 编码结果
                       │   └─ 发布到 parameters 指定的目标实例
                       │
                       └─ 通过 A2A Task artifact 返回执行状态和 QoS metadata
```

## 技术栈

| 组件 | 技术 | 说明 |
| --- | --- | --- |
| A2A 协议 | a2a-python / a2a-sdk | Agent Card、JSON-RPC、Task/Artifact 生命周期 |
| Web 框架 | FastAPI + Uvicorn | 提供 ASGI 服务 |
| 消息中间件 | NATS JetStream | Agent 间异步数据传输 |
| 数据序列化 | JSON + Base64 | 原生类型 JSON 编码，numpy 数组 base64 编码 |
| QoS 指标 | prometheus-client | `/metrics/` 暴露分阶段时延和调用计数 |
| 日志 | Python logging | 统一命名空间日志 |

## 项目结构

```text
Agent_Template/
├── main.py
├── fast_api/
│   └── app.py               # FastAPI 应用、A2A Executor、核心业务逻辑
├── protocols/
│   ├── __init__.py
│   └── nats_comm.py         # NATS 通信封装
├── utils/
│   ├── logger_utils.py
│   ├── numpy_utils.py
│   └── prometheus_metrics.py
├── Dockerfile
└── k8s/
    └── agent-template.yaml
```

## A2A 接口

本模板使用 a2a-sdk 1.1 的 `add_a2a_routes_to_fastapi` 将 Agent Card 和 JSON-RPC 路由直接注册到现有 FastAPI 应用中，不再使用自研 `/a2a/execute` 协议。

### Agent Card

```http
GET /.well-known/agent-card.json
```

Agent Card 会声明 Agent 名称、能力、输入输出模式和 JSON-RPC 服务地址。服务地址由 `A2A_AGENT_URL` 环境变量控制，默认是 `http://localhost:9001`。

### JSON-RPC

```http
POST /
```

调用方应使用 a2a-python 客户端发送标准 data Part，结构如下：

```json
{
  "task_description": "Process the input data",
  "parameters": {
    "source_cluster": "edge-a",
    "operation": "in",
    "target_cluster": "edge-b",
    "target_agent_id": "agent-template",
    "target_instance_id": "agent-template-xxxxx"
  },
  "metadata": {
  }
}
```

`task_description`、`parameters` 和 `metadata` 会分别传入 `agent_function()`。

NATS 路由字段必须通过 `parameters` 显式提供：仅输入型 Agent 传入
`source_cluster`；仅输出型 Agent 传入 `target_cluster`、`target_agent_id`
和 `target_instance_id`；同时具有输入和输出时传入两组字段。
`operation` 为可选字段，默认值为 `in`。

Agent 执行成功后会通过 A2A Task artifact 返回类似：

```json
{"status": "success"}
```

QoS 数据会放在标准 A2A artifact 和最终 status update 的 metadata 中：

```json
{
  "qos": {
    "schema_version": "1",
    "agent_id": "agent-template",
    "instance_id": "agent-template-xxxxx",
    "task_id": "sdk-task-id",
    "queue_wait_ms": 12.3,
    "nats_input_wait_ms": 100.2,
    "execution_ms": 820.4,
    "nats_output_publish_ms": 8.1,
    "server_total_ms": 950.7
  }
}
```

## 环境变量

`k8s/agent-template.yaml` 中配置了以下环境变量：

| 变量名 | YAML 示例值 | 说明 |
| --- | --- | --- |
| `A2A_AGENT_URL` | `http://192.168.49.2:30091` | Agent Card 中声明的集群外可访问地址。应根据 Kubernetes 节点 IP 和 Service NodePort 修改。未配置时默认使用 `http://localhost:9001`。 |
| `AGENT_MAX_CONCURRENT_TASKS` | `"1"` | 单个 Agent 实例允许并发执行的任务数量。未配置时默认值为 `1`。 |
| `CLUSTER_ID` | `edge-c` | 当前 Agent 所属的 NATS JetStream domain/集群标识，必须与工作流路由使用的集群名称一致。 |
| `AGENT_ID` | `agent-template` | Agent 的逻辑标识。部署具体智能体时，必须修改为对应的智能体镜像名称；同一种镜像的所有副本应保持一致。 |
| `AGENT_INSTANCE_ID` | Pod 的 `metadata.uid` | Agent 实例的唯一标识。YAML 通过 Kubernetes Downward API 自动注入 Pod UID，无需手工填写。 |

例如镜像为 `perception2intermediatefeature:0.1.0` 时，应将 YAML 中的
`AGENT_ID` 设置为镜像名称（不含仓库地址和版本标签）：

```yaml
- name: AGENT_ID
  value: perception2intermediatefeature
```

同时应同步修改 Deployment/Service 名称、标签、容器名称和 `image` 字段，避免模板名称残留造成路由或部署对象不一致。

## 分阶段时延监控

模板在标准 A2A Executor 中采集服务端真实分段时延，并通过标准 A2A metadata、结构化日志和 `/metrics/` 同时输出。

```text
server_total_ms
├── queue_wait_ms
├── nats_input_wait_ms
├── execution_ms
├── nats_output_publish_ms
└── 解析、编解码等其他服务端开销
```

调用方应使用单调时钟测量完整 RTT，并计算网络残差：

```text
total_latency_ms = 请求发送至响应接收的完整 RTT
network_ms = max(total_latency_ms - server_total_ms, 0)
```

Prometheus 暴露以下低基数指标，标签均为 `agent_id`、`instance_id` 和 `status`：

| 指标 | 含义 |
| --- | --- |
| `agent_calls_total` | Agent 实例处理的 A2A 调用总数 |
| `agent_queue_wait_seconds` | 请求等待执行槽的时间 |
| `agent_nats_input_wait_seconds` | 等待 NATS 输入数据的时间 |
| `agent_execution_seconds` | 核心业务逻辑执行时间 |
| `agent_nats_output_publish_seconds` | 发布 NATS 输出数据的时间 |
| `agent_server_total_seconds` | Agent 服务端总处理时间 |

## 自定义性能指标

业务代码得到模型评估结果后，可通过统一接口上报任意数值型性能指标。例如目标
检测或分割算法在 `agent_function()` 的开发者替换区计算出 mIoU 后：

```python
from utils.prometheus_metrics import observe_performance_metric

miou = evaluator.compute_miou(prediction, ground_truth)
observe_performance_metric("miou", miou)
```

一次产生多个指标时可批量上报：

```python
from utils.prometheus_metrics import observe_performance_metrics

observe_performance_metrics({
    "miou": miou,
    "map_50": map_50,
    "pixel_accuracy": pixel_accuracy,
})
```

`/metrics/` 将暴露：

| 指标 | 含义 |
| --- | --- |
| `agent_performance_count` | 指标累计观测次数 |
| `agent_performance_sum` | 指标累计值，可用 `sum / count` 计算区间平均值 |
| `agent_performance_latest` | 当前实例最近一次观测值 |

这些指标的固定标签为 `agent_id`、`instance_id` 和 `metric_name`。例如查询所有
实例最近 5 分钟的 mIoU 平均值：

```promql
sum(rate(agent_performance_sum{metric_name="miou"}[5m]))
/
sum(rate(agent_performance_count{metric_name="miou"}[5m]))
```

单次调用上报的值不会写入 artifact 的结果 JSON，而是像 QoS 一样通过 artifact
和最终 status update 的 metadata 返回：

```json
{
  "qos": {
    "schema_version": "1"
  },
  "performance": {
    "miou": 0.73,
    "map_50": 0.81
  }
}
```

指标同时写入结构化日志。指标名须匹配
`[a-zA-Z_][a-zA-Z0-9_]{0,63}`，并应使用稳定名称；不要把 task ID、图片名、
时间戳等高基数值作为指标名。

`task_id` 只写入 A2A metadata 与结构化日志，不作为 Prometheus 标签，避免高基数时间序列。安装 kube-prometheus-stack 后，清单中附带的 `ServiceMonitor` 会通过 Service 的 `http` 端口抓取 `/metrics/`。

## 如何定制你的 Agent

### 步骤 1: 加载模型

在 [app.py](fast_api/app.py) 的 `lifespan()` 函数中替换模型加载逻辑：

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    model = MyModel.load("path/to/model")
    logger.info("Model loaded successfully")

    try:
        yield
    finally:
        await _nats_comm.close()
```

### 步骤 2: 编写业务逻辑

在 `agent_function()` 中实现 Agent 核心逻辑：

```python
async def agent_function(...):
    data = await _receive_data_from_nats(...)
    decode_data = decode_structured_numpy(data)

    result_tensor = model.inference(decode_data)

    result = {
        "status": "success",
        "processed_data": encode_structured_numpy(result_tensor),
    }
    await _send_data_to_nats(result, ...)

    return {"status": "success"}
```

## NumPy 数组传输

```text
ndarray -> encode_structured_numpy() -> JSON-friendly dict -> NATS
NATS -> dict -> decode_structured_numpy() -> ndarray
```

## 容器化部署

### Docker

```bash
docker build -t agent-template:0.2.1 .
docker run -p 9001:9001 agent-template:0.2.1
```

### Kubernetes (Minikube)

```bash
minikube image load agent-template:0.2.1
kubectl apply -f k8s/agent-template.yaml
```

Service 类型为 `NodePort`，可通过以下地址访问标准 A2A 服务：

```text
http://localhost:30091/.well-known/agent-card.json
http://localhost:30091/
http://localhost:30091/metrics/
```
