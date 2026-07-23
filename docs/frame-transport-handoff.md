# FrameComm 跨机器部署与压测交接

## 1. 数据链路

```text
发送智能体 --gRPC UploadFrame--> agent-grpc 临时帧存储
发送智能体 --NATS frame_ref----> 云端 NATS Hub -> 接收智能体
接收智能体 --gRPC DownloadFrame-> agent-grpc
接收智能体 --NATS result-------> 云端 NATS Hub -> 请求方
```

大帧不经过 NATS。NATS 只传 `frame_ref`、任务字段和结果字段。

## 2. 两台机器必须配置的地址

帧服务所在机器：

```text
FRAME_PUBLIC_ADDR=<帧服务机器可直连IP或域名>:30051
```

接收智能体所在机器：

```text
FRAME_ALLOWED_TARGETS=<帧服务机器可直连IP或域名>:30051
```

发送智能体直接使用 `FrameComm.send()` 时还需要：

```text
FRAME_UPLOAD_TARGET=<帧服务机器可直连IP或域名>:30051
```

必须确认接收智能体能够访问该地址：

```bash
nc -vz <帧服务机器IP> 30051
```

`agent-grpc:50051` 只适合同一个 Kubernetes 集群，不能作为跨集群地址。

## 3. 智能体代码迁移

普通消息保持不变：

```python
comm = FrameComm()
await comm.send("workflow.demo.task", {"task_id": "task-1"})
```

发送大帧：

```python
await comm.send(
    "workflow.demo.task",
    {"task_id": "task-1"},
    frame_path="/data/frame.bin",
)
```

消费大帧：

```python
async def handler(data):
    model.infer(data["frame_path"])

await comm.serve(
    subject="workflow.demo.task",
    durable="model-worker",
    handler=handler,
    download_frames=True,
    delete_remote_frame=True,
)
```

`frame_path` 在 handler 返回后自动删除，不能交给后台任务继续使用。

## 4. 构建与部署

```bash
docker build -f agent_gRPC/Dockerfile -t agent-grpc:v1 .
docker build -f agent_b/Dockerfile -t agent-b-worker:v5 .
docker build -f agent_c/Dockerfile -t agent-c-worker:v3 .

minikube image load agent-grpc:v1
minikube image load agent-b-worker:v5
minikube image load agent-c-worker:v3

kubectl apply -f k8s/agent-grpc-deploy.yaml
kubectl apply -f k8s/agent-grpc-svc.yaml
kubectl apply -f k8s/agent-b-deploy.yaml
kubectl apply -f k8s/agent-c-deploy.yaml
kubectl rollout restart deployment/agent-grpc deployment/agent-b deployment/agent-c
kubectl rollout status deployment/agent-grpc --timeout=180s
kubectl rollout status deployment/agent-b --timeout=180s
kubectl rollout status deployment/agent-c --timeout=180s
```

应用清单前，先把 `FRAME_PUBLIC_ADDR` 和 `FRAME_ALLOWED_TARGETS` 替换成实际可达地址。

## 5. 压测

先做单帧端到端验证：

```bash
PYTHONPATH=.:agent_gRPC python tests/grpc_frame_smoke.py \
  --target <帧服务机器IP>:30051 \
  --size-mib 10 \
  --infer
```

再运行 30 秒基线压测：

```bash
PYTHONPATH=.:agent_gRPC python tests/grpc_frame_load.py \
  --target <帧服务机器IP>:30051 \
  --size-mib 10 \
  --fps 10 \
  --duration-sec 30
```

30 秒全部通过后，再运行 5 分钟稳定性压测：

```bash
PYTHONPATH=.:agent_gRPC python tests/grpc_frame_load.py \
  --target <帧服务机器IP>:30051 \
  --size-mib 10 \
  --fps 10 \
  --duration-sec 300
```

同时采集：

```bash
kubectl top pods
kubectl logs deployment/agent-grpc --since=10m
kubectl logs deployment/agent-b --since=10m
kubectl logs deployment/agent-c --since=10m
```

验收条件：

- `success` 等于 `requested`
- `errors={}`
- `completed_fps >= 9.5`
- P95 端到端延迟不持续增长
- 没有 `RESOURCE_EXHAUSTED`、`DEADLINE_EXCEEDED`、NATS timeout
- 网关和 Agent C 的临时目录不会持续增长

## 6. 给新机器 Codex 的任务描述

```text
请在当前 K8S_demo 仓库做跨机器 FrameComm 验证，不要先修改传输参数。

背景：
1. 所有智能体连接同一个云端 NATS，NATS 只负责控制消息和 frame_ref。
2. 10MiB 帧通过 gRPC FrameTransport 点对点传输，不进入 NATS。
3. 帧服务地址是 <SOURCE_IP>:30051。
4. 新机器上的 Agent C 必须把 FRAME_ALLOWED_TARGETS 配置为 <SOURCE_IP>:30051。
5. 如果新机器也作为帧发送方，把 FRAME_UPLOAD_TARGET 配置为 <SOURCE_IP>:30051。

请按 docs/frame-transport-handoff.md 执行：
1. 检查代码、环境变量、NATS subject 和 30051 端口连通性。
2. 构建并部署最新 agent-grpc、agent-b、agent-c 镜像。
3. 先运行 10MiB 单帧 smoke test。
4. 再运行 10MiB、10 FPS、30 秒压力测试。
5. 30 秒全通过后运行 5 分钟压力测试。
6. 同时采集 kubectl top、三个 Agent 日志、成功率、P50/P95/P99 和错误分类。
7. 先报告原始结果和瓶颈证据，不要在基线测试前擅自调大超时、并发或缓存。
```
