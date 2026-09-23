from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from common.codec import decode_token_payload, unpack_array
from common.prompts import EVENT_PROMPT, NORMAL_ADVICE, NORMAL_DESCRIPTION
from common.timing import record_time


def sample_video_by_chunks(
    video_path: Path,
    *,
    chunk_seconds: float,
    frames_per_chunk: int,
    max_size: int,
) -> list[tuple[float, float, list[Any]]]:
    """覆盖整段视频分块采样，避免长视频只取少量全局帧而漏掉短事件。"""
    import cv2
    from PIL import Image

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    if total_frames <= 0 or fps <= 0:
        capture.release()
        raise RuntimeError(f"视频帧数或帧率无效：{video_path}")

    duration = total_frames / fps
    result: list[tuple[float, float, list[Any]]] = []
    start_seconds = 0.0
    try:
        while start_seconds < duration:
            end_seconds = min(duration, start_seconds + chunk_seconds)
            start_index = min(
                total_frames - 1,
                max(0, int(start_seconds * fps)),
            )
            end_index = min(
                total_frames - 1,
                max(start_index, int(end_seconds * fps) - 1),
            )
            count = max(
                1,
                min(int(frames_per_chunk), end_index - start_index + 1),
            )
            indices = np.linspace(
                start_index,
                end_index,
                count,
                dtype=np.int64,
            )
            frames: list[Any] = []
            for index in indices:
                capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
                ok, frame = capture.read()
                if not ok:
                    continue
                image = Image.fromarray(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                )
                width, height = image.size
                scale = min(
                    float(max_size) / width,
                    float(max_size) / height,
                    1.0,
                )
                if scale < 1.0:
                    image = image.resize(
                        (
                            max(1, int(width * scale)),
                            max(1, int(height * scale)),
                        )
                    )
                frames.append(image)
            if not frames:
                raise RuntimeError(
                    f"无法读取视频区间：{start_seconds:.1f}-"
                    f"{end_seconds:.1f}s"
                )
            result.append((start_seconds, end_seconds, frames))
            start_seconds = end_seconds
    finally:
        capture.release()

    return result


def infer_event_detected(description: str) -> bool:
    normalized = "".join(description.strip().lower().split())
    negative_phrases = (
        "未检测到交通事故",
        "未发生交通事故",
        "没有发生交通事故",
        "无交通事故",
        "未发现事故",
        "没有事故",
        "noaccident",
    )
    return not any(phrase in normalized for phrase in negative_phrases)


def restore_visual_features(decoded: dict[str, Any]) -> np.ndarray:
    features = np.asarray(decoded["vision_features"])
    if features.ndim == 2:
        features = features[None, ...]
    if features.ndim != 3 or features.shape[0] != 1:
        raise ValueError(f"视觉Token形状异常：{features.shape}")

    compression = decoded.get("compression")
    if not compression or not compression.get("enabled"):
        return features

    original_shapes = compression["original_shapes"]
    lengths = [int(value) for value in compression["compressed_lengths"]]
    packed_indices = compression["keep_indices"]
    if not (len(original_shapes) == len(lengths) == len(packed_indices)):
        raise ValueError("压缩元数据中的图片数量不一致")
    if sum(lengths) != features.shape[1]:
        raise ValueError(
            "压缩Token长度与compressed_lengths不一致："
            f"{features.shape[1]} != {sum(lengths)}"
        )

    restored_parts: list[np.ndarray] = []
    offset = 0
    for shape_value, length, packed in zip(
        original_shapes, lengths, packed_indices
    ):
        shape = tuple(int(value) for value in shape_value)
        if len(shape) != 3 or shape[0] != 1:
            raise ValueError(f"不支持的原始Token形状：{shape}")
        indices = unpack_array(packed).astype(np.int64).reshape(-1)
        if len(indices) != length:
            raise ValueError("保留索引数量与压缩Token数量不一致")
        part = features[:, offset : offset + length, :]
        restored = np.zeros(shape, dtype=features.dtype)
        restored[:, indices, :] = part
        restored_parts.append(restored)
        offset += length
    return np.concatenate(restored_parts, axis=1)


