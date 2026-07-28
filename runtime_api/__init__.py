"""
runtime_api — 基于 NATS JetStream 的运行时通信层
===============================================

本模块为 K8S 容器化应用提供一套基于 NATS JetStream 的消息通信基础设施。
应用在 Pod 内部通过 NatsComm 客户端发送/接收消息，无需关心底层 NATS
集群的部署细节。

模块组成
--------
- NatsComm         : 核心通信客户端，支持发布/订阅、请求/响应、流式消费
- NatsMessage      : 消息封装类，支持 ACK/NACK/进度/终止等 JetStream 操作
- NatsBinaryMessage: Core NATS 原始二进制消息
- NatsMemoryFrameMessage: JetStream Memory 原始二进制帧
- build_stream_config  : 构建 JetStream Stream 配置（名称、主题、存储策略）
- ensure_jetstream_stream : 确保 JetStream Stream 存在（自动创建/更新）
- parse_bytes      : 解析 NATS 风格的大小字符串（如 "5GB" → int）

快速示例
--------
    # 发送消息
    nc = NatsComm()
    await nc.connect()
    result = await nc.send("workflow.task.execute", {"task_id": "123"})

    # 消费消息
    msgs = await nc.receive("workflow.task.execute", durable="worker-1", batch=5)
    for msg in msgs:
        print(msg.payload)
        await msg.ack()

    # 请求-响应模式（服务端）
    await nc.respond("workflow.rpc.task", handler=my_handler)

    # 请求-响应模式（客户端）
    result = await nc.request("workflow.rpc.task", {"action": "query"})

环境变量
--------
    NATS_SERVERS              NATS 服务器地址（默认 nats://nats:4222）
    NATS_STREAM               兼容模式流名称（默认 WORKFLOW）
    NATS_STREAM_SUBJECTS      兼容流主题列表（默认 legacy.workflow.>）
    NATS_WORKFLOW_STREAM_PREFIX 实例 Stream 前缀（默认 WF）
    NATS_FRAME_STREAM_PREFIX Memory 帧 Stream 前缀（默认 FRAME）
    NATS_FRAME_STREAM_MAX_BYTES 单实例 Memory 帧上限（默认 512MiB）
    NATS_FRAME_STREAM_MAX_AGE_SEC Memory 帧最大保留时间（默认120秒）
    NATS_FRAME_ACK_WAIT_SEC Memory 帧 ACK 等待时间（默认60秒）
    NATS_FRAME_MAX_DELIVER Memory 帧最大投递次数（默认3）
    NATS_STREAM_MAX_BYTES     流最大大小（默认 512MiB）
    NATS_STREAM_DISCARD       淘汰策略（默认 new）
    NATS_STREAM_RETENTION     保留策略（默认 workqueue）
    NATS_STREAM_STORAGE       存储类型（默认 file）
    NATS_JETSTREAM_DOMAIN     JetStream 域（默认 hub）
    NATS_SEND_DELAY_SECONDS   发送前延迟秒数（默认 0）
    NATS_SEND_DELAY_FILE      运行时延迟配置文件（默认 /tmp/nats_send_delay_seconds）
    NATS_CONTROL_MAX_BYTES    NATS 控制消息上限（默认 1MiB）
    NATS_BINARY_MAX_BYTES     NATS 二进制消息上限（默认 64MiB）
    NATS_PENDING_SIZE_BYTES   NATS 客户端发送缓冲上限（默认 128MiB）
    NATS_BINARY_PENDING_MSGS  二进制订阅待处理消息数（默认 32）
    NATS_BINARY_PENDING_BYTES 二进制订阅待处理字节数（默认 128MiB）
    NATS_EPHEMERAL_CONSUMER_INACTIVE_SEC 临时consumer自动清理时间（默认30秒）
    NATS_STREAM_PROVISION_TIMEOUT_SEC 等待编排器创建实例 Stream 的时间（默认120秒）

运行时修改发送延迟
----------------
容器内执行以下命令可影响后续 send() 调用：

    echo 0.2 > /tmp/nats_send_delay_seconds   # 设置 200ms 发送前延迟
    echo 0 > /tmp/nats_send_delay_seconds     # 关闭延迟
"""

from .jetstream_stream import build_stream_config, ensure_jetstream_stream, parse_bytes
from .nats_comm import (
    NatsBinaryMessage,
    NatsComm,
    NatsMemoryFrameMessage,
    NatsMessage,
)

__all__ = [
    "NatsComm",
    "NatsMessage",
    "NatsBinaryMessage",
    "NatsMemoryFrameMessage",
    "FrameComm",
    "FrameTransportClient",
    "DownloadedFrame",
    "build_stream_config",
    "ensure_jetstream_stream",
    "parse_bytes",
]


def __getattr__(name):
    if name in {"FrameComm", "FrameTransportClient", "DownloadedFrame"}:
        from .frame_comm import DownloadedFrame, FrameComm, FrameTransportClient

        return {
            "FrameComm": FrameComm,
            "FrameTransportClient": FrameTransportClient,
            "DownloadedFrame": DownloadedFrame,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
