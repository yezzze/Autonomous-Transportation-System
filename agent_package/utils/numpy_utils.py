"""NumPy 数组编解码工具 — 用于 NATS 传输 numpy 数组"""
import base64
import json
import logging
from typing import Any, Dict, Union

import numpy as np

logger = logging.getLogger(__name__)


def encode_structured_numpy(data: Any) -> Any:
    """
    递归遍历字典/列表，将 numpy 数组编码为 base64 字典。
    格式: {"__numpy__": true, "shape": [...], "dtype": "...", "data": "base64..."}
    非 numpy 值原样返回。
    """
    if isinstance(data, dict):
        return {k: encode_structured_numpy(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [encode_structured_numpy(v) for v in data]
    if isinstance(data, np.ndarray):
        return {
            "__numpy__": True,
            "shape": list(data.shape),
            "dtype": str(data.dtype),
            "data": base64.b64encode(data.tobytes()).decode("ascii"),
        }
    return data


def decode_structured_numpy(data: Any) -> Any:
    """
    递归遍历字典/列表，将 __numpy__ 标记的字典还原为 numpy 数组。
    非标记值原样返回。
    """
    if isinstance(data, dict):
        if data.get("__numpy__"):
            arr = np.frombuffer(
                base64.b64decode(data["data"]),
                dtype=np.dtype(data["dtype"]),
            ).reshape(data["shape"])
            return arr
        return {k: decode_structured_numpy(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [decode_structured_numpy(v) for v in data]
    return data
