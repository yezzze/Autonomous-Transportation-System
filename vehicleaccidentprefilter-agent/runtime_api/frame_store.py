from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, Iterator, Optional


_FRAME_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class FrameStoreError(RuntimeError):
    pass


class FrameNotFound(FrameStoreError):
    pass


class FrameTooLarge(FrameStoreError):
    pass


class FrameIntegrityError(FrameStoreError):
    pass


@dataclass(frozen=True)
class StoredFrame:
    frame_id: str
    path: Path
    size_bytes: int
    sha256: str
    content_type: str
    created_at: float


class FrameStore:
    """Bounded, TTL-based local spool for frames transferred over gRPC."""

    def __init__(
        self,
        root: str,
        max_frame_bytes: int,
        max_store_bytes: int,
        ttl_seconds: float,
    ) -> None:
        if max_frame_bytes <= 0 or max_store_bytes <= 0:
            raise ValueError("frame store byte limits must be positive")
        if max_frame_bytes > max_store_bytes:
            raise ValueError("max_frame_bytes cannot exceed max_store_bytes")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_frame_bytes = max_frame_bytes
        self.max_store_bytes = max_store_bytes
        self.ttl_seconds = ttl_seconds
        self._frames: Dict[str, StoredFrame] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "FrameStore":
        return cls(
            root=os.environ.get("FRAME_STORE_DIR", "/tmp/agent-frames"),
            max_frame_bytes=int(os.environ.get("FRAME_MAX_BYTES", str(64 * 1024 * 1024))),
            max_store_bytes=int(os.environ.get("FRAME_STORE_MAX_BYTES", str(512 * 1024 * 1024))),
            ttl_seconds=float(os.environ.get("FRAME_TTL_SECONDS", "120")),
        )

    @staticmethod
    def validate_frame_id(frame_id: str) -> str:
        if not _FRAME_ID_RE.fullmatch(frame_id or ""):
            raise FrameStoreError("frame_id must match [A-Za-z0-9._-]{1,128}")
        return frame_id

    def put(
        self,
        frame_id: str,
        chunks: Iterable[bytes],
        content_type: str = "application/octet-stream",
        expected_size: int = 0,
        expected_sha256: str = "",
    ) -> StoredFrame:
        frame_id = self.validate_frame_id(frame_id)
        part_path = self.root / f".{frame_id}.{uuid.uuid4().hex}.part"
        final_path = self.root / f"{frame_id}.frame"
        digest = hashlib.sha256()
        size_bytes = 0

        try:
            with part_path.open("xb") as output:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise FrameStoreError("frame chunks must be bytes")
                    if not chunk:
                        continue
                    size_bytes += len(chunk)
                    if size_bytes > self.max_frame_bytes:
                        raise FrameTooLarge(
                            f"frame exceeds FRAME_MAX_BYTES={self.max_frame_bytes}"
                        )
                    output.write(chunk)
                    digest.update(chunk)

            actual_sha256 = digest.hexdigest()
            if size_bytes == 0:
                raise FrameIntegrityError("frame is empty")
            if expected_size and size_bytes != expected_size:
                raise FrameIntegrityError(
                    f"frame size mismatch: expected={expected_size}, actual={size_bytes}"
                )
            if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
                raise FrameIntegrityError("frame sha256 mismatch")

            stored = StoredFrame(
                frame_id=frame_id,
                path=final_path,
                size_bytes=size_bytes,
                sha256=actual_sha256,
                content_type=content_type or "application/octet-stream",
                created_at=time.time(),
            )
            with self._lock:
                self._cleanup_locked()
                existing = self._frames.get(frame_id)
                if existing:
                    if (
                        existing.size_bytes == stored.size_bytes
                        and existing.sha256 == stored.sha256
                    ):
                        return existing
                    raise FrameStoreError(f"frame already exists: {frame_id}")
                self._make_room_locked(size_bytes)
                os.replace(str(part_path), str(final_path))
                self._frames[frame_id] = stored
            return stored
        finally:
            try:
                part_path.unlink()
            except FileNotFoundError:
                pass

    def get(self, frame_id: str) -> StoredFrame:
        frame_id = self.validate_frame_id(frame_id)
        with self._lock:
            self._cleanup_locked()
            stored = self._frames.get(frame_id)
            if stored is None or not stored.path.is_file():
                self._frames.pop(frame_id, None)
                raise FrameNotFound(f"frame not found: {frame_id}")
            return stored

    def open(self, frame_id: str) -> BinaryIO:
        return self.get(frame_id).path.open("rb")

    def iter_chunks(self, frame_id: str, chunk_size: int) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        with self.open(frame_id) as source:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    def delete(self, frame_id: str) -> bool:
        frame_id = self.validate_frame_id(frame_id)
        with self._lock:
            stored = self._frames.pop(frame_id, None)
            path = stored.path if stored else self.root / f"{frame_id}.frame"
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked()

    def _cleanup_locked(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        expired = [
            frame_id
            for frame_id, stored in self._frames.items()
            if stored.created_at < cutoff or not stored.path.is_file()
        ]
        for frame_id in expired:
            stored = self._frames.pop(frame_id)
            try:
                stored.path.unlink()
            except FileNotFoundError:
                pass
        return len(expired)

    def _make_room_locked(self, additional_bytes: int) -> None:
        if additional_bytes <= 0:
            return
        current_bytes = sum(frame.size_bytes for frame in self._frames.values())
        if current_bytes + additional_bytes > self.max_store_bytes:
            raise FrameTooLarge(
                f"frame store exceeds FRAME_STORE_MAX_BYTES={self.max_store_bytes}"
            )
