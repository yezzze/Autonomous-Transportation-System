"""
NATS 运行时通信客户端
====================

为 K8S 容器化应用提供基于 NATS JetStream 的高层消息通信抽象。
应用在 Pod 内部使用 NatsComm 即可完成消息收发，无需感知底层
NATS 集群的部署、连接管理和流的生命周期。

支持的通信模式
--------------
1. 发布-订阅 (Pub/Sub)        : send() + receive()
2. 请求-响应 (Request/Reply)  : request() + respond()
3. 流式消费 (Stream Consumer)  : serve()   — 长轮询拉模式
4. 通用二进制 (JetStream)      : send_bytes() + receive_bytes()
5. 二进制帧 (Core NATS)        : request_frame_bytes() + subscribe_frame_bytes()
6. 可靠帧 (JetStream Memory)   : request_memory_frame() + serve_memory_frames()

类说明
------
- NatsComm    : 核心通信客户端类
- NatsMessage : 消息封装类，提供 ACK/NACK/进度/终止等操作
- NatsBinaryMessage : Core NATS 原始二进制消息

快速示例
--------
    import asyncio
    from runtime_api.nats_comm import NatsComm

    async def main():
        # 1. 创建客户端
        nc = NatsComm()

        # 2. 连接（自动确保流存在）
        await nc.connect()

        # 3. 发送消息
        result = await nc.send("workflow.task.start", {
            "task_id": "task-001",
            "action": "build",
        })
        print(f"发送成功: stream={result['stream']}, seq={result['seq']}")

        # 4. 接收消息（Pull 消费者）
        msgs = await nc.receive(
            subject="workflow.task.>",
            durable="worker-1",        # 消费者名称（用于断点续传）
            batch=5,                   # 一次拉取 5 条
            timeout_sec=3.0,           # 等待超时
            ack=False,                 # 手动确认
        )
        for msg in msgs:
            print(f"收到: {msg.payload}")
            await msg.ack()            # 手动确认

        # 5. 请求-响应模式
        # 启动服务端
        asyncio.create_task(nc.respond("workflow.rpc.calc", handler=calc_handler))
        # 客户端发起请求
        result = await nc.request("workflow.rpc.calc", {"x": 1, "y": 2})
        print(f"RPC 结果: {result}")

        await nc.close()

    def calc_handler(payload):
        return {"sum": payload["x"] + payload["y"]}

    asyncio.run(main())

环境变量
--------
    NATS_SERVERS              NATS 集群地址（默认 nats://nats:4222）
    NATS_STREAM               显式兼容流名称（Agent 模式不要设置）
    NATS_STREAM_SUBJECTS      显式兼容流主题（Agent 模式不要设置）
    NATS_JETSTREAM_DOMAIN     Agent 默认 CLUSTER_ID，非 Agent 默认 hub
    NATS_WORKFLOW_STREAM_PREFIX 每实例 Stream 名称前缀（默认 WF）
    NATS_FRAME_STREAM_PREFIX    每实例 Memory 帧 Stream 前缀（默认 FRAME）
    NATS_FRAME_STREAM_MAX_BYTES 每实例 Memory 帧 Stream 上限（默认 512MiB）
    NATS_FRAME_STREAM_MAX_AGE_SEC Memory 帧最长保留秒数（默认 120）
    NATS_FRAME_ACK_WAIT_SEC     Memory 帧 Consumer ACK 等待秒数（默认 60）
    NATS_FRAME_MAX_DELIVER      Memory 帧最大投递次数（默认 3）
    NATS_SEND_DELAY_SECONDS   发送前延迟秒数（默认 0）
    NATS_SEND_DELAY_FILE      运行时延迟配置文件（默认 /tmp/nats_send_delay_seconds）
    NATS_CONTROL_MAX_BYTES    NATS 控制消息上限（默认 1MiB）
    NATS_BINARY_MAX_BYTES     NATS 二进制消息上限（默认 64MiB）
    NATS_PENDING_SIZE_BYTES   NATS 客户端发送缓冲上限（默认 128MiB）
    NATS_BINARY_PENDING_MSGS  二进制订阅待处理消息数（默认 32）
    NATS_BINARY_PENDING_BYTES 二进制订阅待处理字节数（默认 128MiB）
"""

import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from nats.aio.client import Client as NATS
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import (
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    RetentionPolicy,
)
from nats.js.errors import BadRequestError, NotFoundError
from runtime_api.jetstream_stream import ensure_jetstream_stream, parse_bytes

logger = logging.getLogger(__name__)

BinaryPayload = Union[bytes, bytearray, memoryview]

_FRAME_REPLY_HEADER = "X-Frame-Reply"
_FRAME_REQUEST_ID_HEADER = "X-Frame-Request-Id"


def _iso_timestamp(value) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _binary_payload_bytes(payload: BinaryPayload, field_name: str = "payload") -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError(f"{field_name} must be bytes-like")
    return bytes(payload)


def _subject_token(value: str, field_name: str) -> str:
    token = str(value).strip()
    if not token or any(char.isspace() for char in token) or "." in token:
        raise ValueError(f"{field_name} must be one non-empty NATS subject token")
    if "*" in token or ">" in token:
        raise ValueError(f"{field_name} cannot contain NATS wildcards")
    return token


@dataclass
class NatsMessage:
    """
    NATS JetStream 消息封装。

    封装从 JetStream 拉取的原始消息，提供便捷的确认/拒绝操作。
    每个消息包含主题、载荷和元数据。

    属性
    ----
    subject : str
        消息所在的主题
    payload : Dict[str, Any]
        消息的 JSON 载荷（已自动反序列化）
    stream : Optional[str]
        所属流名称
    consumer : Optional[str]
        消费者名称
    stream_seq : Optional[int]
        流中的序列号
    consumer_seq : Optional[int]
        消费者中的序列号
    _raw : Any
        原始 JetStream 消息对象（用于 ACK/NACK 操作）

    示例
    ----
        msg = NatsMessage(
            subject="workflow.task.execute",
            payload={"cmd": "build"},
            stream="WORKFLOW",
            stream_seq=42,
            _raw=raw_js_msg,
        )
        await msg.ack()         # 确认处理成功
        await msg.nak()         # 拒绝，重新投递
        await msg.nak(delay=5)  # 延迟 5 秒后重新投递
        await msg.in_progress() # 标记处理中（防止超时重投）
        await msg.term()        # 终止（不再重试）
    """

    subject: str
    payload: Dict[str, Any]
    stream: Optional[str] = None
    consumer: Optional[str] = None
    stream_seq: Optional[int] = None
    consumer_seq: Optional[int] = None
    _raw: Any = field(default=None, repr=False)

    async def ack(self) -> None:
        """
        确认消息已成功处理（ACK）。

        通知 JetStream 该消息已被成功处理，不会再次投递。
        如果消息配置了手动确认模式，必须在处理完成后调用此方法。
        """
        if self._raw is None:
            raise RuntimeError("消息没有 JetStream 确认句柄")
        await self._raw.ack()

    async def nak(self, delay: Optional[float] = None) -> None:
        """
        拒绝消息（NACK），使其重新投递。

        当消息处理失败时调用，JetStream 会将该消息重新投递给
        当前或另一个消费者。

        参数
        ----
        delay : Optional[float]
            可选的重投延迟（秒），在此时间内不会重新投递
        """
        if self._raw is None:
            raise RuntimeError("消息没有 JetStream 确认句柄")
        if delay is None:
            await self._raw.nak()
        else:
            await self._raw.nak(delay=delay)

    async def in_progress(self) -> None:
        """
        标记消息正在处理中。

        用于长时间处理的任务，防止 JetStream 认为消息超时未处理
        而将其重新投递给其他消费者。
        """
        if self._raw is None:
            raise RuntimeError("消息没有 JetStream 确认句柄")
        await self._raw.in_progress()

    async def term(self) -> None:
        """
        终止消息（Term），不再重试。

        当消息无法被处理且不应该重新投递时调用。
        例如: 消息格式错误、业务逻辑明确拒绝等场景。
        """
        if self._raw is None:
            raise RuntimeError("消息没有 JetStream 确认句柄")
        await self._raw.term()


@dataclass
class NatsJetStreamBinaryMessage:
    """JetStream 原始二进制消息，保留 ACK/NACK 和序列元数据。"""

    subject: str
    data: bytes
    headers: Optional[Dict[str, str]] = None
    stream: Optional[str] = None
    consumer: Optional[str] = None
    stream_seq: Optional[int] = None
    consumer_seq: Optional[int] = None
    _raw: Any = field(default=None, repr=False)

    async def ack(self) -> None:
        if self._raw is None:
            raise RuntimeError("binary message does not contain an ACK handle")
        await self._raw.ack()

    async def nak(self, delay: Optional[float] = None) -> None:
        if self._raw is None:
            raise RuntimeError("binary message does not contain an ACK handle")
        if delay is None:
            await self._raw.nak()
        else:
            await self._raw.nak(delay=delay)

    async def in_progress(self) -> None:
        if self._raw is None:
            raise RuntimeError("binary message does not contain an ACK handle")
        await self._raw.in_progress()

    async def term(self) -> None:
        if self._raw is None:
            raise RuntimeError("binary message does not contain an ACK handle")
        await self._raw.term()


@dataclass
class NatsBinaryMessage:
    """Core NATS 原始二进制消息。"""

    subject: str
    data: bytes
    reply_subject: str = ""
    headers: Optional[Dict[str, str]] = None
    _raw: Any = field(default=None, repr=False)
    _max_payload_bytes: Optional[int] = field(default=None, repr=False)

    async def respond(self, payload: BinaryPayload) -> None:
        if not self.reply_subject:
            raise ValueError("binary message does not contain a reply subject")
        if self._raw is None:
            raise RuntimeError("binary message does not contain a NATS reply handle")
        encoded = _binary_payload_bytes(payload)
        if (
            self._max_payload_bytes is not None
            and len(encoded) > self._max_payload_bytes
        ):
            raise ValueError(
                f"NATS binary response is {len(encoded)} bytes, exceeding "
                f"NATS_BINARY_MAX_BYTES={self._max_payload_bytes}"
            )
        await self._raw.respond(encoded)


@dataclass
class NatsMemoryFrameMessage:
    """JetStream Memory 中的一帧二进制消息。"""

    subject: str
    data: bytes
    reply_subject: str
    request_id: str
    headers: Optional[Dict[str, str]] = None
    stream: Optional[str] = None
    consumer: Optional[str] = None
    stream_seq: Optional[int] = None
    delivered: int = 1
    _raw: Any = field(default=None, repr=False)

    async def in_progress(self) -> None:
        if self._raw is None:
            raise RuntimeError("memory frame does not contain an ACK handle")
        await self._raw.in_progress()


