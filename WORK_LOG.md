# 工作日志：agent_gRPC 改名与 runtime_api 接入

日期：2026-04-24

## 已完成

- 将原 `agent_a` 目录收敛为 `agent_gRPC`，静态 K8s 资源名改为 `agent-grpc`。
- 修正 `agent_gRPC/Dockerfile`，改为从 `K8S_demo` 根目录构建，并复制 `runtime_api/` 到镜像内。
- 保留 gRPC 服务入口，内部使用 `runtime_api.NatsComm` 发布和接收 NATS/JetStream 消息。
- 在 `agent_gRPC` 启动日志中补充 `NATS_SERVERS`、请求 subject、回复 subject 前缀，便于 `kubectl logs` 直接确认配置。
- 修正 `agent_b` 的回包逻辑：优先使用请求里的 `reply_subject`，避免 gRPC 入口等待私有回包 subject 时收不到消息。
- 将静态部署清单调整为：
  - `k8s/agent-grpc-deploy.yaml`
  - `k8s/agent-grpc-svc.yaml`
  - Deployment/Service 名：`agent-grpc`
  - 镜像名：`agent-grpc:v1`
- 更新 `client.py`，默认从 `agent_gRPC` 导入 protobuf 代码，并支持 `AGENT_GRPC_ADDR`。
- 为 runtime_api 使用方的 requirements 补充 Python 3.6 所需的 `dataclasses` backport，Python 3.11 容器会自动忽略该依赖。
- 更新编排器 Kubernetes scheduler，使 `agent-grpc` / 兼容旧 `agent-a` 都会按 gRPC 入口处理，端口为 `50051`，Service 类型为 `NodePort`。
- 更新内置应用与 UI 的默认能力名为 `agent-grpc`。
- 更新编排器持久化内容：镜像仓库默认项改为 `agent-grpc:v1`，应用列表中旧 `app_builtin_agent_a` 迁移为 `app_builtin_agent_grpc`，历史 `agent-a` guidance 会在启动时自动改写为 `agent-grpc`。
- 在 `examples/README.md` 新增 runtime_api 使用手册，包含 send/receive/serve/request/respond、subject 通配符语言、部署和日志排查。
- 处理 Minikube 本地镜像与代理问题：`agent-grpc:v1` 需要 `minikube image load`，且设置了 `HTTP_PROXY/HTTPS_PROXY` 时需要为 Minikube IP 设置 `NO_PROXY`。

## 关键问题记录

之前 `kubectl logs deployment/agent-a-agent -f` 看不到业务消息，可能有两类原因：

- 改名后真实 Deployment 名不再是 `agent-a-agent`，需要先 `kubectl get deploy` 确认。
- `agent_gRPC` 只有收到 gRPC 请求后才会打印业务消息；如果只启动 Deployment，没有执行 `client.py` 或其他 gRPC 调用，日志只会有启动信息。

另外发现一个实际链路问题：`agent_gRPC` 已生成私有 `reply_subject`，但 `agent_b` 仍回到固定 `workflow.demo.agent.a.reply`。这会导致 gRPC 入口等不到回包。现在已改为优先回到请求携带的 `reply_subject`。

## 推荐验证命令

```bash
cd /home/t/Projects/czl/K8S_demo
docker build -f agent_gRPC/Dockerfile -t agent-grpc:v1 .
docker build -f agent_b/Dockerfile -t agent-b-worker:v3 .
docker build -f agent_c/Dockerfile -t agent-c-worker:v1 .

kubectl apply -f k8s/nats.yaml
kubectl apply -f k8s/agent-grpc-deploy.yaml
kubectl apply -f k8s/agent-grpc-svc.yaml
kubectl apply -f k8s/agent-b-deploy.yaml
kubectl apply -f k8s/agent-c-deploy.yaml

kubectl logs deployment/agent-grpc -f
AGENT_GRPC_ADDR=<node-ip>:30051 python client.py
```

如果 `kubectl` 报 `TLS handshake timeout`，先执行：

```bash
export NO_PROXY="$(minikube ip),192.168.49.2,localhost,127.0.0.1"
```
