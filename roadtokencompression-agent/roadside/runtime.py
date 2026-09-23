from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import numpy as np

from common.codec import build_token_payload, pack_array
from common.prompts import EVENT_PROMPT
from common.timing import record_time
from common.video import sample_frames_from_clips


class MockRoadRuntime:
    """不加载模型的协议测试运行时。"""

    def encode_clips(
        self, clip_paths: list[Path]
    ) -> tuple[dict[str, Any], dict[str, float], dict[str, Any]]:
        timings: dict[str, float] = {}
        with record_time(timings, "road_frame_sampling"):
            frame_count = max(1, len(clip_paths))
        with record_time(timings, "road_vision_encoding"):
            features = np.arange(32, dtype=np.float32).reshape(1, 8, 4)
        with record_time(timings, "road_token_selection"):
            compressed = features[:, :6, :]
            keep_indices = np.arange(6, dtype=np.int64)
            compression = {
                "enabled": True,
                "keep_indices": [pack_array(keep_indices)],
                "original_shapes": [[1, 8, 4]],
                "compressed_lengths": [6],
                "compression_ratio": 0.75,
            }
        payload = build_token_payload(
            vision_features=compressed,
            input_ids=np.array([[1, 2, 3]], dtype=np.int64),
            attention_mask=np.ones((1, 3), dtype=np.int64),
            image_grid_thw=np.array([[1, 2, 2]], dtype=np.int64),
            compression=compression,
            num_frames=frame_count,
        )
        metrics = {
            "original_visual_tokens": 8,
            "retained_visual_tokens": 6,
            "token_keep_ratio_actual": 0.75,
            "description_frame_count": frame_count,
        }
        return payload, timings, metrics


