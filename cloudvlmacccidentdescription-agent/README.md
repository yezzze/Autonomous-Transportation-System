# cloudvlmacccidentdescription-agent 云端事故理解 Agent

基于上游 `agents_source_code/Agent_Template` 改造，A2A 调用入口，NATS 接收路端 Token。

## A2A 调用

```json
{
  "task_description": "恢复Token并生成结构化事故描述",
  "parameters": {
    "source_cluster": "edge-c"
  },
  "metadata": {}
}
```

云端是链路终点，只消费 NATS 输入，不发布下游数据；结构化描述通过 A2A artifact 返回。

## 构建

```bash
docker build \
  --build-context weights=/data/gaoshuo/fjh/agent_packages_release/weights \
  -t cloudvlmacccidentdescription-agent:1.1.0 \
  .
```

接口：`/.well-known/agent-card.json`、`POST /`、`/metrics/`。
