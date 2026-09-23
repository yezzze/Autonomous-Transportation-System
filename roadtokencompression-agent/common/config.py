from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent.parent


DEFAULT_CONFIG: dict[str, Any] = {
    "runtime": {
        "mock": False,
        "request_timeout_seconds": 900,
        "random_seed": 20260730,
    },
    "paths": {
        "work_dir": "./work",
    },
    "models": {
        "videomae_model": "./weights/videomae/backbone",
        "videomae_checkpoint": "./weights/videomae/checkpoint_epoch_11.pth",
        "mobilenet_checkpoint": "./weights/mobilenet/model_best.pth",
        "mobilenet_inference_script": "./scripts/03_infer_video.py",
        "qwen": "./weights/qwen/Qwen2.5-VL-3B-Instruct",
        "compression_checkpoint": "./weights/compression/best_compression_module.pth",
        "embedding_model": "./weights/embedding/text2vec-base-chinese",
        "kb_path_1": "./assets/kb_accident_law",
        "kb_path_2": "./assets/kb_accident_law_1",
    },
    "vehicle": {
        "detector_mode": "videomae",
        "window_frames": 16,
        "stride_frames": 8,
        "anomaly_threshold": 0.6,
        "padding_seconds": 2.0,
        "mobilenet_sample_fps": 2.0,
        "mobilenet_batch_size": 32,
        "mobilenet_decoder": "auto",
        "mobilenet_clip_ratio": 0.2,
        "mobilenet_smooth_window": 0,
        "mobilenet_video_encoder": "auto",
        "mobilenet_fp16": True,
        "mobilenet_transport_mode": "clip",
        "mobilenet_cached_frame_size": 224,
        "mobilenet_transport_sample_fps": 1.0,
        "oracle_detection_ms": 500.0,
        "oracle_clip_seconds": 10.0,
    },
    "road": {
        "host": "0.0.0.0",
        "port": 5101,
        "description_frames": 8,
        "max_frame_size": 768,
        "use_compression": True,
        "token_keep_ratio": 0.9,
        "token_quantization": "none",
    },
    "cloud": {
        "host": "0.0.0.0",
        "port": 5102,
        "summary_max_new_tokens": 256,
        "restore_mode": "zero_fill",
        "enable_rag": True,
        "description_only": False,
    },
    "network": {
        "road_url": "http://127.0.0.1:5101",
        "cloud_url": "http://127.0.0.1:5102",
        "simulate": False,
        "include_return_path": True,
        "wifi": {
            "bandwidth_mbps": 100.0,
            "one_way_latency_ms": 10.0,
        },
        "wired": {
            "bandwidth_mbps": 1000.0,
            "one_way_latency_ms": 1.0,
        },
    },
    "benchmark": {
        "repeats": 3,
        "warmup_runs": 1,
        "normal_reference": "未检测到交通事故。",
        "min_efficiency_improvement_percent": 10.0,
        "max_driving_score_drop_percent": 2.0,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    path: str | Path,
    *,
    role: str = "all",
) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        user_config = json.load(handle)
    config = _deep_merge(DEFAULT_CONFIG, user_config)
    config["_config_path"] = str(config_path)
    _resolve_relative_paths(config, config_path.parent)
    validate_config(config, role=role)
    return config


def _resolve_relative_paths(config: dict[str, Any], base_dir: Path) -> None:
    work_dir = Path(config["paths"]["work_dir"]).expanduser()
    if not work_dir.is_absolute():
        work_dir = base_dir / work_dir
    config["paths"]["work_dir"] = str(work_dir.resolve())

    path_keys = (
        "videomae_model",
        "videomae_checkpoint",
        "mobilenet_checkpoint",
        "mobilenet_inference_script",
        "compression_checkpoint",
        "embedding_model",
        "kb_path_1",
        "kb_path_2",
    )
    for key in path_keys:
        value = str(config["models"][key])
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            config["models"][key] = str(candidate)
        elif value.startswith(".") or (base_dir / candidate).exists():
            config["models"][key] = str((base_dir / candidate).resolve())

    qwen_value = str(config["models"]["qwen"])
    qwen_candidate = Path(qwen_value).expanduser()
    if qwen_candidate.is_absolute():
        config["models"]["qwen"] = str(qwen_candidate)
    elif qwen_value.startswith(".") or (base_dir / qwen_candidate).exists():
        config["models"]["qwen"] = str((base_dir / qwen_candidate).resolve())


def validate_config(config: dict[str, Any], *, role: str = "all") -> None:
    keep_ratio = float(config["road"]["token_keep_ratio"])
    if not 0.0 < keep_ratio <= 1.0:
        raise ValueError("road.token_keep_ratio 必须位于 (0, 1] 区间")
    if int(config["benchmark"]["repeats"]) <= 0:
        raise ValueError("benchmark.repeats 必须大于0")
    if config["cloud"]["restore_mode"] != "zero_fill":
        raise ValueError("当前 cloud.restore_mode 仅支持 zero_fill")
    if config["vehicle"]["detector_mode"] not in {
        "videomae",
        "oracle_fixed_clip",
        "mobilenet_peak20",
    }:
        raise ValueError(
            "vehicle.detector_mode 必须为 videomae、oracle_fixed_clip或mobilenet_peak20"
        )
    if float(config["vehicle"]["mobilenet_sample_fps"]) <= 0:
        raise ValueError("vehicle.mobilenet_sample_fps 必须大于0")
    if int(config["vehicle"]["mobilenet_batch_size"]) <= 0:
        raise ValueError("vehicle.mobilenet_batch_size 必须大于0")
    if not 0.0 < float(config["vehicle"]["mobilenet_clip_ratio"]) <= 1.0:
        raise ValueError("vehicle.mobilenet_clip_ratio 必须位于 (0, 1] 区间")
    if config["vehicle"]["mobilenet_transport_mode"] not in {
        "clip",
        "cached_frames",
        "segment_uniform_frames",
    }:
        raise ValueError(
            "vehicle.mobilenet_transport_mode 必须为 clip、cached_frames"
            "或segment_uniform_frames"
        )
    if int(config["vehicle"]["mobilenet_cached_frame_size"]) < 224:
        raise ValueError("vehicle.mobilenet_cached_frame_size 不得小于224")
    if float(config["vehicle"]["mobilenet_transport_sample_fps"]) <= 0:
        raise ValueError("vehicle.mobilenet_transport_sample_fps 必须大于0")
    if config["road"]["token_quantization"] not in {"none", "int8"}:
        raise ValueError("road.token_quantization 必须为 none 或 int8")
    if float(config["vehicle"]["oracle_detection_ms"]) < 0:
        raise ValueError("vehicle.oracle_detection_ms 不得小于0")
    if float(config["vehicle"]["oracle_clip_seconds"]) <= 0:
        raise ValueError("vehicle.oracle_clip_seconds 必须大于0")
    for link_name in ("wifi", "wired"):
        link = config["network"][link_name]
        if float(link["bandwidth_mbps"]) <= 0:
            raise ValueError(
                f"network.{link_name}.bandwidth_mbps 必须大于0"
            )
        if float(link["one_way_latency_ms"]) < 0:
            raise ValueError(
                f"network.{link_name}.one_way_latency_ms 不得小于0"
            )

    if config["runtime"]["mock"]:
        return

    detector_path_keys = (
        ("mobilenet_checkpoint", "mobilenet_inference_script")
        if config["vehicle"]["detector_mode"] == "mobilenet_peak20"
        else ("videomae_model", "videomae_checkpoint")
    )
    role_path_keys = {
        "vehicle": detector_path_keys,
        "road": ("qwen", "compression_checkpoint"),
        "cloud": ("qwen", "embedding_model", "kb_path_1", "kb_path_2"),
        "evaluation": ("embedding_model",),
        "common": (),
        "all": (
            *detector_path_keys,
            "qwen",
            "compression_checkpoint",
            "embedding_model",
            "kb_path_1",
            "kb_path_2",
        ),
    }
    if role not in role_path_keys:
        raise ValueError(f"未知部署角色：{role}")

    missing = []
    for key in role_path_keys[role]:
        if key == "compression_checkpoint" and not config["road"]["use_compression"]:
            continue
        raw_value = str(config["models"][key])
        value = Path(raw_value).expanduser()
        if key in {"qwen", "embedding_model"} and not value.is_absolute():
            # 允许直接使用Hugging Face模型名；离线交接配置使用绝对本地路径。
            continue
        if not value.exists():
            missing.append(f"models.{key}: {value}")
    if missing:
        raise FileNotFoundError("以下模型资源不存在：\n" + "\n".join(missing))


def ensure_work_dirs(config: dict[str, Any]) -> Path:
    work_dir = Path(config["paths"]["work_dir"]).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir
