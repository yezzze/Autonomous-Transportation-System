"""JetStream stream defaults: max-bytes, discard, retention (shared by NatsComm and APIs)."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = "5GB"
_DEFAULT_DISCARD = "old"
_DEFAULT_RETENTION = "limits"
_DEFAULT_STORAGE = "file"


def parse_bytes(value: str) -> int:
    """Parse NATS-style size strings (5GB, 512MiB, plain integer bytes)."""
    raw = (value or "").strip()
    if not raw:
        return 5 * 1024**3

    if raw.isdigit():
        return int(raw)

    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$", raw)
    if not match:
        raise ValueError(f"invalid byte size: {value!r}")

    amount = float(match.group(1))
    unit = match.group(2).upper()
    if unit in {"B", ""}:
        return int(amount)
    if unit in {"KB", "KIB"}:
        base = 1024 if unit == "KIB" else 1000
        return int(amount * base)
    if unit in {"MB", "MIB"}:
        base = 1024**2 if unit == "MIB" else 1000**2
        return int(amount * base)
    if unit in {"GB", "GIB"}:
        base = 1024**3 if unit in {"GB", "GIB"} else 1000**3
        return int(amount * base)
    if unit in {"TB", "TIB"}:
        base = 1024**4 if unit == "TIB" else 1000**4
        return int(amount * base)
    raise ValueError(f"unsupported byte unit in {value!r}")


def stream_name_from_env() -> str:
    return os.getenv("NATS_STREAM", "WORKFLOW").strip() or "WORKFLOW"


def stream_subjects_from_env() -> List[str]:
    raw = os.getenv("NATS_STREAM_SUBJECTS", "workflow.>")
    subjects = [item.strip() for item in raw.split(",") if item.strip()]
    return subjects or ["workflow.>"]


def _retention_from_env() -> RetentionPolicy:
    raw = os.getenv("NATS_STREAM_RETENTION", _DEFAULT_RETENTION).strip().lower()
    if raw in {"interest", "interestpolicy"}:
        return RetentionPolicy.INTEREST
    if raw in {"work", "workqueue", "workqueuepolicy"}:
        return RetentionPolicy.WORK_QUEUE
    return RetentionPolicy.LIMITS


def _discard_from_env() -> DiscardPolicy:
    raw = os.getenv("NATS_STREAM_DISCARD", _DEFAULT_DISCARD).strip().lower()
    if raw in {"new", "discardnew"}:
        return DiscardPolicy.NEW
    return DiscardPolicy.OLD


def _storage_from_env(storage: Optional[str]) -> StorageType:
    raw = (storage or os.getenv("NATS_STREAM_STORAGE", _DEFAULT_STORAGE)).strip().lower()
    if raw == "memory":
        return StorageType.MEMORY
    return StorageType.FILE


def build_stream_config(
    name: str,
    subjects: List[str],
    storage: Optional[str] = None,
) -> StreamConfig:
    """Build StreamConfig equivalent to: --max-bytes 5GB --discard old --retention limits."""
    return StreamConfig(
        name=name,
        subjects=subjects,
        retention=_retention_from_env(),
        max_bytes=parse_bytes(os.getenv("NATS_STREAM_MAX_BYTES", _DEFAULT_MAX_BYTES)),
        discard=_discard_from_env(),
        storage=_storage_from_env(storage),
    )


def merge_stream_subjects(current: List[str], required: List[str]) -> List[str]:
    merged = list(current or [])
    for subject in required:
        if subject and subject not in merged:
            merged.append(subject)
    return merged or list(required)


async def apply_stream_config(js, config: StreamConfig) -> None:
    """Update stream with full config (limits + subjects)."""
    await js.update_stream(config)


async def ensure_jetstream_stream(
    js,
    name: Optional[str] = None,
    subjects: Optional[List[str]] = None,
    storage: Optional[str] = None,
) -> Dict[str, Any]:
    """
  Ensure JetStream stream exists with env-driven limits.

  Returns metadata: created, updated, subjects, max_bytes, discard.
    """
    stream = name or stream_name_from_env()
    required = subjects or stream_subjects_from_env()
    config = build_stream_config(stream, required, storage=storage)

    try:
        info = await js.stream_info(stream)
    except NotFoundError:
        await js.add_stream(config)
        logger.info(
            "created JetStream stream %s subjects=%s max_bytes=%s discard=%s",
            stream,
            required,
            config.max_bytes,
            config.discard,
        )
        return {
            "created": True,
            "updated": False,
            "subjects": required,
            "max_bytes": config.max_bytes,
            "discard": str(config.discard),
        }

    current = list(getattr(getattr(info, "config", None), "subjects", None) or [])
    merged = merge_stream_subjects(current, required)
    config = build_stream_config(stream, merged, storage=storage)

    await apply_stream_config(js, config)
    updated = merged != current or (
        getattr(info.config, "max_bytes", None) != config.max_bytes
        or str(getattr(info.config, "discard", "")) != str(config.discard)
    )
    if updated:
        logger.info(
            "updated JetStream stream %s subjects=%s max_bytes=%s discard=%s",
            stream,
            merged,
            config.max_bytes,
            config.discard,
        )
    return {
        "created": False,
        "updated": updated,
        "subjects": merged,
        "max_bytes": config.max_bytes,
        "discard": str(config.discard),
    }
