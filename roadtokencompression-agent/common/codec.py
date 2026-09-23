from __future__ import annotations

import base64
import zlib
from typing import Any

import numpy as np


CODEC_NAME = "zlib+base64+raw"


def pack_array(
    array: np.ndarray,
    *,
    quantization: str = "none",
) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array)
    quantization_metadata = None
    packed = contiguous
    if quantization == "int8":
        values = contiguous.astype(np.float32, copy=False)
        max_abs = float(np.max(np.abs(values))) if values.size else 0.0
        scale = max(max_abs / 127.0, np.finfo(np.float32).eps)
        packed = np.clip(np.rint(values / scale), -127, 127).astype(np.int8)
        quantization_metadata = {
            "scheme": "symmetric_int8",
            "scale": scale,
            "original_dtype": str(contiguous.dtype),
        }
    elif quantization != "none":
        raise ValueError(f"不支持的量化方式：{quantization}")

    compressed = zlib.compress(packed.tobytes(order="C"), level=3)
    result = {
        "codec": CODEC_NAME,
        "dtype": str(packed.dtype),
        "shape": list(packed.shape),
        "data": base64.b64encode(compressed).decode("ascii"),
    }
    if quantization_metadata is not None:
        result["quantization"] = quantization_metadata
    return result


def unpack_array(payload: dict[str, Any]) -> np.ndarray:
    if payload.get("codec") != CODEC_NAME:
        raise ValueError(f"不支持的数组编码：{payload.get('codec')}")
    raw = zlib.decompress(base64.b64decode(payload["data"]))
    array = np.frombuffer(raw, dtype=np.dtype(payload["dtype"]))
    expected = int(np.prod(payload["shape"], dtype=np.int64))
    if array.size != expected:
        raise ValueError(
            f"数组元素数量不匹配：实际{array.size}，期望{expected}"
        )
    array = array.reshape(payload["shape"]).copy()
    quantization = payload.get("quantization")
    if quantization is not None:
        if quantization.get("scheme") != "symmetric_int8":
            raise ValueError(
                f"不支持的量化格式：{quantization.get('scheme')}"
            )
        array = array.astype(np.float32) * float(quantization["scale"])
        array = array.astype(np.dtype(quantization["original_dtype"]))
    return array


def build_token_payload(
    *,
    vision_features: np.ndarray,
    input_ids: np.ndarray,
    attention_mask: np.ndarray | None,
    image_grid_thw: np.ndarray,
    compression: dict[str, Any] | None,
    num_frames: int,
    vision_quantization: str = "none",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "vision_features": pack_array(
            vision_features,
            quantization=vision_quantization,
        ),
        "input_ids": pack_array(input_ids),
        "attention_mask": (
            pack_array(attention_mask) if attention_mask is not None else None
        ),
        "image_grid_thw": pack_array(image_grid_thw),
        "compression": compression,
        "num_frames": int(num_frames),
    }


def decode_token_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("不支持的Token载荷版本")
    return {
        "vision_features": unpack_array(payload["vision_features"]),
        "input_ids": unpack_array(payload["input_ids"]),
        "attention_mask": (
            unpack_array(payload["attention_mask"])
            if payload.get("attention_mask") is not None
            else None
        ),
        "image_grid_thw": unpack_array(payload["image_grid_thw"]),
        "compression": payload.get("compression"),
        "num_frames": int(payload.get("num_frames", 0)),
    }


def json_size_bytes(payload: Any) -> int:
    import json

    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
