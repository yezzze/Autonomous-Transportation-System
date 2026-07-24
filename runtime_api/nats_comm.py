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

类说明
------
- NatsComm    : 核心通信客户端类
- NatsMessage : 消息封装类，提供 ACK/NACK/进度/终止等操作

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
    NATS_STREAM               默认流名称（默认 WORKFLOW）
    NATS_STREAM_SUBJECTS      默认流主题（默认 workflow.>）
    NATS_JETSTREAM_DOMAIN     JetStream 域（默认 hub）
    NATS_SEND_DELAY_SECONDS   发送前延迟秒数（默认 0）
    NATS_SEND_DELAY_FILE      运行时延迟配置文件（默认 /tmp/nats_send_delay_seconds）
    NATS_CONTROL_MAX_BYTES    NATS 控制消息上限（默认 1MiB）
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from nats.aio.client import Client as NATS
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import ConsumerConfig
from runtime_api.jetstream_stream import ensure_jetstream_stream

logger = logging.getLogger(__name__)


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
        使用的 JetStream 流名称，默认从环境变量 NATS_STREAM 读取
    stream_subjects : Optional[List[str]]
        流关联的主题列表，默认从环境变量 NATS_STREAM_SUBJECTS 读取
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
    ):
        """
        初始化 NatsComm 实例。

        所有参数都有默认值，大多数场景只需 `NatsComm()` 即可。

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
        self.stream = stream or os.environ.get("NATS_STREAM", "WORKFLOW")
        self.stream_subjects = stream_subjects or self._stream_subjects_from_env()
        self.jetstream_domain = jetstream_domain or os.environ.get("NATS_JETSTREAM_DOMAIN", "hub")
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
        self._nc = NATS()
        self._js = None
        self._connect_lock = asyncio.Lock()
        self._pull_subscription_lock = asyncio.Lock()
        self._pull_subscriptions: Dict[Tuple[str, str], Any] = {}
        self.ephemeral_consumer_inactive_sec = float(
            os.environ.get("NATS_EPHEMERAL_CONSUMER_INACTIVE_SEC", "30")
        )
        if self.ephemeral_consumer_inactive_sec <= 0:
            raise ValueError("NATS_EPHEMERAL_CONSUMER_INACTIVE_SEC must be positive")

    def _encode_control_payload(self, payload: Dict[str, Any]) -> bytes:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        if len(encoded) > self.max_control_payload_bytes:
            raise ValueError(
                f"NATS control payload is {len(encoded)} bytes, exceeding "
                f"NATS_CONTROL_MAX_BYTES={self.max_control_payload_bytes}; "
                "send large binary data over gRPC and publish only its reference"
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
    def _stream_subjects_from_env() -> List[str]:
        """
        从环境变量 NATS_STREAM_SUBJECTS 读取流主题列表。

        返回
        ----
        List[str]
            主题列表，默认 ["workflow.>"]
        """
        raw = os.environ.get("NATS_STREAM_SUBJECTS", "workflow.>")
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
        获取 JetStream 上下文对象。

        根据 jetstream_domain 选择是否带域参数创建。
        域（Domain）是 NATS 超级集群（Super-Cluster）中的逻辑分区。
        """
        if self.jetstream_domain:
            return self._nc.jetstream(domain=self.jetstream_domain)
        return self._nc.jetstream()

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
        async with self._connect_lock:
            if not self._nc.is_connected:
                await self._nc.connect(
                    servers=self.servers,
                    connect_timeout=5,           # 连接超时 5 秒
                    reconnect_time_wait=2,       # 重连间隔 2 秒
                    max_reconnect_attempts=10,   # 最多重试 10 次
                    error_cb=self._on_error,
                    disconnected_cb=self._on_disconnected,
                    reconnected_cb=self._on_reconnected,
                    closed_cb=self._on_closed,
                )
                logger.info("NATS 已连接 servers=%s", self.servers)
            if ensure_stream and self._js is None:
                self._js = self._jetstream()
                await self._ensure_stream()

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

    async def close(self) -> None:
        """
        关闭与 NATS 的连接。

        使用 drain() 优雅关闭，等待所有未完成的消息处理完毕。
        建议在应用退出时调用。
        """
        async with self._pull_subscription_lock:
            subscriptions = list(self._pull_subscriptions.values())
            self._pull_subscriptions.clear()
        for subscription in subscriptions:
            try:
                await subscription.unsubscribe()
            except Exception:
                logger.debug("关闭 pull subscription 失败", exc_info=True)
        if self._nc.is_connected:
            await self._nc.drain()

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
        await self.connect()
        # 模拟网络时延
        send_delay = self.get_send_delay()
        if send_delay > 0:
            await asyncio.sleep(send_delay)
        encoded = self._encode_control_payload(payload)
        ack = await self._js.publish(subject, encoded)
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
        await self.connect()
        sub = await self._get_pull_subscription(subject, durable)

        try:
            raw_messages = await sub.fetch(batch, timeout=timeout_sec)
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
            if ack:
                await raw.ack()

        return messages

    async def _get_pull_subscription(self, subject: str, durable: Optional[str]):
        if durable is None:
            config = ConsumerConfig(
                inactive_threshold=self.ephemeral_consumer_inactive_sec
            )
            return await self._js.pull_subscribe(
                subject,
                durable=None,
                config=config,
            )

        key = (subject, durable)
        async with self._pull_subscription_lock:
            subscription = self._pull_subscriptions.get(key)
            if subscription is None:
                subscription = await self._js.pull_subscribe(
                    subject,
                    durable=durable,
                )
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
        await self.connect()
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