class MockCloudRuntime:
    def _mock_output(self, sample_id: str) -> dict[str, Any]:
        is_normal = "normal" in sample_id.lower()
        if is_normal:
            return {
                "event_detected": False,
                "description": NORMAL_DESCRIPTION,
                "advice": NORMAL_ADVICE,
                "regulations_count": 0,
            }
        return {
            "event_detected": True,
            "description": "检测到道路上的车辆发生碰撞事故。",
            "advice": "立即减速停车、开启危险报警灯并联系救援。",
            "regulations_count": 0,
        }

    def process_video(
        self, video_path: Path, sample_id: str
    ) -> tuple[dict[str, Any], dict[str, float]]:
        timings: dict[str, float] = {}
        with record_time(timings, "cloud_video_decode_and_sampling"):
            _ = video_path.stat().st_size
        with record_time(timings, "cloud_full_model_generation"):
            output = self._mock_output(sample_id)
        return output, timings

    def process_tokens(
        self, payload: dict[str, Any], sample_id: str
    ) -> tuple[dict[str, Any], dict[str, float]]:
        timings: dict[str, float] = {}
        with record_time(timings, "cloud_token_decode_and_restore"):
            decoded = decode_token_payload(payload)
            restore_visual_features(decoded)
        with record_time(timings, "cloud_token_model_generation"):
            output = self._mock_output(sample_id)
        return output, timings


