# NATS Object Store 大帧测试

## 1. 结论

JetStream 普通消息不会自动分块。一条 10MiB 消息仍然是一条 NATS 消息，
必须同时满足沿途所有 Server 和客户端的 payload、pending bytes 和超时配置。

Object Store 是 JetStream 上的客户端抽象，会把对象切成多条 chunk 消息，
写入完成后发布对象元数据，并在下载时校验 SHA-256。它适合以下场景：

- 帧必须持久化，接收端短暂离线后仍要取回。
- 可以接受数百毫秒的数据面延迟。
- 希望只部署 NATS，不再单独部署 gRPC 或 MinIO。

它不适合当前 10MiB 帧、逐帧等待回复、目标 10 FPS 的实时推理链路。

## 2. 推荐数据链路

实时模式保持：

```text
10MiB frame bytes
  -> Core NATS frame.local/global.* 或点对点 gRPC
  -> 接收端推理
  -> Core NATS reply inbox
```

需要可靠暂存时使用混合模式：

```text
10MiB frame bytes
  -> 目标 edge domain 的 Object Store bucket
  -> 得到 bucket + object_name + size + digest
  -> 每实例 workflow Stream 只发送 frame_ref 和任务字段
  -> 接收端 get(frame_ref)
  -> 推理成功后 delete(object_name)
  -> 回复结果
```

Object Store bucket 建议按 Agent 或固定分片创建，不要每帧创建，也不必跟随
每个 Pod 创建：

```text
Bucket: FRAME_detector
Object: <instance-id>/<workflow-id>/<frame-id>
```

建议配置：

```text
storage=file
replicas=1                   # 单节点边缘；三节点集群可设 3
ttl=60s~300s
max_bytes=按磁盘容量设置
max_chunk_size=1MiB
```

Object Store 不是外部对象存储，bucket 的全部内容必须能放入目标 JetStream
节点磁盘。JetStream 存储应使用本地 SSD，不应使用共享 NFS/NAS。

## 3. 实测方法

测试拓扑：

```text
edge-a -> cloud-hub -> edge-b
```

启动：

```bash
docker compose -f tests/nats-topology-compose.yaml up -d
```

本地 domain：

```bash
conda run -n k8s python tests/nats_object_store_load.py \
  --target-domain edge-a \
  --producer-servers nats://127.0.0.1:24223 \
  --consumer-servers nats://127.0.0.1:24223 \
  --size-mib 10 \
  --chunk-kib 1024 \
  --fps 10 \
  --duration-sec 20
```

跨集群写入 edge-b domain：

```bash
conda run -n k8s python tests/nats_object_store_load.py \
  --target-domain edge-b \
  --producer-servers nats://127.0.0.1:24223 \
  --consumer-servers nats://127.0.0.1:24224 \
  --size-mib 10 \
  --chunk-kib 1024 \
  --fps 10 \
  --duration-sec 20
```

## 4. 本机结果

环境为同一台机器上的三个 NATS 2.10.29 容器，不包含真实公网延迟。

| 路径 | 分块 | 完成帧 | 错误 | FPS | 上传 P50 | 下载 P50 | 往返 P50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| edge-a 本地 | 128KiB | 7/5s | 0 | 1.37 | 77ms | 586ms | 733ms |
| edge-a 本地 | 1MiB | 30/20s | 0 | 1.49 | 49ms | 561ms | 670ms |
| edge-a 到 edge-b | 128KiB | 7/5s | 0 | 1.30 | 128ms | 574ms | 758ms |
| edge-a 到 edge-b | 1MiB | 29/20s | 0 | 1.43 | 82ms | 558ms | 700ms |

跨集群观测结果：

- `OBJ_FRAME_ROUTE_EDGE_B` 只存在于 edge-b JetStream domain。
- cloud-hub 和 edge-a 没有该 Object Store Stream。
- 对象消费并删除后只留下约 500B 删除元数据，TTL 到期后清理。
- 测试期间 NATS 日志没有断连、慢消费者或 JetStream 存储错误。

当前 nats-py Object Store 下载使用 ordered consumer。ordered consumer 会开启
flow control，客户端流控循环检查间隔为 250ms。10MiB 对象会触发多轮等待；
增大 chunk 只能改善上传次数，无法消除下载阶段的主要延迟。

## 5. 选择建议

| 要求 | 传输方式 |
| --- | --- |
| 最低延迟，同集群实时帧 | Core NATS 二进制 |
| 最低延迟，跨机器直连 | gRPC streaming |
| 接收端允许离线，帧不能丢 | Object Store + workflow frame_ref |
| 长期存储、大容量和断点续传 | MinIO + workflow frame_ref |

Object Store 的分块不等于断点续传。当前客户端上传中途失败时会清理本次 chunk，
需要调用方重新上传整个对象。

官方资料：

- <https://docs.nats.io/nats-concepts/jetstream/obj_store>
- <https://docs.nats.io/nats-concepts/jetstream>
- <https://docs.nats.io/running-a-nats-service/configuration>
