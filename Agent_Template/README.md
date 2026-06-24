# Agent Template

一个基于 **FastAPI + a2a-python + NATS** 的智能体模板项目。A2A 负责标准化调用入口，NATS 负责大数据或中间结果传输，适合快速构建可接入多智能体工作流的独立 Agent 服务。

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
                       ├─ 从 message 文本 JSON 的 metadata 或环境变量获取 NATS 主题
                       │
                       ├─ agent_function()
                       │   ├─ 从 NATS_IN_SUBJECT 拉取上游数据
                       │   ├─ decode_structured_numpy() 还原 numpy 数组
                       │   ├─ 执行业务逻辑 / 模型推理
                       │   ├─ encode_structured_numpy() 编码结果
                       │   └─ 发布到 NATS_OUT_SUBJECT
                       │
                       └─ 通过 A2A Task artifact 返回执行状态
```

## 技术栈

| 组件 | 技术 | 说明 |
| --- | --- | --- |
| A2A 协议 | a2a-python / a2a-sdk | Agent Card、JSON-RPC、Task/Artifact 生命周期 |
| Web 框架 | FastAPI + Uvicorn | 提供 ASGI 服务 |
| 消息中间件 | NATS JetStream | Agent 间异步数据传输 |
| 数据序列化 | JSON + Base64 | 原生类型 JSON 编码，numpy 数组 base64 编码 |
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
│   └── numpy_utils.py
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

调用方应使用 a2a-python 客户端发送标准 text message。普通文本会触发 Agent 并使用默认 NATS subject：

```text
Process the input data
```

如需覆盖 NATS subject，可发送 JSON 文本：

```json
{
  "task_description": "Process the input data",
  "metadata": {
    "nats_in_subject": "workflow.vision.input",
    "nats_in_durable": "workflow-vision-input",
    "nats_out_subject": "workflow.vision.output"
  }
}
```

Agent 执行成功后会通过 A2A Task artifact 返回类似：

```json
{"status": "success"}
```

## 环境变量

| 变量名 | 说明 | 默认值 |
| --- | --- | --- |
| `A2A_AGENT_URL` | Agent Card 中声明的服务地址 | `http://localhost:9001` |
| `NATS_SERVER_URL` | NATS 服务器地址 | `nats://nats:4222` |
| `NATS_IN_SUBJECT` | 输入主题 | `workflow.previousagent.result` |
| `NATS_IN_DURABLE` | 输入持久化消费者名称 | `workflow-previousagent-result` |
| `NATS_OUT_SUBJECT` | 输出主题 | `workflow.agenttemplate.result` |

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
docker build -t agent-template:0.1.1 .
docker run -p 9001:9001 agent-template:0.1.1
```

### Kubernetes (Minikube)

```bash
minikube image load agent-template:0.1.1
kubectl apply -f k8s/agent-template.yaml
```

Service 类型为 `NodePort`，可通过以下地址访问标准 A2A 服务：

```text
http://localhost:30091/.well-known/agent-card.json
http://localhost:30091/
```
