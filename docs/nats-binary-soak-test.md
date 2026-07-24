# NATS 二进制帧 local/global 稳定性测试

## 1. 测试目标

验证同一个 Agent 进程长期复用 NATS 连接时，按以下方式连续传输大帧：

1. 单帧大小 15 MiB。
2. 每次发送后等待处理回复。
3. 收到回复后再发送下一帧。
4. 最大发送速率 10 FPS。
5. local 和 global 各持续 5 分钟。
6. 同时观察 edge-a、cloud-hub、edge-b 的流量、pending、内存和慢消费者。

## 2. 测试拓扑

仓库提供：

```text
tests/nats-topology-compose.yaml
tests/nats_topology/cloud.conf
tests/nats_topology/edge-a.conf
tests/nats_topology/edge-b.conf
tests/nats_topology_soak.py
```

拓扑：

```text
local:
requester -> edge-a -> local responder

global:
requester -> edge-a -> cloud-hub -> edge-b -> global responder
```

三个 NATS Server 均配置：

```text
max_payload=67108864
```

edge-a 和 edge-b 的 LeafNode 均配置：

```text
deny_imports=["frame.local.>"]
deny_exports=["frame.local.>"]
```

## 3. 运行方法

启动临时拓扑：

```bash
docker compose -f tests/nats-topology-compose.yaml up -d
```

确认三台 NATS Ready 且 Hub 有两条 LeafNode 连接：

```bash
docker compose -f tests/nats-topology-compose.yaml ps
curl -sS http://127.0.0.1:28222/leafz
```

在 Conda `k8s`环境运行正式测试：

```bash
conda activate k8s
python tests/nats_topology_soak.py \
  --duration-sec 300 \
  --fps 10 \
  --payload-mib 15 \
  --sample-interval-sec 5 |
  tee /tmp/nats-topology-soak-5min.jsonl
```

结束后关闭临时拓扑：

```bash
docker compose -f tests/nats-topology-compose.yaml down
```

## 4. 正式测试结果

测试日期：2026-07-24。

### 4.1 local

```text
subject=frame.local.edge-a.topology-soak-agent.infer
duration=300.017s
payload=15 MiB
attempted=3000
succeeded=3000
errors=0
actual_fps=9.999
P50=55.906ms
P95=63.071ms
P99=67.341ms
max=88.425ms
```

NATS 流量增量：

| 节点 | in bytes | out bytes |
|---|---:|---:|
| edge-a | 47,185,968,000 | 47,185,968,000 |
| cloud-hub | 0 | 0 |
| edge-b | 0 | 0 |

local 的 15 MiB 帧只经过 edge-a。cloud-hub 和 edge-b 数据字节增量严格为 0，
LeafNode 的 local subject 隔离生效。

### 4.2 global

```text
subject=frame.global.edge-b.topology-soak-agent.infer
duration=300.009s
payload=15 MiB
attempted=2883
succeeded=2883
errors=0
actual_fps=9.610
P50=84.575ms
P95=121.593ms
P99=130.533ms
max=150.556ms
```

NATS 流量增量：

| 节点 | in bytes | out bytes |
|---|---:|---:|
| edge-a | 45,345,715,248 | 45,345,715,248 |
| cloud-hub | 45,345,715,248 | 45,345,715,248 |
| edge-b | 45,345,715,248 | 45,345,715,248 |

三端字节增量完全一致，证明请求数据经过：

```text
edge-a -> cloud-hub -> edge-b
```

Hub `/leafz`在包含预跑流量后的最终记录为：

```text
edge-a -> hub:
in_msgs=2978
in_bytes=46839889920

hub -> edge-b:
out_msgs=2978
out_bytes=46839889920
```

回复仅为 16 bytes，因此反向累计字节约 47 KiB。

## 5. 运行状态

两个正式阶段均满足：

- 请求成功率 100%。
- 没有超时或 NoResponders。
- 没有 NATS 或 LeafNode 重连。
- `slow_consumers=0`。
- 客户端 pending 始终为 0。
- NATS Server 没有出现超过两帧的 pending。
- NATS 进程内存没有持续增长。
- Core NATS 不写磁盘，容器 Block I/O 基本为 0。

