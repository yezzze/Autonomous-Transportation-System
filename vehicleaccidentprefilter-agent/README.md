# vehicleaccidentprefilter-agent 车端事故初筛 Agent

基于上游 `agents_source_code/Agent_Template` 改造，A2A JSON-RPC 调用入口，NATS 数据下发。

## A2A 调用

```json
{
  "task_description": "对输入视频执行车端事故初筛",
  "parameters": {
    "video_path": "/app/data/green14/vru_over20_dada_2000_vru_10.mp4",
    "sample_id": "sample-001",
    "target_cluster": "edge-c",
    "target_agent_id": "roadtokencompression-agent",
    "target_instance_id": "road-agent-1"
  },
  "metadata": {}
}
```

发送到 `POST /`。车端只做输出型 Agent，不接收上游 NATS 数据。

## 构建

```bash
docker build \
  --build-context weights=/data/gaoshuo/fjh/agent_packages_release/weights \
  -t vehicleaccidentprefilter-agent:1.1.0 \
  .
```

接口：`/.well-known/agent-card.json`、`POST /`、`/metrics/`。
