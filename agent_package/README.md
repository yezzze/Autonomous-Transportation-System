# Auto-Agent v3 · 智能体生成平台

根据上传的 `agent.md` / `workflow.md` 生成可 Docker 部署的 A2A 协议智能对话代理。

## 生成的 Agent 特性

| 特性 | 说明 |
|---|---|
| **A2A 协议** | 标准 Agent-to-Agent 通信，唯一入口 `POST /a2a/execute` |
| **NATS 数据流** | 通过 NATS JetStream 与上下游 Agent 交换数据（图像/张量等） |
| **LLM 对话** | DeepSeek API，支持 agent.md 角色注入 + workflow.md 工作流 |
| **龙虾记忆** | MEMORY.md 长期记忆 + 每日日志 + TF-IDF/BM25 混合搜索 |
| **多会话** | X-Session-Id 隔离 + token 追踪 + 自动记忆整理 |
| **Docker 部署** | 一键构建运行，端口 9001 |

## 快速开始

### 1. 启动生成平台

```bash
pip install -r requirements.txt
python server.py
# 访问 http://localhost:8080
```

### 2. 生成智能体

1. 打开浏览器访问 http://localhost:8080
2. 上传 `agent.md`（必填）和 `workflow.md`（可选）
3. 点击"生成智能体"
4. 下载 ZIP 文件

### 3. 部署智能体

```bash
unzip agent-xxxxxxxx.zip -d my-agent
cd my-agent
# 设置 DeepSeek API Key
export DEEPSEEK_API_KEY=sk-xxx
docker-compose up -d
# 服务运行在 http://localhost:9001
```

### 4. 测试

```bash
# 健康检查
curl http://localhost:9001/health

# A2A 协议调用
curl -X POST http://localhost:9001/a2a/execute \
  -H "Content-Type: application/json" \
  -d "{\"sender_id\":\"Test\",\"receiver_id\":\"AutoAgent\",\"message_type\":\"request\",\"payload\":{\"task_id\":\"001\",\"task_type\":\"chat\",\"task_description\":\"你好\"}}"

# 传统 /chat 调用
curl -X POST http://localhost:9001/chat \
  -H "Content-Type: application/json" \
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"

# 自动化测试
python test_agent.py --url http://localhost:9001
```

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/a2a/execute` | POST | **A2A 协议** - Agent 间通信唯一入口 |
| `/chat` | POST | 传统对话接口（兼容旧版） |
| `/health` | GET | 健康检查 |
| `/` | GET | 服务信息 |
| `/memory/search` | GET | 搜索记忆 |
| `/memory/entry` | POST | 手动写入记忆 |
| `/memory/files` | GET | 列出记忆文件 |
| `/session` | GET/DELETE | 会话管理 |

## 项目结构

```
auto-agent/
├── server.py              # 生成平台（FastAPI，端口 8080）
├── static/index.html      # 前端页面
├── templates/             # agent.md / workflow.md 示例模板
├── agent_package/         # Agent 模板（被打包为 ZIP）
│   ├── main.py            # 入口（端口 9001）
│   ├── fast_api/app.py    # 核心：A2A + LLM + 记忆
│   ├── protocols/         # A2A 协议 + NATS 通信层
│   ├── utils/             # 日志 + NumPy 编解码
│   ├── memory_store.py    # 记忆文件管理
│   ├── search_engine.py   # 混合搜索引擎
│   ├── session_manager.py # 多会话管理
│   ├── config/            # agent.md / workflow.md
│   ├── workspace/         # 运行时记忆数据
│   ├── Dockerfile
│   └── docker-compose.yml
├── test_agent.py          # 部署后测试工具
└── outputs/               # 生成的 ZIP 文件
```

## 与上游项目的关系

本 Agent 完全兼容 [Autonomous-Transportation-System](https://github.com/yezzze/Autonomous-Transportation-System) 的 Agent 框架：

- **A2A 协议**: 相同的 `A2AMessage` + `A2ATaskRequest/Response` 消息格式
- **NATS 通信**: 相同的 JetStream pull_subscribe/publish 模式
- **任务调度**: L2 Scheduler 通过 `POST /a2a/execute` 调用本 Agent
- **数据管道**: 通过 NATS 主题与上下游 Agent 交换数据

### 接入方式

L2 Scheduler 调用示例：

```json
POST /a2a/execute
{
    "sender_id": "L2_Scheduler",
    "receiver_id": "AutoAgent",
    "message_type": "request",
    "payload": {
        "task_id": "task-001",
        "task_type": "chat",
        "task_description": "分析传感器数据并给出建议",
        "metadata": {
            "nats_in_subject": "workflow.perception.result",
            "nats_out_subject": "workflow.autoagent.result",
            "temperature": 0.3
        }
    }
}
```
