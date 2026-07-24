import asyncio
import hashlib
import inspect
import io
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

import grpc

from . import frame_transport_pb2, frame_transport_pb2_grpc
from .nats_comm import NatsComm

logger = logging.getLogger(__name__)


def frame_reference_to_dict(reference) -> Dict[str, Any]:
    return {
        "transport": reference.transport,
        "target": reference.target,
        "frame_id": reference.frame_id,
        "size_bytes": reference.size_bytes,
        "sha256": reference.sha256,
        "content_type": reference.content_type,
        "chunk_size": reference.chunk_size,
    }


def frame_reference_from_dict(reference: Dict[str, Any]):
    return frame_transport_pb2.FrameReference(
        transport=str(reference.get("transport", "")),
        target=str(reference.get("target", "")),
        frame_id=str(reference.get("frame_id", "")),
        size_bytes=int(reference.get("size_bytes", 0)),
        sha256=str(reference.get("sha256", "")),
        content_type=str(reference.get("content_type", "")),
        chunk_size=int(reference.get("chunk_size", 0)),
    )


@dataclass
class DownloadedFrame:
    path: Path
    size_bytes: int
    sha256: str
    content_type: str
    frame_ref: Dict[str, Any]

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.cleanup()


class FrameTransportClient:
    def __init__(
        self,
        upload_target: Optional[str] = None,
        allowed_targets: Optional[Iterable[str]] = None,
        download_dir: Optional[str] = None,
        chunk_size: Optional[int] = None,
        max_chunk_bytes: Optional[int] = None,
        upload_timeout_sec: Optional[float] = None,
        download_timeout_sec: Optional[float] = None,
        retry_attempts: Optional[int] = None,
        retry_delay_sec: Optional[float] = None,
    ) -> None:
        self.upload_target = upload_target or self._default_upload_target()
        self.allowed_targets = set(allowed_targets or self._allowed_targets_from_env())
        self.allowed_targets.add(self.upload_target)
        self.download_dir = Path(
            download_dir
            or os.environ.get("FRAME_DOWNLOAD_DIR", "/tmp/agent-frame-downloads")
        )
        self.chunk_size = chunk_size or int(
            os.environ.get("FRAME_CHUNK_SIZE", str(1024 * 1024))
        )
        self.max_chunk_bytes = max_chunk_bytes or int(
            os.environ.get("FRAME_MAX_CHUNK_BYTES", str(2 * 1024 * 1024))
        )
        self.upload_timeout_sec = upload_timeout_sec or float(
            os.environ.get("FRAME_UPLOAD_TIMEOUT_SEC", "120")
        )
        self.download_timeout_sec = download_timeout_sec or float(
            os.environ.get("FRAME_DOWNLOAD_TIMEOUT_SEC", "120")
        )
        self.retry_attempts = (
            retry_attempts
            if retry_attempts is not None
            else int(os.environ.get("FRAME_RETRY_ATTEMPTS", "3"))
        )
        self.retry_delay_sec = (
            retry_delay_sec
            if retry_delay_sec is not None
            else float(os.environ.get("FRAME_RETRY_DELAY_SEC", "0.2"))
        )
        if self.chunk_size <= 0 or self.chunk_size > self.max_chunk_bytes:
            raise ValueError("FRAME_CHUNK_SIZE must be positive and within maximum")
        if self.retry_attempts <= 0:
            raise ValueError("FRAME_RETRY_ATTEMPTS must be positive")
        self._channels = {}
        self._channel_lock = threading.Lock()

    @staticmethod
    def _default_upload_target() -> str:
        return (
            os.environ.get("FRAME_UPLOAD_TARGET")
            or os.environ.get("AGENT_GRPC_ADDR")
            or os.environ.get("FRAME_PUBLIC_ADDR")
            or "agent-grpc:50051"
        )

    def _allowed_targets_from_env(self) -> Set[str]:
        raw = os.environ.get("FRAME_ALLOWED_TARGETS", self.upload_target)
        return {target.strip() for target in raw.split(",") if target.strip()}

    def _stub(self, target: str):
        with self._channel_lock:
            channel = self._channels.get(target)
            if channel is None:
                options = [
                    (
                        "grpc.max_receive_message_length",
                        self.max_chunk_bytes + 64 * 1024,
                    ),
                    (
                        "grpc.max_send_message_length",
                        self.max_chunk_bytes + 64 * 1024,
                    ),
                ]
                channel = grpc.insecure_channel(target, options=options)
                self._channels[target] = channel
        return frame_transport_pb2_grpc.FrameTransportStub(channel)

    @staticmethod
    def _file_metadata(path: Path, read_size: int):
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as source:
            while True:
                data = source.read(read_size)
                if not data:
                    break
                size_bytes += len(data)
                digest.update(data)
        if size_bytes <= 0:
            raise ValueError("frame is empty")
        return size_bytes, digest.hexdigest()

    def _upload(
        self,
        chunks_factory,
        size_bytes: int,
        sha256: str,
        content_type: str,
        frame_id: Optional[str],
    ) -> Dict[str, Any]:
        frame_id = frame_id or uuid.uuid4().hex

        def requests():
            for chunk_index, data in enumerate(chunks_factory()):
                yield frame_transport_pb2.FrameChunk(
                    frame_id=frame_id,
                    chunk_index=chunk_index,
                    data=data,
                    content_type=content_type if chunk_index == 0 else "",
                    total_size=size_bytes if chunk_index == 0 else 0,
                    sha256=sha256 if chunk_index == 0 else "",
                )

        stub = self._stub(self.upload_target)
        last_error = None
        for attempt in range(self.retry_attempts):
            try:
                reference = stub.UploadFrame(
                    requests(),
                    timeout=self.upload_timeout_sec,
                    wait_for_ready=True,
                )
                return frame_reference_to_dict(reference)
            except grpc.RpcError as exc:
                last_error = exc
                retryable = exc.code() in {
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                }
                if not retryable or attempt + 1 >= self.retry_attempts:
                    raise
                time.sleep(self.retry_delay_sec * (2**attempt))
        raise last_error

    def upload_file(
        self,
        frame_path,
        content_type: str = "application/octet-stream",
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = Path(frame_path).resolve()
        size_bytes, sha256 = self._file_metadata(path, self.chunk_size)

        def chunks():
            with path.open("rb") as source:
                while True:
                    data = source.read(self.chunk_size)
                    if not data:
                        return
                    yield data

        return self._upload(chunks, size_bytes, sha256, content_type, frame_id)

    def upload_bytes(
        self,
        frame_bytes: bytes,
        content_type: str = "application/octet-stream",
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not frame_bytes:
            raise ValueError("frame is empty")
        size_bytes = len(frame_bytes)
        sha256 = hashlib.sha256(frame_bytes).hexdigest()

        def chunks():
            source = io.BytesIO(frame_bytes)
            while True:
                data = source.read(self.chunk_size)
                if not data:
                    return
                yield data

        return self._upload(chunks, size_bytes, sha256, content_type, frame_id)

    def _validate_reference(self, reference: Dict[str, Any]):
        if reference.get("transport") != "grpc":
            raise ValueError(
                f"unsupported frame transport: {reference.get('transport')!r}"
            )
        target = str(reference.get("target", ""))
        if target not in self.allowed_targets:
            raise ValueError(f"frame target is not allowed: {target!r}")
        frame_id = str(reference.get("frame_id", ""))
        size_bytes = int(reference.get("size_bytes", 0))
        sha256 = str(reference.get("sha256", "")).lower()
        chunk_size = int(reference.get("chunk_size", 0))
        if not frame_id:
            raise ValueError("missing frame_id")
        if size_bytes <= 0:
            raise ValueError("invalid frame size")
        if len(sha256) != 64:
            raise ValueError("invalid frame sha256")
        if chunk_size <= 0 or chunk_size > self.max_chunk_bytes:
            raise ValueError(
                f"invalid chunk size {chunk_size}; maximum={self.max_chunk_bytes}"
            )
        return target, frame_id, size_bytes, sha256

    def download(self, reference: Dict[str, Any]) -> DownloadedFrame:
        target, frame_id, expected_size, expected_sha256 = self._validate_reference(
            reference
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        temp_file = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="frame-",
            suffix=".bin",
            dir=str(self.download_dir),
            delete=False,
        )
        temp_path = Path(temp_file.name)
        digest = hashlib.sha256()
        size_bytes = 0
        expected_index = 0
        try:
            request = frame_reference_from_dict(reference)
            with temp_file:
                for chunk in self._stub(target).DownloadFrame(
                    request,
                    timeout=self.download_timeout_sec,
                    wait_for_ready=True,
                ):
                    if chunk.frame_id != frame_id:
                        raise ValueError("downloaded frame_id does not match reference")
                    if chunk.chunk_index != expected_index:
                        raise ValueError(
                            f"chunk index mismatch: expected={expected_index}, "
                            f"actual={chunk.chunk_index}"
                        )
                    data = bytes(chunk.data)
                    temp_file.write(data)
                    digest.update(data)
                    size_bytes += len(data)
                    expected_index += 1

            actual_sha256 = digest.hexdigest()
            if size_bytes != expected_size:
                raise ValueError(
                    f"frame size mismatch: expected={expected_size}, actual={size_bytes}"
                )
            if actual_sha256 != expected_sha256:
                raise ValueError("frame sha256 mismatch")
            return DownloadedFrame(
                path=temp_path,
                size_bytes=size_bytes,
                sha256=actual_sha256,
                content_type=str(reference.get("content_type", "")),
                frame_ref=dict(reference),
            )
        except Exception:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def delete(self, reference: Dict[str, Any]) -> bool:
        target, _frame_id, _size, _sha256 = self._validate_reference(reference)
        response = self._stub(target).DeleteFrame(
            frame_reference_from_dict(reference),
            timeout=min(self.download_timeout_sec, 30),
            wait_for_ready=True,
        )
        return response.deleted

    def close(self) -> None:
        with self._channel_lock:
            channels = list(self._channels.values())
            self._channels.clear()
        for channel in channels:
            channel.close()


class FrameComm:
    """Unified NATS control-plane and gRPC frame data-plane facade."""

    def __init__(
        self,
        nats: Optional[NatsComm] = None,
        transport: Optional[FrameTransportClient] = None,
        **transport_kwargs,
    ) -> None:
        self.nats = nats or NatsComm()
        self.transport = transport or FrameTransportClient(**transport_kwargs)

    async def _prepare_payload(
        self,
        payload: Dict[str, Any],
        frame_path=None,
        frame_bytes: Optional[bytes] = None,
        content_type: str = "application/octet-stream",
    ):
        if frame_path is not None and frame_bytes is not None:
            raise ValueError("provide frame_path or frame_bytes, not both")
        control_payload = dict(payload)
        if (
            "frame_ref" in control_payload
            and (frame_path is not None or frame_bytes is not None)
        ):
            raise ValueError("payload already contains frame_ref")
        reference = None
        if frame_path is not None:
            reference = await asyncio.to_thread(
                self.transport.upload_file,
                frame_path,
                content_type,
            )
        elif frame_bytes is not None:
            reference = await asyncio.to_thread(
                self.transport.upload_bytes,
                frame_bytes,
                content_type,
            )
        if reference:
            control_payload["frame_ref"] = reference
        return control_payload, reference

    async def send(
        self,
        subject: str,
        payload: Dict[str, Any],
        frame_path=None,
        frame_bytes: Optional[bytes] = None,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        control_payload, reference = await self._prepare_payload(
            payload,
            frame_path=frame_path,
            frame_bytes=frame_bytes,
            content_type=content_type,
        )
        try:
            result = await self.nats.send(subject, control_payload)
            if reference:
                result["frame_ref"] = reference
            return result
        except Exception:
            if reference:
                try:
                    await asyncio.to_thread(self.transport.delete, reference)
                except Exception:
                    logger.warning("failed to clean uploaded frame", exc_info=True)
            raise

    async def download(self, payload_or_reference: Dict[str, Any]) -> DownloadedFrame:
        reference = payload_or_reference.get("frame_ref", payload_or_reference)
        return await asyncio.to_thread(self.transport.download, reference)

    async def serve(
        self,
        subject: str,
        durable: str,
        handler,
        poll_timeout_sec: float = 5.0,
        max_inflight: int = 1,
        ack_progress_interval_sec: float = 10.0,
        download_frames: bool = False,
        delete_remote_frame: bool = False,
    ) -> None:
        async def wrapped(payload):
            downloaded = None
            enriched = dict(payload)
            try:
                if download_frames and payload.get("frame_ref"):
                    downloaded = await self.download(payload)
                    enriched.update(
                        {
                            "frame_path": str(downloaded.path),
                            "frame_size_bytes": downloaded.size_bytes,
                            "frame_sha256": downloaded.sha256,
                            "frame_content_type": downloaded.content_type,
                        }
                    )
                result = handler(enriched)
                if inspect.isawaitable(result):
                    result = await result
                if downloaded and delete_remote_frame:
                    await asyncio.to_thread(
                        self.transport.delete,
                        downloaded.frame_ref,
                    )
                return result
            finally:
                if downloaded:
                    downloaded.cleanup()

        await self.nats.serve(
            subject=subject,
            durable=durable,
            handler=wrapped,
            poll_timeout_sec=poll_timeout_sec,
            max_inflight=max_inflight,
            ack_progress_interval_sec=ack_progress_interval_sec,
        )

    async def receive(self, *args, **kwargs):
        return await self.nats.receive(*args, **kwargs)

    async def request(self, *args, **kwargs):
        return await self.nats.request(*args, **kwargs)

    async def publish_core(self, *args, **kwargs):
        return await self.nats.publish_core(*args, **kwargs)

    async def send_and_wait(
        self,
        subject: str,
        payload: Dict[str, Any],
        timeout_sec: float = 30.0,
        reply_subject: Optional[str] = None,
        frame_path=None,
        frame_bytes: Optional[bytes] = None,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        """
        上传可选帧、持久化任务引用，并通过 Core NATS inbox 等待回复。

        实例应在 Agent 进程启动时创建并在所有请求之间复用，这样 NATS
        连接和 gRPC channel 都不会按帧重复建立。
        """
        control_payload, _reference = await self._prepare_payload(
            payload,
            frame_path=frame_path,
            frame_bytes=frame_bytes,
            content_type=content_type,
        )
        return await self.nats.send_and_wait(
            subject=subject,
            payload=control_payload,
            reply_subject=reply_subject,
            timeout_sec=timeout_sec,
        )

    async def respond(self, *args, **kwargs):
        return await self.nats.respond(*args, **kwargs)

    async def close(self) -> None:
        self.transport.close()
        await self.nats.close()