class RoadRuntime:
    """真实路侧运行时：抽帧、Qwen视觉编码和Token筛选。"""

    def __init__(self, config: dict[str, Any]):
        import torch
        from transformers import AutoModelForVision2Seq, AutoProcessor

        self.config = config
        self.torch = torch
        self.num_frames = int(config["road"]["description_frames"])
        self.max_frame_size = int(config["road"]["max_frame_size"])
        self.use_compression = bool(config["road"]["use_compression"])
        self.keep_ratio = float(config["road"]["token_keep_ratio"])
        self.token_quantization = str(config["road"]["token_quantization"])

        model_path = config["models"]["qwen"]
        self.processor = AutoProcessor.from_pretrained(
            model_path,
            trust_remote_code=True,
            local_files_only=True,
        )
        if torch.cuda.is_available():
            # 路侧只调用视觉编码器，不执行语言模型生成。先把完整模型放在
            # CPU，再仅将 visual 子模块移到 GPU，避免未使用的语言模型与
            # 云端 Qwen 同时占满单卡显存。
            self.model = AutoModelForVision2Seq.from_pretrained(
                model_path,
                device_map="cpu",
                torch_dtype=torch.float16,
                trust_remote_code=True,
                local_files_only=True,
            )
            if not hasattr(self.model, "visual"):
                raise RuntimeError("当前Qwen模型没有visual模块，无法在路侧提取视觉Token")
            self.model.visual.to(device="cuda", dtype=torch.float16)
        else:
            self.model = AutoModelForVision2Seq.from_pretrained(
                model_path,
                device_map="cpu",
                torch_dtype=torch.float32,
                trust_remote_code=True,
                local_files_only=True,
            )
        self.model.eval()

        if not hasattr(self.model, "visual"):
            raise RuntimeError("当前Qwen模型没有visual模块，无法在路侧提取视觉Token")
        self.visual_device = next(self.model.visual.parameters()).device

        self.compression_module = None
        if self.use_compression:
            from vendor.semantic_compression_module import SemanticCompressionModule

            self.compression_module = SemanticCompressionModule(
                embed_dim=2048,
                target_keep_ratio=self.keep_ratio,
            ).to(device=self.visual_device, dtype=self.model.visual.dtype)
            checkpoint = torch.load(
                config["models"]["compression_checkpoint"],
                map_location=self.visual_device,
            )
            state_dict = checkpoint.get("model_state_dict", checkpoint)
            self.compression_module.load_state_dict(state_dict, strict=True)
            self.compression_module.eval()

    def encode_clips(
        self, clip_paths: list[Path]
    ) -> tuple[dict[str, Any], dict[str, float], dict[str, Any]]:
        torch = self.torch
        timings: dict[str, float] = {}

        with record_time(timings, "road_frame_sampling"):
            frames = sample_frames_from_clips(
                clip_paths,
                total_frames=self.num_frames,
                max_size=self.max_frame_size,
            )
        if not frames:
            raise RuntimeError("路侧未能从事故片段提取关键帧")

        with record_time(timings, "road_preprocessing"):
            messages = [
                {
                    "role": "user",
                    "content": [
                        *[{"type": "image", "image": frame} for frame in frames],
                        {"type": "text", "text": EVENT_PROMPT},
                    ],
                }
            ]
            prompt = self.processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            processed = self.processor(
                text=[prompt],
                images=frames,
                return_tensors="pt",
            )
            pixel_values = processed["pixel_values"].to(self.visual_device)
            image_grid_thw = processed["image_grid_thw"].to(self.visual_device)

        with record_time(timings, "road_vision_encoding"):
            with torch.inference_mode():
                visual_features = self.model.visual(
                    pixel_values.to(dtype=self.model.visual.dtype),
                    grid_thw=image_grid_thw,
                )
            if visual_features.ndim == 2:
                visual_features = visual_features.unsqueeze(0)
            if visual_features.ndim != 3:
                raise RuntimeError(
                    f"视觉Token形状异常：{tuple(visual_features.shape)}"
                )

        original_tokens = int(visual_features.shape[1])
        compression: dict[str, Any] | None = None
        features_to_send = visual_features

        with record_time(timings, "road_token_selection"):
            if self.use_compression:
                assert self.compression_module is not None
                with torch.inference_mode():
                    result = self.compression_module.compress_per_image(
                        visual_features,
                        image_grid_thw,
                    )
                feature_parts = result["compressed_features_list"]
                features_to_send = torch.cat(feature_parts, dim=1)
                compression = {
                    "enabled": True,
                    "keep_indices": [
                        pack_array(indices.detach().cpu().numpy().astype(np.int64))
                        for indices in result["keep_indices_list"]
                    ],
                    "original_shapes": [
                        list(shape) for shape in result["original_shapes"]
                    ],
                    "compressed_lengths": [
                        int(part.shape[1]) for part in feature_parts
                    ],
                    "compression_ratio": float(result["compression_ratio"]),
                }

        retained_tokens = int(features_to_send.shape[1])
        attention_mask = processed.get("attention_mask")
        payload = build_token_payload(
            vision_features=features_to_send.detach().cpu().numpy(),
            input_ids=processed["input_ids"].cpu().numpy(),
            attention_mask=(
                attention_mask.cpu().numpy() if attention_mask is not None else None
            ),
            image_grid_thw=image_grid_thw.cpu().numpy(),
            compression=compression,
            num_frames=len(frames),
            vision_quantization=self.token_quantization,
        )
        metrics = {
            "original_visual_tokens": original_tokens,
            "retained_visual_tokens": retained_tokens,
            "token_keep_ratio_actual": (
                retained_tokens / original_tokens if original_tokens else 1.0
            ),
            "description_frame_count": len(frames),
            "token_quantization": self.token_quantization,
        }
        # 路端与云端在测试服务器上共享同一张GPU。载荷已经复制到CPU后，
        # 及时释放本次视觉编码临时张量，避免路端CUDA缓存挤占下一次云端视频推理。
        del processed, pixel_values, image_grid_thw
        del visual_features, features_to_send
        if "result" in locals():
            del result
        if "feature_parts" in locals():
            del feature_parts
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return payload, timings, metrics


def build_road_runtime(config: dict[str, Any]):
    if config["runtime"]["mock"]:
        return MockRoadRuntime()
    return RoadRuntime(config)
