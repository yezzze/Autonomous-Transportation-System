import itertools
import threading
from typing import Callable, Optional

import grpc

from . import frame_transport_pb2, frame_transport_pb2_grpc
from .frame_store import (
    FrameIntegrityError,
    FrameNotFound,
    FrameStore,
    FrameStoreError,
    FrameTooLarge,
)


class FrameTransportService(frame_transport_pb2_grpc.FrameTransportServicer):
    def __init__(
        self,
        store: FrameStore,
        public_addr: str,
        chunk_size: int = 1024 * 1024,
        max_inflight_uploads: int = 4,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_inflight_uploads <= 0:
            raise ValueError("max_inflight_uploads must be positive")
        self.store = store
        self.public_addr = public_addr
        self.chunk_size = chunk_size
        self._upload_slots = threading.BoundedSemaphore(max_inflight_uploads)
        self._max_inflight_uploads = max_inflight_uploads
        self._log = logger or (lambda _message: None)

    def reference_for(self, stored):
        return frame_transport_pb2.FrameReference(
            transport="grpc",
            target=self.public_addr,
            frame_id=stored.frame_id,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            content_type=stored.content_type,
            chunk_size=self.chunk_size,
        )

    def UploadFrame(self, request_iterator, context):
        if not self._upload_slots.acquire(blocking=False):
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"too many concurrent uploads; limit={self._max_inflight_uploads}",
            )

        try:
            try:
                first = next(request_iterator)
            except StopIteration:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "empty frame stream")

            frame_id = first.frame_id
            expected_size = first.total_size
            expected_sha256 = first.sha256
            content_type = first.content_type or "application/octet-stream"

            def validated_chunks():
                for expected_index, chunk in enumerate(
                    itertools.chain((first,), request_iterator)
                ):
                    if not context.is_active():
                        raise FrameStoreError("upload cancelled")
                    if chunk.frame_id != frame_id:
                        raise FrameIntegrityError("frame_id changed during upload")
                    if chunk.chunk_index != expected_index:
                        raise FrameIntegrityError(
                            f"chunk index mismatch: expected={expected_index}, "
                            f"actual={chunk.chunk_index}"
                        )
                    if len(chunk.data) > self.chunk_size:
                        raise FrameTooLarge(
                            f"chunk exceeds FRAME_CHUNK_SIZE={self.chunk_size}"
                        )
                    yield bytes(chunk.data)

            stored = self.store.put(
                frame_id=frame_id,
                chunks=validated_chunks(),
                content_type=content_type,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            self._log(
                f"uploaded frame_id={stored.frame_id}, "
                f"bytes={stored.size_bytes}, sha256={stored.sha256}"
            )
            return self.reference_for(stored)
        except FrameTooLarge as exc:
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
        except (FrameIntegrityError, FrameStoreError) as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        finally:
            self._upload_slots.release()

    def DownloadFrame(self, request, context):
        try:
            stored = self.store.get(request.frame_id)
            if request.sha256 and request.sha256.lower() != stored.sha256.lower():
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "frame sha256 mismatch")
            if request.size_bytes and request.size_bytes != stored.size_bytes:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "frame size mismatch")

            for chunk_index, data in enumerate(
                self.store.iter_chunks(stored.frame_id, self.chunk_size)
            ):
                if not context.is_active():
                    return
                yield frame_transport_pb2.FrameChunk(
                    frame_id=stored.frame_id,
                    chunk_index=chunk_index,
                    data=data,
                    content_type=stored.content_type if chunk_index == 0 else "",
                    total_size=stored.size_bytes if chunk_index == 0 else 0,
                    sha256=stored.sha256 if chunk_index == 0 else "",
                )
        except FrameNotFound as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except FrameStoreError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

    def DeleteFrame(self, request, context):
        try:
            return frame_transport_pb2.DeleteFrameResponse(
                deleted=self.store.delete(request.frame_id)
            )
        except FrameStoreError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