class NatsComm:
    """
    NATS 运行时通信客户端。

    容器化应用中 NATS 通信的入口类，封装了连接管理、消息发送、
    消息接收、请求响应和流式消费等功能。

    构造参数
    --------
    servers : Optional[List[str]]
        NATS 服务器地址列表，默认从环境变量 NATS_SERVERS 读取
    stream : Optional[str]
        显式兼容 JetStream 流名称。Agent 模式不传，由环境变量身份生成。
    stream_subjects : Optional[List[str]]
        显式兼容流主题。Agent 模式不传，由环境变量身份生成。
    jetstream_domain : Optional[str]
        JetStream 域，默认从环境变量 NATS_JETSTREAM_DOMAIN 读取
    send_delay : Optional[float]
        发送前模拟的网络延迟（秒）。默认从 NATS_SEND_DELAY_SECONDS 读取。
        运行中可通过 NATS_SEND_DELAY_FILE 指向的文件动态覆盖。

    使用模式
    --------
    1. 简单 Pub/Sub
        nc = NatsComm()
        await nc.connect()
        await nc.send("subject", {"data": "hello"})
        msgs = await nc.receive("subject", durable="consumer1")
        await nc.close()

    2. 请求-响应
        # 服务端
        await nc.respond("rpc.subject", handler)
        # 客户端
        result = await nc.request("rpc.subject", {"req": "data"})

    3. 流式消费（持续拉取）
        await nc.serve("subject", durable="worker1", handler=my_handler)
    """

    def __init__(
        self,
        servers: Optional[List[str]] = None,
        stream: Optional[str] = None,
        stream_subjects: Optional[List[str]] = None,
        jetstream_domain: Optional[str] = None,
        send_delay: Optional[float] = None,
        send_delay_file: Optional[str] = None,
        max_control_payload_bytes: Optional[int] = None,
        max_binary_payload_bytes: Optional[int] = None,
    ):
        """
        初始化 NatsComm 实例。

        所有参数都有默认值，大多数场景只需 `NatsComm()` 即可。存在
        AGENT_INSTANCE_ID 时，默认 Stream 为 WF_<instance-id>，Subject 由
        CLUSTER_ID、AGENT_ID 和 AGENT_INSTANCE_ID 共同生成。

        参数
        ----
        send_delay : Optional[float]
            发送前模拟的网络延迟（秒）。如果不传，则读取
            NATS_SEND_DELAY_SECONDS，默认 0（不延迟）。
        send_delay_file : Optional[str]
            运行时延迟配置文件。默认读取 NATS_SEND_DELAY_FILE，
            如果未设置则使用 /tmp/nats_send_delay_seconds。
        """
        self.servers = servers or self._servers_from_env()
        self.workflow_stream_prefix = os.environ.get(
            "NATS_WORKFLOW_STREAM_PREFIX",
            "WF",
        )
        self.frame_stream_prefix = os.environ.get(
            "NATS_FRAME_STREAM_PREFIX",
            "FRAME",
        )
        self._default_workflow_identity: Optional[Tuple[str, str, str]] = None
        configured_stream = (
            stream if stream is not None else os.environ.get("NATS_STREAM")
        )
        configured_subjects = (
            stream_subjects
            if stream_subjects is not None
            else (
                self._stream_subjects_from_env()
                if "NATS_STREAM_SUBJECTS" in os.environ
                else None
            )
        )
        instance_from_env = (
            os.environ.get("AGENT_INSTANCE_ID")
            or os.environ.get("POD_UID")
        )
        if (
            configured_stream is None
            and configured_subjects is None
            and instance_from_env
        ):
            cluster = self._required_agent_env_token("CLUSTER_ID")
            agent = self._required_agent_env_token("AGENT_ID")
            instance = self._instance_id(instance_from_env)
            self.stream = self.workflow_stream_name(instance)
            self.stream_subjects = list(
                self.workflow_stream_subjects(cluster, agent, instance)
            )
            self._default_workflow_identity = (cluster, agent, instance)
        else:
            self.stream = configured_stream or "WORKFLOW"
            self.stream_subjects = (
                configured_subjects or self._stream_subjects_from_env()
            )
        configured_domain = (
            jetstream_domain
            if jetstream_domain is not None
            else os.environ.get("NATS_JETSTREAM_DOMAIN")
        )
        if self._default_workflow_identity is not None:
            cluster, _, _ = self._default_workflow_identity
            self.jetstream_domain = configured_domain or cluster
            if self.jetstream_domain != cluster:
                raise ValueError(
                    "NATS_JETSTREAM_DOMAIN must equal CLUSTER_ID in Agent "
                    f"mode: domain={self.jetstream_domain!r}, cluster={cluster!r}"
                )
        else:
            self.jetstream_domain = configured_domain or "hub"
        self.frame_stream_max_bytes = parse_bytes(
            os.environ.get("NATS_FRAME_STREAM_MAX_BYTES", "512MiB")
        )
        self.frame_stream_max_age_sec = float(
            os.environ.get("NATS_FRAME_STREAM_MAX_AGE_SEC", "120")
        )
        self.frame_ack_wait_sec = float(
            os.environ.get("NATS_FRAME_ACK_WAIT_SEC", "60")
        )
        self.frame_max_deliver = int(
            os.environ.get("NATS_FRAME_MAX_DELIVER", "3")
        )
        self.send_delay = self._parse_delay_seconds(
            str(send_delay) if send_delay is not None else os.environ.get("NATS_SEND_DELAY_SECONDS"),
            default=0.0,
            source="NATS_SEND_DELAY_SECONDS",
        )
        self.send_delay_file = Path(
            send_delay_file
            or os.environ.get("NATS_SEND_DELAY_FILE", "/tmp/nats_send_delay_seconds")
        )
        self.max_control_payload_bytes = (
            max_control_payload_bytes
            if max_control_payload_bytes is not None
            else int(os.environ.get("NATS_CONTROL_MAX_BYTES", str(1024 * 1024)))
        )
        if self.max_control_payload_bytes <= 0:
            raise ValueError("NATS_CONTROL_MAX_BYTES must be positive")
        self.max_binary_payload_bytes = (
            max_binary_payload_bytes
            if max_binary_payload_bytes is not None
            else int(os.environ.get("NATS_BINARY_MAX_BYTES", str(64 * 1024 * 1024)))
        )
        if self.max_binary_payload_bytes <= 0:
            raise ValueError("NATS_BINARY_MAX_BYTES must be positive")
        self.pending_size_bytes = int(
            os.environ.get("NATS_PENDING_SIZE_BYTES", str(128 * 1024 * 1024))
        )
        self.binary_pending_msgs = int(
            os.environ.get("NATS_BINARY_PENDING_MSGS", "32")
        )
        self.binary_pending_bytes = int(
            os.environ.get(
                "NATS_BINARY_PENDING_BYTES",
                str(128 * 1024 * 1024),
            )
        )
        if self.pending_size_bytes <= 0:
            raise ValueError("NATS_PENDING_SIZE_BYTES must be positive")
        if self.binary_pending_msgs <= 0:
            raise ValueError("NATS_BINARY_PENDING_MSGS must be positive")
        if self.binary_pending_bytes <= 0:
            raise ValueError("NATS_BINARY_PENDING_BYTES must be positive")
        if self.frame_stream_max_bytes <= 0:
            raise ValueError("NATS_FRAME_STREAM_MAX_BYTES must be positive")
        if self.frame_stream_max_age_sec <= 0:
            raise ValueError("NATS_FRAME_STREAM_MAX_AGE_SEC must be positive")
        if self.frame_ack_wait_sec <= 0:
            raise ValueError("NATS_FRAME_ACK_WAIT_SEC must be positive")
        if self.frame_max_deliver <= 0:
            raise ValueError("NATS_FRAME_MAX_DELIVER must be positive")
        self._nc = NATS()
        self._js = None
        self._routed_js: Dict[str, Any] = {}
        self._routed_streams_ready: Set[str] = set()
        self._connect_lock = asyncio.Lock()
        self._routed_stream_lock = asyncio.Lock()
        self._pull_subscription_lock = asyncio.Lock()
        self._pull_subscriptions: Dict[Tuple[str, str, str, str], Any] = {}
        self._binary_subscription_lock = asyncio.Lock()
        self._binary_subscriptions: Dict[Tuple[str, str], Any] = {}
        self._managed_workflow_stream_lock = asyncio.Lock()
        self._managed_workflow_streams: Set[Tuple[str, str]] = set()
        self._managed_frame_stream_lock = asyncio.Lock()
        self._managed_frame_streams: Set[Tuple[str, str]] = set()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._ever_connected = False
        self.ephemeral_consumer_inactive_sec = float(
            os.environ.get("NATS_EPHEMERAL_CONSUMER_INACTIVE_SEC", "30")
        )
        self.stream_provision_timeout_sec = float(
            os.environ.get("NATS_STREAM_PROVISION_TIMEOUT_SEC", "120")
        )
        if self.ephemeral_consumer_inactive_sec <= 0:
            raise ValueError("NATS_EPHEMERAL_CONSUMER_INACTIVE_SEC must be positive")
        if self.stream_provision_timeout_sec <= 0:
            raise ValueError("NATS_STREAM_PROVISION_TIMEOUT_SEC must be positive")

    @classmethod
    async def create(cls, **kwargs) -> "NatsComm":
        """
        创建客户端并等待默认 Stream 在 NATS 服务端就绪。

        Agent 模式从 CLUSTER_ID、AGENT_ID、AGENT_INSTANCE_ID 生成
        WF_<instance-id> 以及实例级 local/global Subjects。网络操作不能放在
        同步 __init__() 中，因此需要通过该异步工厂保证返回时 Stream 已创建。
        """
        comm = cls(**kwargs)
        try:
            await comm.connect()
        except BaseException:
            await comm.close()
            raise
        return comm

    def _encode_control_payload(self, payload: Dict[str, Any]) -> bytes:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        if len(encoded) > self.max_control_payload_bytes:
            raise ValueError(
                f"NATS control payload is {len(encoded)} bytes, exceeding "
                f"NATS_CONTROL_MAX_BYTES={self.max_control_payload_bytes}; "
                "use request_frame_bytes() or request_bytes() for binary data"
            )
        return encoded

    def _encode_binary_payload(self, payload: BinaryPayload) -> bytes:
        encoded = _binary_payload_bytes(payload)
        if len(encoded) > self.max_binary_payload_bytes:
            raise ValueError(
                f"NATS binary payload is {len(encoded)} bytes, exceeding "
                f"NATS_BINARY_MAX_BYTES={self.max_binary_payload_bytes}"
            )
        return encoded

    @staticmethod
    def _servers_from_env() -> List[str]:
        """
        从环境变量 NATS_SERVERS 读取 NATS 服务器地址。

        多个地址用逗号分隔: "nats://host1:4222,nats://host2:4222"

        返回
        ----
        List[str]
            服务器地址列表，默认 ["nats://nats:4222"]
        """
        raw = os.environ.get("NATS_SERVERS", "nats://nats:4222")
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _required_agent_env_token(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            raise ValueError(
                f"{name} is required when AGENT_INSTANCE_ID or POD_UID is set"
            )
        return _subject_token(value, name)

    @staticmethod
    def _stream_subjects_from_env() -> List[str]:
        """
        从环境变量 NATS_STREAM_SUBJECTS 读取流主题列表。

        返回
        ----
        List[str]
            主题列表，默认 ["legacy.workflow.>"]
        """
        raw = os.environ.get("NATS_STREAM_SUBJECTS", "legacy.workflow.>")
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _parse_delay_seconds(
        raw: Optional[str],
        default: float = 0.0,
        source: str = "send_delay",
    ) -> float:
        """
        解析延迟秒数，非法值回退到 default，负数归零。
        """
        if raw is None:
            return max(0.0, default)
        raw = raw.strip()
        if not raw:
            return max(0.0, default)
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("忽略非法发送延迟配置 %s=%r，继续使用 %.3fs", source, raw, default)
            return max(0.0, default)

    def set_send_delay(self, seconds: float) -> None:
        """
        在当前进程内设置基础发送延迟。

        如果 send_delay_file 存在，send() 会优先使用文件中的值。
        容器运行中建议直接修改该文件以影响已启动进程。
        """
        self.send_delay = max(0.0, float(seconds))

    def get_send_delay(self) -> float:
        """
        获取当前生效的发送前延迟秒数。

        如果运行时延迟文件存在并且内容合法，返回文件中的值；
        否则返回构造参数或环境变量设置的基础值。
        """
        try:
            raw = self.send_delay_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self.send_delay
        except OSError as exc:
            logger.warning("读取发送延迟配置文件失败 path=%s error=%s", self.send_delay_file, exc)
            return self.send_delay
        return self._parse_delay_seconds(
            raw,
            default=self.send_delay,
            source=str(self.send_delay_file),
        )

    def _jetstream(self):
        """
        本地测试：忽略 jetstream_domain，直接使用默认域。
        """
        return self._nc.jetstream()

    @staticmethod
    def _workflow_route(
        subject: str,
    ) -> Optional[Tuple[str, str, str, str]]:
        tokens = subject.split(".")
        if (
            len(tokens) < 8
            or tokens[0] != "workflow"
            or tokens[1] not in {"local", "global"}
            or tokens[3] != "agent"
            or tokens[5] != "instance"
        ):
            return None
        return (
            tokens[1],
            _subject_token(tokens[2], "target_cluster"),
            _subject_token(tokens[4], "agent_id"),
            NatsComm._instance_id(tokens[6]),
        )

    @staticmethod
    def _instance_id(instance_id: Optional[str] = None) -> str:
        configured = (
            instance_id
            or os.environ.get("AGENT_INSTANCE_ID")
            or os.environ.get("POD_UID")
            or ""
        )
        token = _subject_token(configured, "instance_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError(
                "instance_id may contain only ASCII letters, digits, '-' and '_'"
            )
        return token

    def workflow_stream_name(self, instance_id: str) -> str:
        instance = self._instance_id(instance_id)
        prefix = self.workflow_stream_prefix.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
            raise ValueError(
                "NATS_WORKFLOW_STREAM_PREFIX may contain only ASCII letters, "
                "digits, '-' and '_'"
            )
        return f"{prefix}_{instance}"

    def memory_frame_stream_name(self, instance_id: str) -> str:
        """返回一个 Agent 实例独享的 Memory 帧 Stream 名称。"""
        instance = self._instance_id(instance_id)
        prefix = self.frame_stream_prefix.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
            raise ValueError(
                "NATS_FRAME_STREAM_PREFIX may contain only ASCII letters, "
                "digits, '-' and '_'"
            )
        return f"{prefix}_{instance}"

    @staticmethod
    def workflow_stream_subjects(
        cluster_id: str,
        agent_id: str,
        instance_id: str,
    ) -> Tuple[str, str]:
        cluster = _subject_token(cluster_id, "cluster_id")
        agent = _subject_token(agent_id, "agent_id")
        instance = NatsComm._instance_id(instance_id)
        return (
            f"workflow.local.{cluster}.agent.{agent}.instance.{instance}.>",
            f"workflow.global.{cluster}.agent.{agent}.instance.{instance}.>",
        )

    @staticmethod
    def memory_frame_stream_subjects(
        cluster_id: str,
        agent_id: str,
        instance_id: str,
    ) -> Tuple[str, str]:
        """返回实例级 Memory 帧 Stream 的 local/global Subject 范围。"""
        cluster = _subject_token(cluster_id, "cluster_id")
        agent = _subject_token(agent_id, "agent_id")
        instance = NatsComm._instance_id(instance_id)
        return (
            f"frame.local.{cluster}.agent.{agent}.instance.{instance}.>",
            f"frame.global.{cluster}.agent.{agent}.instance.{instance}.>",
        )

    async def _jetstream_for_subject(self, subject: str):
        route = self._workflow_route(subject)
        if route is None:
            await self.connect()
            return self._js, "legacy"

        _, target_cluster, _, instance_id = route
        await self.connect(ensure_stream=False)
        async with self._routed_stream_lock:
            js = self._routed_js.get(target_cluster)
            if js is None:
                js = self._nc.jetstream(domain=target_cluster)
                self._routed_js[target_cluster] = js
            return js, self.workflow_stream_name(instance_id)

    async def _jetstream_for_domain(self, cluster_id: str):
        cluster = _subject_token(cluster_id, "cluster_id")
        await self.connect(ensure_stream=False)
        async with self._routed_stream_lock:
            js = self._routed_js.get(cluster)
            if js is None:
                js = self._nc.jetstream(domain=cluster)
                self._routed_js[cluster] = js
            return js

    async def connect(self, ensure_stream: bool = True) -> None:
        """
        连接到 NATS 服务器。

        幂等方法——如果已经连接则跳过。可选择是否自动确保流存在。

        参数
        ----
        ensure_stream : bool
            是否在连接后确保 JetStream 流存在并创建/更新（默认 True）

        示例
        ----
            nc = NatsComm()
            await nc.connect()                    # 连接 + 确保流存在
            await nc.connect(ensure_stream=False)  # 仅连接，不创建流
        """
        register_identity = None
        async with self._connect_lock:
            if not self._nc.is_connected:
                await self._nc.connect(
                    servers=self.servers,
                    connect_timeout=5,           # 连接超时 5 秒
                    reconnect_time_wait=2,       # 重连间隔 2 秒
                    max_reconnect_attempts=10,   # 最多重试 10 次
                    pending_size=self.pending_size_bytes,
                    error_cb=self._on_error,
                    disconnected_cb=self._on_disconnected,
                    reconnected_cb=self._on_reconnected,
                    closed_cb=self._on_closed,
                )
                self._ever_connected = True
                logger.info("NATS 已连接 servers=%s", self.servers)
            if ensure_stream and self._js is None:
                self._js = self._jetstream()
                await self._ensure_stream()
            if ensure_stream and self._default_workflow_identity is not None:
                register_identity = self._default_workflow_identity

        if register_identity is not None:
            cluster, _, instance = register_identity
            async with self._managed_workflow_stream_lock:
                closing = self._closing
                if not closing:
                    self._managed_workflow_streams.add((cluster, instance))
            if closing:
                await self.delete_workflow_stream(
                    target_cluster=cluster,
                    instance_id=instance,
                )
                raise RuntimeError("NatsComm is closing")

    async def _on_error(self, exc: Exception) -> None:
        logger.warning("NATS 异步连接错误: %s", exc)

    async def _on_disconnected(self) -> None:
        if getattr(self._nc, "is_draining", False) or self._nc.is_closed:
            logger.info("NATS 连接正在正常关闭")
        else:
            logger.warning("NATS 连接已断开，等待自动重连")

    async def _on_reconnected(self) -> None:
        logger.info("NATS 已重新连接 url=%s", self._nc.connected_url)

    async def _on_closed(self) -> None:
        logger.info("NATS 连接已关闭")

    async def __aenter__(self) -> "NatsComm":
        if self._closing or self._closed:
            raise RuntimeError("NatsComm is closing or already closed")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        await self.close()

    def __del__(self) -> None:
        try:
            if self._ever_connected and not self._closed:
                logger.error(
                    "NatsComm 在未调用 close() 的情况下被回收；"
                    "自主管理的实例 Stream 可能残留"
                )
        except Exception:
            pass

    async def close(self) -> None:
        """
        销毁当前客户端自主管理的 Memory Stream，并关闭 NATS 连接。

        使用 drain() 优雅关闭，等待所有未完成的消息处理完毕。
        应用必须在退出时调用；推荐使用 ``async with NatsComm()``。
        """
        async with self._close_lock:
            if self._closed:
                return
            async with self._managed_workflow_stream_lock:
                self._closing = True
                managed_workflow_streams = list(
                    self._managed_workflow_streams
                )
            async with self._managed_frame_stream_lock:
                self._closing = True
                managed_frame_streams = list(
                    self._managed_frame_streams
                )
            async with self._pull_subscription_lock:
                subscriptions = list(self._pull_subscriptions.values())
                self._pull_subscriptions.clear()
            for subscription in subscriptions:
                try:
                    await subscription.unsubscribe()
                except Exception:
                    logger.debug(
                        "关闭 pull subscription 失败",
                        exc_info=True,
                    )
            async with self._binary_subscription_lock:
                self._binary_subscriptions.clear()
            for cluster, instance_id in managed_workflow_streams:
                try:
                    await self.delete_workflow_stream(
                        target_cluster=cluster,
                        instance_id=instance_id,
                    )
                    logger.info(
                        "NatsComm 关闭时已删除自主管理的 Workflow Stream "
                        "cluster=%s instance_id=%s",
                        cluster,
                        instance_id,
                    )
                except Exception:
                    logger.exception(
                        "NatsComm 关闭时删除自主管理的 Workflow Stream 失败 "
                        "cluster=%s instance_id=%s",
                        cluster,
                        instance_id,
                    )
                finally:
                    async with self._managed_workflow_stream_lock:
                        self._managed_workflow_streams.discard(
                            (cluster, instance_id)
                        )
            for cluster, instance_id in managed_frame_streams:
                try:
                    await self.delete_memory_frame_stream(
                        target_cluster=cluster,
                        instance_id=instance_id,
                    )
                    logger.info(
                        "NatsComm 关闭时已删除自主管理的 Memory Stream "
                        "cluster=%s instance_id=%s",
                        cluster,
                        instance_id,
                    )
                except Exception:
                    logger.exception(
                        "NatsComm 关闭时删除自主管理的 Memory Stream 失败 "
                        "cluster=%s instance_id=%s",
                        cluster,
                        instance_id,
                    )
                finally:
                    async with self._managed_frame_stream_lock:
                        self._managed_frame_streams.discard(
                            (cluster, instance_id)
                        )
            if self._nc.is_connected:
                await self._nc.drain()
            self._closed = True

    async def _ensure_stream(self) -> None:
        """
        内部方法：确保 JetStream 流存在。

        委托给 jetstream_stream 模块的 ensure_jetstream_stream 函数。
        如果流不存在则自动创建，如果配置变更则自动更新。
        """
        try:
            await ensure_jetstream_stream(
                self._js,
                name=self.stream,
                subjects=self.stream_subjects,
                replace_subjects=self._default_workflow_identity is not None,
            )
        except Exception as exc:
            logger.warning("无法确保 JetStream 流 %s 存在: %s", self.stream, exc)
            raise

    async def send(self, subject: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        向指定主题发送一条消息（发布模式）。

        消息会被 JSON 序列化后发布到 JetStream 流中。
        如果尚未连接，会自动连接。

        如果配置了发送延迟，则会在发送前先 sleep，以模拟真实网络
        链路上的传输时延。运行中可通过修改 send_delay_file 动态调整。

        参数
        ----
        subject : str
            目标主题，如 "workflow.task.execute"
        payload : Dict[str, Any]
            消息载荷（字典），会被 JSON 序列化

        返回
        ----
        Dict[str, Any]
            {"subject": str, "stream": str, "seq": int}
            包含发布主题、所属流和序列号

        示例
        ----
            nc = NatsComm()
            result = await nc.send("workflow.task.execute", {
                "task_id": "123",
                "action": "run",
                "params": {"image": "ubuntu:latest"}
            })
            print(f"已发布: stream={result['stream']}, seq={result['seq']}")
        """
        route = self._workflow_route(subject)
        js, target_stream = await self._jetstream_for_subject(subject)
        # 模拟网络时延
        send_delay = self.get_send_delay()
        if send_delay > 0:
            await asyncio.sleep(send_delay)
        encoded = self._encode_control_payload(payload)
        if route is None:
            ack = await js.publish(subject, encoded)
        else:
            ack = await js.publish(
                subject,
                encoded,
                stream=target_stream,
            )
            if ack.stream != target_stream:
                raise RuntimeError(
                    "工作流消息进入了非目标 Stream: "
                    f"subject={subject} expected={target_stream} "
                    f"actual={ack.stream}"
                )
        return {
            "subject": subject,
            "stream": ack.stream,
            "seq": ack.seq,
        }

    async def send_bytes(
        self,
        subject: str,
        payload: BinaryPayload,
        timeout_sec: float = 30.0,
    ) -> Dict[str, Any]:
        """将原始二进制载荷发布到 JetStream，并等待服务器 PubAck。"""
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        encoded = self._encode_binary_payload(payload)
        route = self._workflow_route(subject)
        js, target_stream = await self._jetstream_for_subject(subject)
        send_delay = self.get_send_delay()
        if send_delay > 0:
            await asyncio.sleep(send_delay)
        if route is None:
            ack = await js.publish(subject, encoded, timeout=timeout_sec)
        else:
            ack = await js.publish(
                subject,
                encoded,
                timeout=timeout_sec,
                stream=target_stream,
            )
            if ack.stream != target_stream:
                raise RuntimeError(
                    "工作流二进制消息进入了非目标 Stream: "
                    f"subject={subject} expected={target_stream} "
                    f"actual={ack.stream}"
                )
        return {
            "subject": subject,
            "stream": ack.stream,
            "seq": ack.seq,
        }

    async def receive(
        self,
        subject: str,
        durable: Optional[str],
        batch: int = 1,
        timeout_sec: float = 5.0,
        ack: bool = False,
    ) -> List[NatsMessage]:
        """
        从指定主题拉取消息（Pull Consumer 模式）。

        创建一个 Pull 订阅者，从 JetStream 拉取指定数量的消息。
        支持自动确认和手动确认两种模式。

        参数
        ----
        subject : str
            订阅的主题，支持通配符，如 "workflow.task.>"
        durable : Optional[str]
            持久消费者名称，用于断点续传。
            相同名称的消费者从上次断开处继续消费。
            如果为 None，则创建临时消费者。
        batch : int
            一次拉取的消息数量（默认 1）
        timeout_sec : float
            等待超时时间（秒），超时返回空列表（默认 5 秒）
        ack : bool
            是否自动确认消息（默认 False）
            True  = 拉取后立即确认
            False = 需手动调用 msg.ack()

        返回
        ----
        List[NatsMessage]
            消息列表，超时或没有消息时返回空列表

        示例
        ----
            msgs = await nc.receive(
                subject="workflow.task.>",
                durable="worker-1",
                batch=10,
                timeout_sec=5.0,
            )
            for msg in msgs:
                print(f"[{msg.subject}] seq={msg.stream_seq}: {msg.payload}")
                await msg.ack()  # 手动确认
        """
        raw_messages = await self._fetch_raw_messages(
            subject,
            durable,
            batch=batch,
            timeout_sec=timeout_sec,
            auto_ack=ack,
        )

        messages: List[NatsMessage] = []
        for raw in raw_messages:
            data = json.loads(raw.data.decode())
            metadata = raw.metadata
            messages.append(
                NatsMessage(
                    subject=raw.subject,
                    payload=data,
                    stream=metadata.stream,
                    consumer=metadata.consumer,
                    stream_seq=metadata.sequence.stream,
                    consumer_seq=metadata.sequence.consumer,
                    _raw=raw,
                )
            )
        return messages

    async def receive_bytes(
        self,
        subject: str,
        durable: Optional[str],
        batch: int = 1,
        timeout_sec: float = 5.0,
        ack: bool = False,
    ) -> List[NatsJetStreamBinaryMessage]:
        """从 JetStream Pull Consumer 接收原始二进制消息。"""
        raw_messages = await self._fetch_raw_messages(
            subject,
            durable,
            batch=batch,
            timeout_sec=timeout_sec,
            auto_ack=ack,
        )
        messages = [self._binary_jetstream_message(raw) for raw in raw_messages]
        return messages

    async def receive_latest(
        self,
        subject: str,
        timeout_sec: float = 5.0,
        ack: bool = False,
    ) -> Optional[NatsMessage]:
        """
        使用临时 DeliverLast Consumer 读取调用时匹配的最后一条 JSON 消息。

        每次调用都创建新的临时 Consumer，避免复用 durable 后从旧消费位置
        继续。DeliverLast 只决定 Consumer 的起始位置，不会在持续消费期间
        自动跳过后来形成的积压。
        """
        raw_messages = await self._fetch_raw_messages(
            subject,
            durable=None,
            batch=1,
            timeout_sec=timeout_sec,
            deliver_policy=DeliverPolicy.LAST,
            auto_ack=ack,
        )
        if not raw_messages:
            return None
        raw = raw_messages[0]
        message = self._json_jetstream_message(raw)
        return message

    async def receive_latest_bytes(
        self,
        subject: str,
        timeout_sec: float = 5.0,
        ack: bool = False,
    ) -> Optional[NatsJetStreamBinaryMessage]:
        """使用临时 DeliverLast Consumer 读取最后一条原始二进制消息。"""
        raw_messages = await self._fetch_raw_messages(
            subject,
            durable=None,
            batch=1,
            timeout_sec=timeout_sec,
            deliver_policy=DeliverPolicy.LAST,
            auto_ack=ack,
        )
        if not raw_messages:
            return None
        raw = raw_messages[0]
        message = self._binary_jetstream_message(raw)
        return message

    @staticmethod
    def _json_jetstream_message(raw) -> NatsMessage:
        metadata = raw.metadata
        return NatsMessage(
            subject=raw.subject,
            payload=json.loads(raw.data.decode()),
            stream=metadata.stream,
            consumer=metadata.consumer,
            stream_seq=metadata.sequence.stream,
            consumer_seq=metadata.sequence.consumer,
            _raw=raw,
        )

    @staticmethod
    def _binary_jetstream_message(raw) -> NatsJetStreamBinaryMessage:
        metadata = raw.metadata
        return NatsJetStreamBinaryMessage(
            subject=raw.subject,
            data=bytes(raw.data),
            headers=raw.headers,
            stream=metadata.stream,
            consumer=metadata.consumer,
            stream_seq=metadata.sequence.stream,
            consumer_seq=metadata.sequence.consumer,
            _raw=raw,
        )

    async def _fetch_raw_messages(
        self,
        subject: str,
        durable: Optional[str],
        *,
        batch: int,
        timeout_sec: float,
        deliver_policy: Optional[DeliverPolicy] = None,
        auto_ack: bool = False,
    ) -> List[Any]:
        if batch <= 0:
            raise ValueError("batch must be positive")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        js, namespace = await self._jetstream_for_subject(subject)
        sub = await self._get_pull_subscription(
            subject,
            durable,
            js=js,
            namespace=namespace,
            deliver_policy=deliver_policy,
        )
        try:
            raw_messages = await sub.fetch(batch, timeout=timeout_sec)
            if auto_ack:
                for raw in raw_messages:
                    await raw.ack()
            return raw_messages
        except NatsTimeoutError:
            return []
        finally:
            if durable is None:
                try:
                    await sub.unsubscribe()
                except Exception:
                    logger.debug(
                        "注销临时 pull subscription 失败 subject=%s",
                        subject,
                        exc_info=True,
                    )

    async def _get_pull_subscription(
        self,
        subject: str,
        durable: Optional[str],
        js=None,
        namespace: str = "legacy",
        deliver_policy: Optional[DeliverPolicy] = None,
    ):
        context = js or self._js
        if durable is None:
            config = ConsumerConfig(
                inactive_threshold=self.ephemeral_consumer_inactive_sec,
                deliver_policy=deliver_policy or DeliverPolicy.ALL,
            )
            try:
                return await context.pull_subscribe(
                    subject,
                    durable=None,
                    config=config,
                )
            except BadRequestError as exc:
                if (
                    deliver_policy == DeliverPolicy.LAST
                    and exc.err_code == 10101
                ):
                    raise ValueError(
                        "receive_latest()/receive_latest_bytes() cannot use "
                        "a WorkQueue stream; use a dedicated Limits stream for "
                        "latest-state reads, or receive()/receive_bytes() for "
                        "reliable WorkQueue consumption"
                    ) from exc
                raise

        policy = (deliver_policy or DeliverPolicy.ALL).value
        key = (namespace, subject, durable, policy)
        async with self._pull_subscription_lock:
            subscription = self._pull_subscriptions.get(key)
            if subscription is None:
                kwargs = {"durable": durable}
                if deliver_policy is not None:
                    kwargs["config"] = ConsumerConfig(
                        deliver_policy=deliver_policy,
                    )
                subscription = await context.pull_subscribe(subject, **kwargs)
                self._pull_subscriptions[key] = subscription
                logger.info(
                    "已创建并缓存 pull subscription subject=%s durable=%s",
                    subject,
                    durable,
                )
            return subscription

    async def serve(
        self,
        subject: str,
        durable: str,
        handler: Callable[[Dict[str, Any]], Any],
        poll_timeout_sec: float = 5.0,
        max_inflight: int = 1,
        ack_progress_interval_sec: float = 10.0,
    ) -> None:
        """
        持续拉取并处理消息（流式消费者）。

        在无限循环中不断拉取消息，并将每条消息交由 handler 处理。
        处理成功自动 ACK，处理失败自动 NACK。
        此函数不会返回，适合用作长时间运行的任务。

        参数
        ----
        subject : str
            订阅的主题
        durable : str
            持久消费者名称（必须提供，用于断点续传）
        handler : Callable[[Dict[str, Any]], Any]
            消息处理函数。接收消息 payload（字典）。
            支持同步和异步（async）函数。
        poll_timeout_sec : float
            拉取超时时间（秒），超时后会重新拉取（默认 5 秒）
        max_inflight : int
            同一 worker 最多并发处理的消息数（默认 1）
        ack_progress_interval_sec : float
            处理期间发送 ACK 进度的间隔秒数，0 表示关闭（默认 10）

        示例
        ----
            async def task_handler(payload):
                print(f"处理任务: {payload['task_id']}")
                # 执行具体业务逻辑...
                await asyncio.sleep(1)

            await nc.serve(
                subject="workflow.task.>",
                durable="task-worker-1",
                handler=task_handler,
            )
        """
        if max_inflight <= 0:
            raise ValueError("max_inflight must be positive")
        if ack_progress_interval_sec < 0:
            raise ValueError("ack_progress_interval_sec cannot be negative")

        async def report_progress(message: NatsMessage) -> None:
            while True:
                await asyncio.sleep(ack_progress_interval_sec)
                try:
                    await message.in_progress()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "发送 ACK 进度失败 subject=%s error=%s",
                        subject,
                        exc,
                    )
                    return

        async def process(message: NatsMessage) -> None:
            progress_task = None
            if ack_progress_interval_sec > 0:
                progress_task = asyncio.create_task(report_progress(message))
            try:
                result = handler(message.payload)
                if asyncio.iscoroutine(result):
                    await result
                await message.ack()
            except Exception:
                logger.exception("消息处理失败 subject=%s payload=%s", subject, message.payload)
                try:
                    await message.nak()
                except Exception as nak_exc:
                    logger.warning("处理异常后 NACK 也失败: %s", nak_exc)
                raise
            finally:
                if progress_task:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)

        pending: Set[asyncio.Task] = set()
        try:
            while True:
                finished = {task for task in pending if task.done()}
                pending.difference_update(finished)
                for task in finished:
                    task.result()

                if len(pending) >= max_inflight:
                    finished, pending = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in finished:
                        task.result()
                    continue

                messages = await self.receive(
                    subject=subject,
                    durable=durable,
                    batch=max_inflight - len(pending),
                    timeout_sec=poll_timeout_sec,
                    ack=False,
                )
                for message in messages:
                    pending.add(asyncio.create_task(process(message)))
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def serve_workflow(
        self,
        agent_id: str,
        durable: str,
        handler: Callable[[Dict[str, Any]], Any],
        operation: str = "in",
        local_cluster: Optional[str] = None,
        instance_id: Optional[str] = None,
        poll_timeout_sec: float = 5.0,
        max_inflight: int = 1,
        ack_progress_interval_sec: float = 10.0,
        manage_stream_lifecycle: bool = True,
    ) -> None:
        """
        在同一连接上同时消费本地和跨集群持久化工作流消息。

        默认创建当前实例的 WF Stream，并在 close() 时删除。兼容旧控制器
        管理方式时传入 manage_stream_lifecycle=False。
        """
        cluster = self._local_cluster(local_cluster)
        instance = self._instance_id(instance_id)
        if manage_stream_lifecycle:
            await self.start_workflow_stream(
                agent_id=agent_id,
                local_cluster=cluster,
                instance_id=instance,
            )
        else:
            await self.wait_workflow_stream(
                agent_id=agent_id,
                local_cluster=cluster,
                instance_id=instance,
            )
        subjects = self.workflow_subscription_subjects(
            agent_id=agent_id,
            operation=operation,
            local_cluster=cluster,
            instance_id=instance,
        )
        tasks = [
            asyncio.create_task(
                self.serve(
                    subject=subject,
                    durable=f"{durable}-{scope}",
                    handler=handler,
                    poll_timeout_sec=poll_timeout_sec,
                    max_inflight=max_inflight,
                    ack_progress_interval_sec=ack_progress_interval_sec,
                )
            )
            for scope, subject in zip(("local", "global"), subjects)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _local_cluster(local_cluster: Optional[str] = None) -> str:
        configured = local_cluster or os.environ.get("CLUSTER_ID", "")
        if not configured:
            raise ValueError(
                "local cluster is required; pass local_cluster or set CLUSTER_ID"
            )
        return _subject_token(configured, "local_cluster")

    def frame_subject(
        self,
        target_cluster: str,
        agent_id: str,
        operation: str = "infer",
        local_cluster: Optional[str] = None,
    ) -> str:
        """根据目标集群生成本地或跨集群帧 subject。"""
        source_cluster = self._local_cluster(local_cluster)
        target = _subject_token(target_cluster, "target_cluster")
        agent = _subject_token(agent_id, "agent_id")
        action = _subject_token(operation, "operation")
        scope = "local" if target == source_cluster else "global"
        return f"frame.{scope}.{target}.{agent}.{action}"

    def memory_frame_subject(
        self,
        target_cluster: str,
        agent_id: str,
        target_instance_id: str,
        operation: str = "infer",
        local_cluster: Optional[str] = None,
    ) -> str:
        """生成指向一个实例级 Memory Stream 的帧 Subject。"""
        source_cluster = self._local_cluster(local_cluster)
        target = _subject_token(target_cluster, "target_cluster")
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(target_instance_id)
        action = _subject_token(operation, "operation")
        scope = "local" if target == source_cluster else "global"
        return (
            f"frame.{scope}.{target}.agent.{agent}."
            f"instance.{instance}.{action}"
        )

    def workflow_subject(
        self,
        target_cluster: str,
        agent_id: str,
        target_instance_id: str,
        operation: str = "in",
        local_cluster: Optional[str] = None,
    ) -> str:
        """根据目标集群生成本地或跨集群持久化工作流 subject。"""
        source_cluster = self._local_cluster(local_cluster)
        target = _subject_token(target_cluster, "target_cluster")
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(target_instance_id)
        action = _subject_token(operation, "operation")
        scope = "local" if target == source_cluster else "global"
        return (
            f"workflow.{scope}.{target}.agent.{agent}."
            f"instance.{instance}.{action}"
        )

    def workflow_subscription_subjects(
        self,
        agent_id: str,
        operation: str = "in",
        local_cluster: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> Tuple[str, str]:
        """返回当前 Agent 应消费的 local/global 持久化工作流 subject。"""
        cluster = self._local_cluster(local_cluster)
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(instance_id)
        action = _subject_token(operation, "operation")
        return (
            f"workflow.local.{cluster}.agent.{agent}.instance.{instance}.{action}",
            f"workflow.global.{cluster}.agent.{agent}.instance.{instance}.{action}",
        )

    async def provision_workflow_stream(
        self,
        target_cluster: str,
        agent_id: str,
        instance_id: str,
    ) -> Dict[str, Any]:
        """为目标实例创建或复用唯一的持久化 Workflow Stream。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(instance_id)
        subject = (
            f"workflow.global.{cluster}.agent.{agent}."
            f"instance.{instance}.in"
        )
        js, stream = await self._jetstream_for_subject(subject)
        async with self._routed_stream_lock:
            subjects = list(
                self.workflow_stream_subjects(cluster, agent, instance)
            )
            await ensure_jetstream_stream(
                js,
                name=stream,
                subjects=subjects,
                replace_subjects=True,
            )
            self._routed_streams_ready.add(stream)
        return {
            "stream": stream,
            "domain": cluster,
            "agent_id": agent,
            "instance_id": instance,
            "subjects": list(
                self.workflow_stream_subjects(cluster, agent, instance)
            ),
        }

    async def start_workflow_stream(
        self,
        agent_id: str,
        instance_id: Optional[str] = None,
        local_cluster: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建当前实例的 WF Stream，并绑定到本客户端生命周期。"""
        cluster = self._local_cluster(local_cluster)
        instance = self._instance_id(instance_id)
        async with self._managed_workflow_stream_lock:
            if self._closing:
                raise RuntimeError("NatsComm is closing")

        result = await self.provision_workflow_stream(
            target_cluster=cluster,
            agent_id=agent_id,
            instance_id=instance,
        )
        async with self._managed_workflow_stream_lock:
            closing = self._closing
            if not closing:
                self._managed_workflow_streams.add((cluster, instance))
        if closing:
            await self.delete_workflow_stream(
                target_cluster=cluster,
                instance_id=instance,
            )
            raise RuntimeError("NatsComm is closing")
        return result

    async def stop_workflow_stream(
        self,
        instance_id: Optional[str] = None,
        local_cluster: Optional[str] = None,
    ) -> bool:
        """提前删除并解除当前客户端管理的 Workflow Stream。"""
        cluster = self._local_cluster(local_cluster)
        instance = self._instance_id(instance_id)
        deleted = await self.delete_workflow_stream(
            target_cluster=cluster,
            instance_id=instance,
        )
        async with self._managed_workflow_stream_lock:
            self._managed_workflow_streams.discard((cluster, instance))
        return deleted

    async def wait_workflow_stream(
        self,
        agent_id: str,
        instance_id: Optional[str] = None,
        local_cluster: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        poll_interval_sec: float = 0.5,
    ) -> Dict[str, Any]:
        """兼容控制器管理模式，等待当前实例 Workflow Stream 就绪。"""
        cluster = self._local_cluster(local_cluster)
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(instance_id)
        stream = self.workflow_stream_name(instance)
        expected_subjects = set(
            self.workflow_stream_subjects(cluster, agent, instance)
        )
        timeout = (
            self.stream_provision_timeout_sec
            if timeout_sec is None
            else float(timeout_sec)
        )
        if timeout <= 0:
            raise ValueError("timeout_sec must be positive")
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")

        await self.connect(ensure_stream=False)
        async with self._routed_stream_lock:
            js = self._routed_js.get(cluster)
            if js is None:
                js = self._nc.jetstream(domain=cluster)
                self._routed_js[cluster] = js

        deadline = time.monotonic() + timeout
        last_error: Optional[Exception] = None
        while True:
            try:
                info = await js.stream_info(stream)
                actual_subjects = set(info.config.subjects or [])
                if actual_subjects != expected_subjects:
                    raise RuntimeError(
                        f"Stream {stream} subjects mismatch: "
                        f"expected={sorted(expected_subjects)}, "
                        f"actual={sorted(actual_subjects)}"
                    )
                self._routed_streams_ready.add(stream)
                return {
                    "stream": stream,
                    "domain": cluster,
                    "agent_id": agent,
                    "instance_id": instance,
                    "subjects": sorted(actual_subjects),
                }
            except NotFoundError as exc:
                last_error = exc
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for orchestrator to provision "
                    f"Stream {stream} in domain {cluster}"
                ) from last_error
            await asyncio.sleep(min(poll_interval_sec, remaining))

    async def delete_workflow_stream(
        self,
        target_cluster: str,
        instance_id: str,
    ) -> bool:
        """由编排器删除已结束实例的 Stream、consumer及未处理消息。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        instance = self._instance_id(instance_id)
        await self.connect(ensure_stream=False)
        async with self._routed_stream_lock:
            js = self._routed_js.get(cluster)
            if js is None:
                js = self._nc.jetstream(domain=cluster)
                self._routed_js[cluster] = js
            stream = self.workflow_stream_name(instance)
            try:
                await js.delete_stream(stream)
                deleted = True
            except NotFoundError:
                deleted = False
            self._routed_streams_ready.discard(stream)
            return deleted

    async def workflow_stream_status(
        self,
        target_cluster: str,
        instance_id: str,
    ) -> Dict[str, Any]:
        """查询一个实例 Stream 的存储量和 Consumer 积压。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        instance = self._instance_id(instance_id)
        await self.connect(ensure_stream=False)
        async with self._routed_stream_lock:
            js = self._routed_js.get(cluster)
            if js is None:
                js = self._nc.jetstream(domain=cluster)
                self._routed_js[cluster] = js
        stream = self.workflow_stream_name(instance)
        try:
            info = await js.stream_info(stream)
        except NotFoundError:
            return {
                "exists": False,
                "stream": stream,
                "domain": cluster,
                "instance_id": instance,
                "messages": 0,
                "bytes": 0,
                "consumer_count": 0,
                "num_pending": 0,
                "num_ack_pending": 0,
                "consumers": [],
            }

        consumer_infos = await js.consumers_info(stream)
        consumers = [
            {
                "name": consumer.name,
                "num_pending": consumer.num_pending,
                "num_ack_pending": consumer.num_ack_pending,
                "num_redelivered": consumer.num_redelivered,
            }
            for consumer in consumer_infos
        ]
        return {
            "exists": True,
            "stream": stream,
            "domain": cluster,
            "instance_id": instance,
            "created": _iso_timestamp(getattr(info, "created", None)),
            "subjects": list(info.config.subjects or []),
            "messages": info.state.messages,
            "bytes": info.state.bytes,
            "consumer_count": len(consumers),
            "num_pending": sum(item["num_pending"] for item in consumers),
            "num_ack_pending": sum(
                item["num_ack_pending"] for item in consumers
            ),
            "consumers": consumers,
        }

    async def list_workflow_streams(
        self,
        target_cluster: str,
    ) -> List[Dict[str, Any]]:
        """列出目标 edge domain 中由实例前缀标识的工作流 Stream。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        await self.connect(ensure_stream=False)
        async with self._routed_stream_lock:
            js = self._routed_js.get(cluster)
            if js is None:
                js = self._nc.jetstream(domain=cluster)
                self._routed_js[cluster] = js
        prefix = f"{self.workflow_stream_prefix}_"
        results = []
        for info in await js.streams_info():
            name = info.config.name
            if not name.startswith(prefix):
                continue
            results.append(
                {
                    "stream": name,
                    "domain": cluster,
                    "instance_id": name[len(prefix):],
                    "created": _iso_timestamp(
                        getattr(info, "created", None)
                    ),
                    "subjects": list(info.config.subjects or []),
                    "messages": info.state.messages,
                    "bytes": info.state.bytes,
                    "consumer_count": info.state.consumer_count,
                }
            )
        return sorted(results, key=lambda item: item["stream"])

    async def provision_memory_frame_stream(
        self,
        target_cluster: str,
        agent_id: str,
        instance_id: str,
    ) -> Dict[str, Any]:
        """由编排器为目标实例创建或更新独享的 Memory 帧 Stream。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(instance_id)
        stream = self.memory_frame_stream_name(instance)
        subjects = list(
            self.memory_frame_stream_subjects(cluster, agent, instance)
        )
        js = await self._jetstream_for_domain(cluster)
        await ensure_jetstream_stream(
            js,
            name=stream,
            subjects=subjects,
            storage="memory",
            replace_subjects=True,
            max_bytes=self.frame_stream_max_bytes,
            max_age=self.frame_stream_max_age_sec,
            max_msg_size=self.max_binary_payload_bytes + 64 * 1024,
            retention=RetentionPolicy.WORK_QUEUE,
            discard=DiscardPolicy.NEW,
        )
        self._routed_streams_ready.add(stream)
        return {
            "stream": stream,
            "domain": cluster,
            "storage": "memory",
            "agent_id": agent,
            "instance_id": instance,
            "subjects": subjects,
            "max_bytes": self.frame_stream_max_bytes,
            "max_age_sec": self.frame_stream_max_age_sec,
            "max_msg_size": self.max_binary_payload_bytes + 64 * 1024,
        }

    async def start_memory_frame_stream(
        self,
        agent_id: str,
        instance_id: Optional[str] = None,
        local_cluster: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建当前实例的 Memory 帧 Stream，并绑定到本客户端生命周期。

        该方法适用于不经过编排器启动的 Agent。Stream 创建成功后会登记为
        当前 NatsComm 自主管理资源，并在 close() 时幂等删除。
        """
        cluster = self._local_cluster(local_cluster)
        instance = self._instance_id(instance_id)
        async with self._managed_frame_stream_lock:
            if self._closing:
                raise RuntimeError("NatsComm is closing")

        result = await self.provision_memory_frame_stream(
            target_cluster=cluster,
            agent_id=agent_id,
            instance_id=instance,
        )
        async with self._managed_frame_stream_lock:
            closing = self._closing
            if not closing:
                self._managed_frame_streams.add((cluster, instance))
        if closing:
            await self.delete_memory_frame_stream(
                target_cluster=cluster,
                instance_id=instance,
            )
            raise RuntimeError("NatsComm is closing")
        return result

    async def stop_memory_frame_stream(
        self,
        instance_id: Optional[str] = None,
        local_cluster: Optional[str] = None,
    ) -> bool:
        """提前删除并解除当前客户端管理的 Memory 帧 Stream。"""
        cluster = self._local_cluster(local_cluster)
        instance = self._instance_id(instance_id)
        deleted = await self.delete_memory_frame_stream(
            target_cluster=cluster,
            instance_id=instance,
        )
        async with self._managed_frame_stream_lock:
            self._managed_frame_streams.discard((cluster, instance))
        return deleted

    async def wait_memory_frame_stream(
        self,
        agent_id: str,
        instance_id: Optional[str] = None,
        local_cluster: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        poll_interval_sec: float = 0.5,
    ) -> Dict[str, Any]:
        """等待编排器创建当前实例的 Memory 帧 Stream。"""
        cluster = self._local_cluster(local_cluster)
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(instance_id)
        stream = self.memory_frame_stream_name(instance)
        expected_subjects = set(
            self.memory_frame_stream_subjects(cluster, agent, instance)
        )
        timeout = (
            self.stream_provision_timeout_sec
            if timeout_sec is None
            else float(timeout_sec)
        )
        if timeout <= 0:
            raise ValueError("timeout_sec must be positive")
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")

        js = await self._jetstream_for_domain(cluster)
        deadline = time.monotonic() + timeout
        last_error: Optional[Exception] = None
        while True:
            try:
                info = await js.stream_info(stream)
                actual_subjects = set(info.config.subjects or [])
                if actual_subjects != expected_subjects:
                    raise RuntimeError(
                        f"Stream {stream} subjects mismatch: "
                        f"expected={sorted(expected_subjects)}, "
                        f"actual={sorted(actual_subjects)}"
                    )
                storage = getattr(
                    getattr(info.config, "storage", None),
                    "value",
                    getattr(info.config, "storage", None),
                )
                if storage != "memory":
                    raise RuntimeError(
                        f"Stream {stream} must use memory storage, "
                        f"actual={storage}"
                    )
                self._routed_streams_ready.add(stream)
                return {
                    "stream": stream,
                    "domain": cluster,
                    "storage": storage,
                    "agent_id": agent,
                    "instance_id": instance,
                    "subjects": sorted(actual_subjects),
                }
            except NotFoundError as exc:
                last_error = exc
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for orchestrator to provision "
                    f"Memory Stream {stream} in domain {cluster}"
                ) from last_error
            await asyncio.sleep(min(poll_interval_sec, remaining))

    async def delete_memory_frame_stream(
        self,
        target_cluster: str,
        instance_id: str,
    ) -> bool:
        """删除实例结束后不再需要的 Memory 帧 Stream。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        instance = self._instance_id(instance_id)
        stream = self.memory_frame_stream_name(instance)
        js = await self._jetstream_for_domain(cluster)
        try:
            await js.delete_stream(stream)
            deleted = True
        except NotFoundError:
            deleted = False
        self._routed_streams_ready.discard(stream)
        return deleted

    async def memory_frame_stream_status(
        self,
        target_cluster: str,
        instance_id: str,
    ) -> Dict[str, Any]:
        """查询一个实例 Memory 帧 Stream 的消息和 Consumer 积压。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        instance = self._instance_id(instance_id)
        stream = self.memory_frame_stream_name(instance)
        js = await self._jetstream_for_domain(cluster)
        try:
            info = await js.stream_info(stream)
        except NotFoundError:
            return {
                "exists": False,
                "stream": stream,
                "domain": cluster,
                "storage": "memory",
                "instance_id": instance,
                "messages": 0,
                "bytes": 0,
                "consumer_count": 0,
                "num_pending": 0,
                "num_ack_pending": 0,
                "consumers": [],
            }

        consumer_infos = await js.consumers_info(stream)
        consumers = [
            {
                "name": consumer.name,
                "num_pending": consumer.num_pending,
                "num_ack_pending": consumer.num_ack_pending,
                "num_redelivered": consumer.num_redelivered,
            }
            for consumer in consumer_infos
        ]
        storage = getattr(
            getattr(info.config, "storage", None),
            "value",
            getattr(info.config, "storage", None),
        )
        return {
            "exists": True,
            "stream": stream,
            "domain": cluster,
            "storage": storage,
            "instance_id": instance,
            "created": _iso_timestamp(getattr(info, "created", None)),
            "subjects": list(info.config.subjects or []),
            "messages": info.state.messages,
            "bytes": info.state.bytes,
            "consumer_count": len(consumers),
            "num_pending": sum(item["num_pending"] for item in consumers),
            "num_ack_pending": sum(
                item["num_ack_pending"] for item in consumers
            ),
            "consumers": consumers,
        }

    async def list_memory_frame_streams(
        self,
        target_cluster: str,
    ) -> List[Dict[str, Any]]:
        """列出目标 edge domain 中的实例级 Memory 帧 Stream。"""
        cluster = _subject_token(target_cluster, "target_cluster")
        js = await self._jetstream_for_domain(cluster)
        prefix = f"{self.frame_stream_prefix}_"
        results = []
        for info in await js.streams_info():
            name = info.config.name
            if not name.startswith(prefix):
                continue
            storage = getattr(
                getattr(info.config, "storage", None),
                "value",
                getattr(info.config, "storage", None),
            )
            results.append(
                {
                    "stream": name,
                    "domain": cluster,
                    "storage": storage,
                    "instance_id": name[len(prefix):],
                    "created": _iso_timestamp(
                        getattr(info, "created", None)
                    ),
                    "subjects": list(info.config.subjects or []),
                    "messages": info.state.messages,
                    "bytes": info.state.bytes,
                    "consumer_count": info.state.consumer_count,
                }
            )
        return sorted(results, key=lambda item: item["stream"])

    def frame_subscription_subjects(
        self,
        agent_id: str,
        operation: str = "infer",
        local_cluster: Optional[str] = None,
    ) -> Tuple[str, str]:
        """返回当前 Agent 应同时监听的 local/global 帧 subject。"""
        cluster = self._local_cluster(local_cluster)
        agent = _subject_token(agent_id, "agent_id")
        action = _subject_token(operation, "operation")
        return (
            f"frame.local.{cluster}.{agent}.{action}",
            f"frame.global.{cluster}.{agent}.{action}",
        )

    def memory_frame_subscription_subjects(
        self,
        agent_id: str,
        operation: str = "infer",
        local_cluster: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> Tuple[str, str]:
        """返回当前实例应拉取的 local/global Memory 帧 Subject。"""
        cluster = self._local_cluster(local_cluster)
        agent = _subject_token(agent_id, "agent_id")
        instance = self._instance_id(instance_id)
        action = _subject_token(operation, "operation")
        return (
            f"frame.local.{cluster}.agent.{agent}."
            f"instance.{instance}.{action}",
            f"frame.global.{cluster}.agent.{agent}."
            f"instance.{instance}.{action}",
        )

    async def send_workflow(
        self,
        target_cluster: str,
        agent_id: str,
        target_instance_id: str,
        payload: Dict[str, Any],
        operation: str = "in",
        local_cluster: Optional[str] = None,
    ) -> Dict[str, Any]:
        """自动选择 local/global JetStream 并发送持久化工作流消息。"""
        subject = self.workflow_subject(
            target_cluster=target_cluster,
            agent_id=agent_id,
            target_instance_id=target_instance_id,
            operation=operation,
            local_cluster=local_cluster,
        )
        return await self.send(subject, payload)

    async def send_workflow_and_wait(
        self,
        target_cluster: str,
        agent_id: str,
        target_instance_id: str,
        payload: Dict[str, Any],
        operation: str = "in",
        local_cluster: Optional[str] = None,
        reply_subject: Optional[str] = None,
        timeout_sec: float = 30.0,
    ) -> Dict[str, Any]:
        """自动选择 local/global JetStream，持久化任务并等待 Core NATS 回复。"""
        subject = self.workflow_subject(
            target_cluster=target_cluster,
            agent_id=agent_id,
            target_instance_id=target_instance_id,
            operation=operation,
            local_cluster=local_cluster,
        )
        return await self.send_and_wait(
            subject=subject,
            payload=payload,
            reply_subject=reply_subject,
            timeout_sec=timeout_sec,
        )

    async def request_memory_frame(
        self,
        target_cluster: str,
        agent_id: str,
        target_instance_id: str,
        payload: BinaryPayload,
        operation: str = "infer",
        local_cluster: Optional[str] = None,
        timeout_sec: float = 30.0,
        request_id: Optional[str] = None,
    ) -> bytes:
        """
        将二进制帧写入目标实例的 JetStream Memory Stream，并等待处理回复。

        JetStream PubAck 只确认帧已进入目标 Stream；本方法随后继续等待接收
        Agent 通过 Core NATS Inbox 返回业务处理结果。
        """
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        encoded = self._encode_binary_payload(payload)
        subject = self.memory_frame_subject(
            target_cluster=target_cluster,
            agent_id=agent_id,
            target_instance_id=target_instance_id,
            operation=operation,
            local_cluster=local_cluster,
        )
        frame_id = _subject_token(
            request_id or uuid.uuid4().hex,
            "request_id",
        )
        await self.connect(ensure_stream=False)
        js = await self._jetstream_for_domain(target_cluster)
        reply_subject = self._nc.new_inbox()
        subscription = await self._nc.subscribe(
            reply_subject,
            max_msgs=1,
            pending_msgs_limit=1,
            pending_bytes_limit=self.binary_pending_bytes,
        )
        await self._nc.flush(timeout=min(5.0, timeout_sec))

        started = time.monotonic()
        try:
            await js.publish(
                subject,
                encoded,
                timeout=timeout_sec,
                stream=self.memory_frame_stream_name(target_instance_id),
                headers={
                    _FRAME_REPLY_HEADER: reply_subject,
                    _FRAME_REQUEST_ID_HEADER: frame_id,
                },
            )
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(
                    f"Memory 帧处理超时 subject={subject} request_id={frame_id}"
                )
            try:
                response = await subscription.next_msg(timeout=remaining)
            except NatsTimeoutError as exc:
                raise TimeoutError(
                    f"Memory 帧处理超时 subject={subject} request_id={frame_id}"
                ) from exc
        finally:
            try:
                await subscription.unsubscribe()
            except Exception:
                logger.debug("注销 Memory 帧回复订阅失败", exc_info=True)

        response_id = ""
        if response.headers:
            response_id = response.headers.get(
                _FRAME_REQUEST_ID_HEADER,
                "",
            )
        if response_id and response_id != frame_id:
            raise RuntimeError(
                f"Memory 帧回复 ID 不匹配: expected={frame_id}, "
                f"actual={response_id}"
            )
        result = self._encode_binary_payload(response.data)
        logger.info(
            "JetStream Memory 帧请求完成 subject=%s request_id=%s "
            "request_bytes=%d response_bytes=%d elapsed_ms=%.1f",
            subject,
            frame_id,
            len(encoded),
            len(result),
            (time.monotonic() - started) * 1000,
        )
        return result

    async def serve_memory_frames(
        self,
        agent_id: str,
        handler: Callable[[NatsMemoryFrameMessage], Any],
        operation: str = "infer",
        local_cluster: Optional[str] = None,
        instance_id: Optional[str] = None,
        durable: Optional[str] = None,
        poll_timeout_sec: float = 5.0,
        max_inflight: int = 1,
        ack_progress_interval_sec: float = 10.0,
        reply_timeout_sec: float = 5.0,
        manage_stream_lifecycle: bool = True,
    ) -> None:
        """
        持续拉取当前实例的 local/global Memory 帧，处理后回复并 ACK。

        handler 返回 bytes-like 响应。handler、回复发布或 ACK 任一环节失败
        都会 NAK 输入帧，由 JetStream 按 Consumer 策略重新投递。

        默认在启动时创建当前实例 Stream，并在 close() 时删除。由控制器管理
        Stream 时传入 manage_stream_lifecycle=False，仅等待 Stream 就绪。
        """
        if poll_timeout_sec <= 0:
            raise ValueError("poll_timeout_sec must be positive")
        if max_inflight <= 0:
            raise ValueError("max_inflight must be positive")
        if ack_progress_interval_sec <= 0:
            raise ValueError("ack_progress_interval_sec must be positive")
        if reply_timeout_sec <= 0:
            raise ValueError("reply_timeout_sec must be positive")

        cluster = self._local_cluster(local_cluster)
        instance = self._instance_id(instance_id)
        if manage_stream_lifecycle:
            await self.start_memory_frame_stream(
                agent_id=agent_id,
                instance_id=instance,
                local_cluster=cluster,
            )
        else:
            await self.wait_memory_frame_stream(
                agent_id=agent_id,
                instance_id=instance,
                local_cluster=cluster,
            )
        js = await self._jetstream_for_domain(cluster)
        stream = self.memory_frame_stream_name(instance)
        subjects = self.memory_frame_subscription_subjects(
            agent_id=agent_id,
            operation=operation,
            local_cluster=cluster,
            instance_id=instance,
        )
        durable_base = durable or f"{self.frame_stream_prefix}_{instance}"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", durable_base):
            raise ValueError(
                "durable may contain only ASCII letters, digits, '-' and '_'"
            )
        subscriptions = []
        for scope, subject in zip(("local", "global"), subjects):
            subscriptions.append(
                await js.pull_subscribe(
                    subject,
                    durable=f"{durable_base}-{scope}",
                    stream=stream,
                    config=ConsumerConfig(
                        ack_wait=self.frame_ack_wait_sec,
                        max_deliver=self.frame_max_deliver,
                        max_ack_pending=max_inflight,
                    ),
                    pending_msgs_limit=max(1, max_inflight),
                    pending_bytes_limit=self.binary_pending_bytes,
                )
            )

        semaphore = asyncio.Semaphore(max_inflight)

        async def report_progress(raw_message) -> None:
            while True:
                await asyncio.sleep(ack_progress_interval_sec)
                await raw_message.in_progress()

        async def process(raw_message) -> None:
            async with semaphore:
                metadata = raw_message.metadata
                headers = raw_message.headers or {}
                reply_subject = headers.get(_FRAME_REPLY_HEADER, "")
                request_id = headers.get(_FRAME_REQUEST_ID_HEADER, "")
                message = NatsMemoryFrameMessage(
                    subject=raw_message.subject,
                    data=bytes(raw_message.data),
                    reply_subject=reply_subject,
                    request_id=request_id,
                    headers=headers,
                    stream=metadata.stream,
                    consumer=metadata.consumer,
                    stream_seq=metadata.sequence.stream,
                    delivered=metadata.num_delivered,
                    _raw=raw_message,
                )
                progress_task = asyncio.create_task(
                    report_progress(raw_message)
                )
                try:
                    result = handler(message)
                    if inspect.isawaitable(result):
                        result = await result
                    encoded = self._encode_binary_payload(
                        b"" if result is None else result
                    )
                    if reply_subject:
                        await self._nc.publish(
                            reply_subject,
                            encoded,
                            headers={
                                _FRAME_REQUEST_ID_HEADER: request_id,
                            },
                        )
                        await self._nc.flush(timeout=reply_timeout_sec)
                    await raw_message.ack_sync(timeout=reply_timeout_sec)
                    logger.info(
                        "Memory 帧处理完成 subject=%s request_id=%s "
                        "bytes=%d delivered=%d",
                        raw_message.subject,
                        request_id or "-",
                        len(raw_message.data),
                        metadata.num_delivered,
                    )
                except asyncio.CancelledError:
                    try:
                        await raw_message.nak()
                    except Exception:
                        logger.debug(
                            "取消 Memory 帧处理后 NAK 失败",
                            exc_info=True,
                        )
                    raise
                except Exception:
                    logger.exception(
                        "Memory 帧处理失败，等待 JetStream 重投 "
                        "subject=%s request_id=%s delivered=%d",
                        raw_message.subject,
                        request_id or "-",
                        metadata.num_delivered,
                    )
                    try:
                        await raw_message.nak(delay=1)
                    except Exception:
                        logger.warning(
                            "Memory 帧处理失败后 NAK 也失败",
                            exc_info=True,
                        )
                finally:
                    progress_task.cancel()
                    await asyncio.gather(
                        progress_task,
                        return_exceptions=True,
                    )

        async def consume(subscription) -> None:
            while True:
                try:
                    messages = await subscription.fetch(
                        max_inflight,
                        timeout=poll_timeout_sec,
                    )
                except NatsTimeoutError:
                    continue
                await asyncio.gather(
                    *(process(message) for message in messages)
                )

        tasks = [
            asyncio.create_task(consume(subscription))
            for subscription in subscriptions
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for subscription in subscriptions:
                try:
                    await subscription.unsubscribe()
                except Exception:
                    logger.debug(
                        "注销 Memory 帧 Pull Consumer 失败",
                        exc_info=True,
                    )

    async def publish_bytes(
        self,
        subject: str,
        payload: BinaryPayload,
        timeout_sec: float = 5.0,
    ) -> None:
        """通过 Core NATS 发布原始 bytes，不执行 JSON 编码或 JetStream 持久化。"""
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        await self.connect(ensure_stream=False)
        encoded = self._encode_binary_payload(payload)
        started = time.monotonic()
        await self._nc.publish(subject, encoded)
        await self._nc.flush(timeout=timeout_sec)
        logger.info(
            "Core NATS 二进制发布完成 subject=%s bytes=%d elapsed_ms=%.1f",
            subject,
            len(encoded),
            (time.monotonic() - started) * 1000,
        )

    async def respond_bytes(
        self,
        reply_subject: str,
        payload: BinaryPayload,
        timeout_sec: float = 5.0,
    ) -> None:
        """向 Core NATS request 携带的回复 subject 返回原始 bytes。"""
        if not reply_subject:
            raise ValueError("reply_subject is required")
        await self.publish_bytes(reply_subject, payload, timeout_sec=timeout_sec)

    async def request_bytes(
        self,
        subject: str,
        payload: BinaryPayload,
        timeout_sec: float = 30.0,
    ) -> bytes:
        """通过 Core NATS 发送原始 bytes，并等待一条原始 bytes 回复。"""
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        await self.connect(ensure_stream=False)
        encoded = self._encode_binary_payload(payload)
        started = time.monotonic()
        try:
            message = await self._nc.request(
                subject,
                encoded,
                timeout=timeout_sec,
            )
        except NatsTimeoutError as exc:
            raise TimeoutError(f"二进制请求超时 subject={subject}") from exc
        response = self._encode_binary_payload(message.data)
        logger.info(
            "Core NATS 二进制请求完成 subject=%s request_bytes=%d "
            "response_bytes=%d elapsed_ms=%.1f",
            subject,
            len(encoded),
            len(response),
            (time.monotonic() - started) * 1000,
        )
        return response

    async def subscribe_bytes(
        self,
        subject: str,
        handler: Callable[[NatsBinaryMessage], Any],
        queue: Optional[str] = None,
    ):
        """
        在复用连接上注册一个常驻 Core NATS 二进制订阅。

        handler 可以直接调用 message.respond()，也可以返回 bytes 由本方法自动回复。
        相同 subject 和 queue 的重复调用会返回已创建的订阅。
        """
        await self.connect(ensure_stream=False)
        queue_name = queue or ""
        key = (subject, queue_name)
        async with self._binary_subscription_lock:
            subscription = self._binary_subscriptions.get(key)
            if subscription is not None:
                return subscription

            async def callback(raw_message):
                message = NatsBinaryMessage(
                    subject=raw_message.subject,
                    data=bytes(raw_message.data),
                    reply_subject=raw_message.reply or "",
                    headers=raw_message.headers,
                    _raw=raw_message,
                    _max_payload_bytes=self.max_binary_payload_bytes,
                )
                try:
                    result = handler(message)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        await message.respond(self._encode_binary_payload(result))
                except Exception:
                    logger.exception(
                        "Core NATS 二进制消息处理失败 subject=%s",
                        subject,
                    )

            subscription = await self._nc.subscribe(
                subject,
                queue=queue_name,
                cb=callback,
                pending_msgs_limit=self.binary_pending_msgs,
                pending_bytes_limit=self.binary_pending_bytes,
            )
            await self._nc.flush(timeout=5.0)
            self._binary_subscriptions[key] = subscription
            logger.info(
                "已创建并缓存 Core NATS 二进制订阅 subject=%s queue=%s",
                subject,
                queue_name or "-",
            )
            return subscription

    async def request_frame_bytes(
        self,
        target_cluster: str,
        agent_id: str,
        payload: BinaryPayload,
        operation: str = "infer",
        local_cluster: Optional[str] = None,
        timeout_sec: float = 30.0,
    ) -> bytes:
        """按目标集群自动选择 local/global subject 并发送二进制帧请求。"""
        subject = self.frame_subject(
            target_cluster=target_cluster,
            agent_id=agent_id,
            operation=operation,
            local_cluster=local_cluster,
        )
        return await self.request_bytes(subject, payload, timeout_sec=timeout_sec)

    async def subscribe_frame_bytes(
        self,
        agent_id: str,
        handler: Callable[[NatsBinaryMessage], Any],
        operation: str = "infer",
        local_cluster: Optional[str] = None,
        queue: Optional[str] = None,
        max_inflight: Optional[int] = None,
    ) -> Tuple[Any, Any]:
        """在同一连接上同时监听当前 Agent 的 local/global 二进制帧 subject。"""
        concurrency = (
            max_inflight
            if max_inflight is not None
            else int(os.environ.get("NATS_MAX_INFLIGHT", "1"))
        )
        if concurrency <= 0:
            raise ValueError("max_inflight must be positive")
        semaphore = asyncio.Semaphore(concurrency)

        async def routed_handler(message: NatsBinaryMessage):
            async with semaphore:
                result = handler(message)
                if inspect.isawaitable(result):
                    result = await result
                return result

        subjects = self.frame_subscription_subjects(
            agent_id=agent_id,
            operation=operation,
            local_cluster=local_cluster,
        )
        local_subscription = await self.subscribe_bytes(
            subjects[0],
            handler=routed_handler,
            queue=queue,
        )
        global_subscription = await self.subscribe_bytes(
            subjects[1],
            handler=routed_handler,
            queue=queue,
        )
        return local_subscription, global_subscription

    async def request(
        self,
        subject: str,
        payload: Dict[str, Any],
        timeout_sec: float = 30.0,
    ) -> Dict[str, Any]:
        """
        发送请求并等待响应（请求-响应模式）。

        基于 NATS 原生 Request-Reply 机制，发送消息后自动等待
        一个 reply subject 上的回复。注意此方法不使用 JetStream，
        而是使用 NATS 核心（Core NATS）的即时通信能力。

        参数
        ----
        subject : str
            请求主题
        payload : Dict[str, Any]
            请求载荷
        timeout_sec : float
            等待响应的超时时间（秒），默认 30 秒

        返回
        ----
        Dict[str, Any]
            响应的 JSON 数据

        抛出
        -----
        TimeoutError
            在指定超时内未收到响应

        示例
        ----
            # 客户端
            result = await nc.request(
                "workflow.rpc.build",
                {"repo": "my-app", "branch": "main"},
                timeout_sec=10.0,
            )
            print(f"构建结果: {result}")
        """
        await self.connect(ensure_stream=False)

        try:
            msg = await self._nc.request(
                subject,
                self._encode_control_payload(payload),
                timeout=timeout_sec,
            )
        except NatsTimeoutError as exc:
            raise TimeoutError(f"请求超时 subject={subject}") from exc

        return json.loads(msg.data.decode())

    async def publish_core(
        self,
        subject: str,
        payload: Dict[str, Any],
        timeout_sec: float = 5.0,
    ) -> None:
        """
        通过 Core NATS 发布短生命周期消息。

        调用方应使用不属于任何 Stream 的 subject（例如 `_INBOX.*`）；
        如果 subject 被 Stream 捕获，普通 Core NATS publish 仍会被持久化。
        """
        await self.connect(ensure_stream=False)
        encoded = self._encode_control_payload(payload)
        started = time.monotonic()
        await self._nc.publish(subject, encoded)
        await self._nc.flush(timeout=timeout_sec)
        logger.info(
            "Core NATS 发布完成 subject=%s bytes=%d elapsed_ms=%.1f",
            subject,
            len(encoded),
            (time.monotonic() - started) * 1000,
        )

    async def send_and_wait(
        self,
        subject: str,
        payload: Dict[str, Any],
        reply_subject: Optional[str] = None,
        timeout_sec: float = 30.0,
    ) -> Dict[str, Any]:
        """
        将任务写入 JetStream，并通过预先建立的 Core NATS 订阅等待回复。

        回复订阅会在任务发布前创建，收到一条回复或超时后自动注销，
        不会创建 JetStream 临时 consumer。
        """
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        await self.connect(ensure_stream=False)
        reply_subject = reply_subject or self._nc.new_inbox()
        request_payload = dict(payload)
        configured_reply = request_payload.get("reply_subject")
        if configured_reply and configured_reply != reply_subject:
            raise ValueError(
                "payload reply_subject does not match send_and_wait reply_subject"
            )
        request_payload["reply_subject"] = reply_subject
        subscription = await self._nc.subscribe(reply_subject, max_msgs=1)
        await self._nc.flush(timeout=min(timeout_sec, 5.0))
        started = time.monotonic()
        try:
            ack = await self.send(subject, request_payload)
            logger.info(
                "JetStream 请求已发布 subject=%s reply_subject=%s stream=%s seq=%s",
                subject,
                reply_subject,
                ack["stream"],
                ack["seq"],
            )
            try:
                message = await subscription.next_msg(timeout=timeout_sec)
            except NatsTimeoutError as exc:
                raise TimeoutError(
                    f"等待回复超时 subject={subject} reply_subject={reply_subject}"
                ) from exc
            logger.info(
                "收到 Core NATS 回复 reply_subject=%s bytes=%d elapsed_ms=%.1f",
                reply_subject,
                len(message.data),
                (time.monotonic() - started) * 1000,
            )
            return json.loads(message.data.decode())
        finally:
            try:
                await subscription.unsubscribe()
            except Exception:
                logger.debug(
                    "注销回复 subscription 失败 subject=%s",
                    reply_subject,
                    exc_info=True,
                )

    async def respond(
        self,
        subject: str,
        handler: Callable[[Dict[str, Any]], Any],
        queue: Optional[str] = None,
    ) -> None:
        """
        注册请求处理器，响应指定主题上的请求（请求-响应模式）。

        订阅指定主题，当收到请求时调用 handler 处理并发送响应。
        注意此方法使用 Core NATS 订阅（非 JetStream），
        不会自动创建或使用流。

        参数
        ----
        subject : str
            要监听的主题
        handler : Callable[[Dict[str, Any]], Any]
            请求处理函数。接收请求 payload，返回响应数据。
            支持同步和异步函数。
            如果处理函数抛出异常，会返回 {"error": str(exc)}。
        queue : Optional[str]
            队列组名称。同一队列组中的多个服务实例会负载均衡。
            相同 queue 名称的订阅者中，每条消息只被其中一个处理。

        示例
        ----
            def calc_handler(payload):
                return {"result": payload["x"] + payload["y"]}

            # 注册 RPC 服务
            await nc.respond("workflow.rpc.calc", handler=calc_handler)

            # 注册带队列组的服务（水平扩展）
            await nc.respond(
                "workflow.rpc.build",
                handler=build_handler,
                queue="build-workers",
            )
        """
        await self.connect(ensure_stream=False)

        async def _callback(msg):
            try:
                payload = json.loads(msg.data.decode())
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    result = await result
                await msg.respond(self._encode_control_payload(result or {}))
            except Exception as exc:
                logger.exception("请求处理失败 subject=%s", subject)
                await msg.respond(
                    self._encode_control_payload({"error": str(exc)})
                )

        await self._nc.subscribe(subject, queue=queue, cb=_callback)
        while True:
            await asyncio.sleep(3600)