监控偶尔采集到约 15 MiB 的服务端 pending。这表示采样时一条帧正在 socket
发送，下一次采样即归零，`pending_msgs=0`，不属于消息队列累积。

## 6. 性能边界

local 的 P95 低于 100 ms，可以在“收到回复再发下一帧”的前提下保持 10 FPS。

global 的 P95 为 121.593 ms，实际吞吐为 9.610 FPS。这里没有消息堆积，限制来自
单帧端到端耗时：

```text
源 Agent 序列化和写入
  + edge-a 转发
  + edge-a 到 Hub
  + Hub 到 edge-b
  + edge-b 向 Agent 投递
  + 处理和回复
```

串行请求要达到 10 FPS，每帧完整往返必须稳定低于 100 ms。15 MiB、10 FPS 对请求
方向的有效载荷带宽要求为：

```text
150 MiB/s，约 1.26 Gbit/s
```

实际跨机器链路还要叠加协议开销、RTT 和网络抖动，因此最先达到上限的通常是
源边缘到云端或云端到目标边缘的可用带宽，而不是 NATS consumer 数量。

## 7. 卡点判断

测试脚本每次采样三个节点的`/varz`和`/connz`。

| 现象 | 判断 |
|---|---|
| edge-a 字节不增长 | 发送 Agent、客户端 pending 或到 edge-a 的连接 |
| edge-a 增长，Hub 不增长 | edge-a LeafNode 到 Hub |
| Hub 增长，edge-b 不增长 | Hub 路由或 edge-b LeafNode |
| edge-b 增长，handler 计数不增长 | subject、订阅或目标 Agent |
| handler 已回复，发送端超时 | `_INBOX`反向路由或 timeout |
| pending 超过两帧并持续增长 | 下游带宽或消费速度不足 |
| pending 约一帧且下一采样归零 | 正常的单帧 socket 发送过程 |
| `slow_consumers`增加 | 接收端处理或 socket 读取速度不足 |
| 没有错误但 FPS 下降 | 单帧往返耗时超过目标发送周期 |

## 8. 给 Codex 子 Agent 的测试 Prompt

```text
请在当前 K8S_demo 仓库实现并执行 NATS Core 二进制帧 local/global 稳定性测试。

测试必须使用 runtime_api.NatsComm 的 request_frame_bytes() 和
subscribe_frame_bytes()，不能把帧转成 JSON/Base64，也不能为每帧创建连接。

测试条件：
1. 使用 Conda k8s 环境。
2. 每帧 15 MiB。
3. 每次发送后必须等待处理回复，再发送下一帧。
4. 最大发送速率 10 FPS。
5. local 和 global 各持续 300 秒。
6. local：发送端和接收端都连接 edge-a。
7. global：发送端连接 edge-a，接收端连接 edge-b，流量必须经过 cloud-hub。
8. 接收端同时订阅 frame.local.<cluster>.<agent>.infer 和
   frame.global.<cluster>.<agent>.infer。
9. LeafNode 必须同时 deny_imports 和 deny_exports frame.local.>。

观测要求：
1. 每 5 秒读取 edge-a、cloud-hub、edge-b 的 /varz 和 /connz。
2. 记录 in/out msgs、in/out bytes、pending、slow_consumers、连接数和内存。
3. 同时保存三台 NATS 的服务日志和 Hub /leafz。
4. local 阶段必须证明 cloud-hub、edge-b 的大帧字节增量为 0。
5. global 阶段必须证明 edge-a、cloud-hub、edge-b 的字节增量与成功帧数匹配。
6. 输出成功数、错误数、实际 FPS、P50/P95/P99/max。
7. pending 约等于一帧且下一采样归零时标记为瞬时发送，不要误判为堆积。
8. pending 超过两帧并持续增长时才判定下游变慢。

发生超时或卡顿时，依据三端字节增量、handler 计数、reply 状态和 pending 判断卡在：
发送 Agent -> edge-a -> LeafNode/Hub -> edge-b -> 目标订阅 -> 回复链路。

测试完成后更新 docs/nats-binary-soak-test.md，写明命令、环境、完整结果、流量证据、
最可能的性能边界和复测方法。不要只报告客户端成功率。
```
