# roadtokencompression-agent 路端视觉 Token 压缩 Agent

基于上游 `agents_source_code/Agent_Template` 改造，A2A 调用入口，NATS 接收车端帧、下发 Token 到云端。

## A2A 调用

```json
{
  "task_description": "对上游帧执行视觉编码与Token压缩",
  "parameters": {
    "source_cluster": "edge-c",
    "target_cluster": "edge-c",
    "target_agent_id": "cloudvlmacccidentdescription-agent",
    "target_instance_id": "cloud-agent-1"
  },
  "metadata": {}
}
```

输入数据来自 NATS 输入主题，输出 Token 载荷发布到 NATS 输出主题。

## 构建

```bash
docker build \
  --build-context weights=/data/gaoshuo/fjh/agent_packages_release/weights \
  -t roadtokencompression-agent:1.0.0 \
  .
```

接口：`/.well-known/agent-card.json`、`POST /`、`/metrics/`。
