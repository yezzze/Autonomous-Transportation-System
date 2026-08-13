import numpy as np
import base64
from typing import Any

def encode_array_to_dict(array: np.ndarray) -> dict:
    return {"data": base64.b64encode(array.tobytes()).decode("utf-8"), "shape": array.shape, "dtype": str(array.dtype)}

def decode_array_from_dict(array_dict: dict) -> np.ndarray:
    data = base64.b64decode(array_dict["data"])
    return np.frombuffer(data, dtype=array_dict["dtype"]).reshape(array_dict["shape"])


def encode_structured_numpy(payload: Any) -> Any:
    """Recursively encode numpy arrays in a JSON-compatible structure."""
    if isinstance(payload, np.ndarray):
        return encode_array_to_dict(payload)
    if isinstance(payload, dict):
        return {key: encode_structured_numpy(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [encode_structured_numpy(value) for value in payload]
    if isinstance(payload, tuple):
        return [encode_structured_numpy(value) for value in payload]
    return payload


def decode_structured_numpy(payload: Any) -> Any:
    """Recursively decode dictionaries produced by encode_structured_numpy()."""
    if isinstance(payload, dict):
        if {"data", "shape", "dtype"}.issubset(payload):
            return decode_array_from_dict(payload)
        return {key: decode_structured_numpy(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [decode_structured_numpy(value) for value in payload]
    return payload