class CloudRuntime:
    """真实云端运行时，复用原有Qwen模型、RAG和建议生成代码。"""

    def __init__(self, config: dict[str, Any]):
        import torch

        self.config = config
        self.torch = torch
        self.max_new_tokens = int(config["cloud"]["summary_max_new_tokens"])
        self.frame_count = int(config["road"]["description_frames"])
        self.max_frame_size = int(config["road"]["max_frame_size"])
        self.baseline_max_frame_size = int(
            config["cloud"].get(
                "baseline_max_frame_size",
                self.max_frame_size,
            )
        )
        self.baseline_chunk_seconds = float(
            config["cloud"].get("baseline_chunk_seconds", 10.0)
        )
        self.baseline_frames_per_chunk = int(
            config["cloud"].get(
                "baseline_frames_per_chunk",
                self.frame_count,
            )
        )
        self.baseline_video_fps = float(
            config["cloud"].get("baseline_video_fps", 1.0)
        )
        self.baseline_video_min_pixels = int(
            config["cloud"].get("baseline_video_min_pixels", 100352)
        )
        self.baseline_video_max_pixels = int(
            config["cloud"].get("baseline_video_max_pixels", 100352)
        )
        self.enable_rag = bool(config["cloud"]["enable_rag"])
        self.description_only = bool(config["cloud"]["description_only"])
        self._generation_lock = threading.Lock()

        from vendor.semantic_comm_server import SemanticCommServer

        self.core = SemanticCommServer(
            model_path=config["models"]["qwen"],
            kb_path_1=config["models"]["kb_path_1"],
            kb_path_2=config["models"]["kb_path_2"],
            embedder_path=config["models"]["embedding_model"],
            port=int(config["cloud"]["port"]),
        )
        self.model = self.core.model
        self.processor = self.core.processor
        self.device = self.core.device

        from qwen_vl_utils import process_vision_info

        self.process_vision_info = process_vision_info

    def _generate_advice(
        self,
        description: str,
        timings: dict[str, float],
    ) -> tuple[str, int]:
        regulations: list[str] = []
        if self.enable_rag:
            with record_time(timings, "cloud_rag_retrieval"):
                regulations = self.core.retrieve_regulations(description)
        with record_time(timings, "cloud_advice_generation"):
            advice = self.core.generate_advice(description, regulations)
        return advice, len(regulations)

    def _decode_generated(
        self,
        output_ids,
        input_length: int,
    ) -> str:
        generated = output_ids[:, input_length:]
        text = self.processor.batch_decode(
            generated,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]
        return text.strip()

    def process_video(
        self,
        video_path: Path,
        sample_id: str,
    ) -> tuple[dict[str, Any], dict[str, float]]:
        del sample_id
        torch = self.torch
        timings: dict[str, float] = {}

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video",
                        "video": str(video_path.resolve()),
                        "fps": self.baseline_video_fps,
                        "min_pixels": self.baseline_video_min_pixels,
                        "max_pixels": self.baseline_video_max_pixels,
                    },
                    {"type": "text", "text": EVENT_PROMPT},
                ],
            }
        ]

        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        with record_time(timings, "cloud_video_decode_and_sampling"):
            try:
                (
                    image_inputs,
                    video_inputs,
                    video_kwargs,
                ) = self.process_vision_info(
                    messages,
                    return_video_kwargs=True,
                )
            except TypeError:
                image_inputs, video_inputs = self.process_vision_info(
                    messages
                )
                video_kwargs = {"fps": [self.baseline_video_fps]}
            if not video_inputs:
                raise RuntimeError("qwen-vl-utils没有产生视频输入")

        with record_time(timings, "cloud_full_preprocessing"):
            prompt = self.processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.processor(
                text=[prompt],
                images=image_inputs,
                videos=video_inputs,
                padding=True,
                return_tensors="pt",
                **video_kwargs,
            ).to(self.device)

        with record_time(timings, "cloud_full_model_generation"):
            with torch.inference_mode():
                output_ids = self.model.generate(
                    **inputs,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=False,
                )
        description = self._decode_generated(
            output_ids,
            int(inputs["input_ids"].shape[1]),
        )
        sampled_frames = int(video_inputs[0].shape[0])
        timings["cloud_native_sampled_frames"] = float(sampled_frames)
        timings["cloud_native_input_tokens"] = float(
            inputs["input_ids"].shape[1]
        )
        timings["cloud_native_peak_gpu_memory_gib"] = float(
            torch.cuda.max_memory_allocated() / (1024**3)
        )

        if self.description_only:
            advice, regulations_count = "", 0
        else:
            advice, regulations_count = self._generate_advice(
                description,
                timings,
            )
        output = {
            "event_detected": infer_event_detected(description),
            "description": description,
            "advice": advice,
            "regulations_count": regulations_count,
            "native_video": {
                "requested_fps": self.baseline_video_fps,
                "sampled_frames": sampled_frames,
                "video_grid_thw": (
                    inputs["video_grid_thw"].detach().cpu().tolist()
                    if "video_grid_thw" in inputs
                    else None
                ),
                "input_token_count": int(inputs["input_ids"].shape[1]),
            },
        }
        return output, timings

    def process_tokens(
        self,
        payload: dict[str, Any],
        sample_id: str,
    ) -> tuple[dict[str, Any], dict[str, float]]:
        del sample_id
        torch = self.torch
        timings: dict[str, float] = {}

        with record_time(timings, "cloud_token_decode_and_restore"):
            decoded = decode_token_payload(payload)
            restored = restore_visual_features(decoded)
            input_ids_np = decoded["input_ids"]
            attention_np = decoded["attention_mask"]
            grid_np = decoded["image_grid_thw"]

        with record_time(timings, "cloud_token_model_generation"):
            input_ids = torch.from_numpy(input_ids_np).long().to(self.device)
            attention_mask = (
                torch.from_numpy(attention_np).long().to(self.device)
                if attention_np is not None
                else None
            )
            image_grid_thw = torch.from_numpy(grid_np).long().to(self.device)
            visual_device = next(self.model.visual.parameters()).device
            visual_features = torch.from_numpy(restored[0]).to(
                device=visual_device,
                dtype=self.model.visual.dtype,
            )

            image_token_id = int(self.model.config.image_token_id)
            expected_tokens = int((input_ids == image_token_id).sum().item())
            if expected_tokens != int(visual_features.shape[0]):
                raise RuntimeError(
                    "视觉Token数量与输入占位符不一致："
                    f"features={visual_features.shape[0]}, "
                    f"placeholders={expected_tokens}"
                )

            original_visual_forward = self.model.visual.forward

            def injected_visual_forward(pixel_values, grid_thw=None):
                del pixel_values, grid_thw
                return visual_features

            dummy_pixels = torch.zeros(
                (1, 1),
                device=visual_device,
                dtype=self.model.visual.dtype,
            )
            generate_kwargs = {
                "input_ids": input_ids,
                "pixel_values": dummy_pixels,
                "image_grid_thw": image_grid_thw,
                "max_new_tokens": self.max_new_tokens,
                "do_sample": False,
            }
            if attention_mask is not None:
                generate_kwargs["attention_mask"] = attention_mask

            with self._generation_lock:
                try:
                    self.model.visual.forward = injected_visual_forward
                    with torch.inference_mode():
                        output_ids = self.model.generate(**generate_kwargs)
                finally:
                    self.model.visual.forward = original_visual_forward

            description = self._decode_generated(
                output_ids,
                int(input_ids.shape[1]),
            )

        if self.description_only:
            advice, regulations_count = "", 0
        else:
            advice, regulations_count = self._generate_advice(
                description,
                timings,
            )
        output = {
            "event_detected": infer_event_detected(description),
            "description": description,
            "advice": advice,
            "regulations_count": regulations_count,
        }
        return output, timings


def build_cloud_runtime(config: dict[str, Any]):
    if config["runtime"]["mock"]:
        return MockCloudRuntime()
    return CloudRuntime(config)
