from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any

from common.timing import record_time


class MockAccidentDetector:
    def detect(
        self,
        video_path: Path,
        output_dir: Path,
        sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del output_dir, sample
        timings: dict[str, float] = {}
        with record_time(timings, "vehicle_accident_detection"):
            event_detected = "normal" not in video_path.as_posix().lower()
        return {
            "event_detected": event_detected,
            "clip_paths": [video_path] if event_detected else [],
            "segments": (
                [{"start_seconds": 0.0, "end_seconds": 1.0}]
                if event_detected
                else []
            ),
            "timings_ms": timings,
        }


class VideoMAEAccidentDetector:
    """复用原项目VideoMAE模型和滑窗切片函数。"""

    def __init__(self, config: dict[str, Any]):
        import torch

        self.config = config
        self.torch = torch
        from vendor.detect_videoMAEv2 import Config
        from vendor.extract_accident_segments import load_model_and_processor

        model_config = Config()
        model_config.model_name = str(config["models"]["videomae_model"])
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model, self.processor = load_model_and_processor(
            model_config,
            str(config["models"]["videomae_checkpoint"]),
            self.device,
        )
        self.model.eval()

    def detect(
        self,
        video_path: Path,
        output_dir: Path,
        sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del sample
        from decord import VideoReader
        from vendor.extract_accident_segments import (
            cut_segments_with_opencv,
            merge_segments,
            score_windows,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        timings: dict[str, float] = {}

        with record_time(timings, "vehicle_video_window_scoring"):
            reader = VideoReader(str(video_path))
            fps = float(reader.get_avg_fps() or 25.0)
            duration = len(reader) / fps
            indices, scores = score_windows(
                vr=reader,
                model=self.model,
                processor=self.processor,
                window_frames=int(self.config["vehicle"]["window_frames"]),
                stride_frames=int(self.config["vehicle"]["stride_frames"]),
                device=self.device,
            )

        with record_time(timings, "vehicle_segment_merge"):
            segments = merge_segments(
                indices_list=indices,
                scores=scores,
                fps=fps,
                threshold=float(self.config["vehicle"]["anomaly_threshold"]),
                pad_sec=float(self.config["vehicle"]["padding_seconds"]),
            )
            segments = [
                (float(start), min(float(end), duration))
                for start, end in segments
            ]

        clip_paths: list[Path] = []
        if segments:
            with record_time(timings, "vehicle_clip_export"):
                clip_paths = [
                    Path(path)
                    for path in cut_segments_with_opencv(
                        str(video_path),
                        str(output_dir),
                        segments,
                        fps,
                    )
                ]

        timings["vehicle_accident_detection"] = round(
            sum(
                value
                for key, value in timings.items()
                if key.startswith("vehicle_")
            ),
            3,
        )
        return {
            "event_detected": bool(clip_paths),
            "clip_paths": clip_paths,
            "segments": [
                {
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                }
                for start, end in segments
            ],
            "timings_ms": timings,
        }


class MobileNetPeakDetector:
    """以低帧率扫描全视频，并围绕最高事故概率时刻截取固定比例片段。"""

    def __init__(self, config: dict[str, Any]):
        import numpy as np
        import torch

        self.config = config
        self.np = np
        self.torch = torch
        script_path = Path(
            config["models"]["mobilenet_inference_script"]
        ).expanduser().resolve()
        spec = importlib.util.spec_from_file_location(
            "mobilenet_accident_inference",
            script_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载MobileNet推理脚本：{script_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.backend = module

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.sample_fps = float(config["vehicle"]["mobilenet_sample_fps"])
        self.batch_size = int(config["vehicle"]["mobilenet_batch_size"])
        self.decoder = str(config["vehicle"]["mobilenet_decoder"])
        self.threshold = float(config["vehicle"]["anomaly_threshold"])
        self.clip_ratio = float(config["vehicle"]["mobilenet_clip_ratio"])
        self.smooth_window = int(config["vehicle"]["mobilenet_smooth_window"])
        self.video_encoder = str(config["vehicle"]["mobilenet_video_encoder"])
        self.use_fp16 = bool(config["vehicle"]["mobilenet_fp16"])
        self.transport_mode = str(
            config["vehicle"]["mobilenet_transport_mode"]
        )
        self.output_frame_count = int(config["road"]["description_frames"])
        self.cached_frame_size = int(
            config["vehicle"]["mobilenet_cached_frame_size"]
        )
        self.transport_sample_fps = float(
            config["vehicle"].get("mobilenet_transport_sample_fps", 1.0)
        )
        self.model = module.build_model(
            config["models"]["mobilenet_checkpoint"],
            self.device,
        )
        module.warm_up_model(
            self.model,
            self.device,
            self.use_fp16,
            self.batch_size,
        )

    def _export_uniform_segment_frames(
        self,
        video_path: Path,
        output_dir: Path,
        start_seconds: float,
        end_seconds: float,
    ) -> tuple[list[Path], list[float], list[int], float]:
        """从原视频片段按固定时间间隔重新解码唯一帧，避免复用低帧率扫描缓存。"""
        import cv2

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"无法打开视频：{video_path}")
        source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0 or source_fps <= 0:
            capture.release()
            raise RuntimeError(f"视频帧数或帧率无效：{video_path}")

        step_seconds = 1.0 / self.transport_sample_fps
        requested_times = self.np.arange(
            float(start_seconds),
            float(end_seconds) + 1e-9,
            step_seconds,
            dtype=self.np.float64,
        )
        if len(requested_times) == 0:
            requested_times = self.np.asarray(
                [(float(start_seconds) + float(end_seconds)) / 2.0],
                dtype=self.np.float64,
            )
        frame_indices = self.np.rint(requested_times * source_fps).astype(
            self.np.int64
        )
        frame_indices = self.np.unique(
            self.np.clip(frame_indices, 0, total_frames - 1)
        )

        paths: list[Path] = []
        actual_times: list[float] = []
        actual_indices: list[int] = []
        try:
            for output_index, frame_index_value in enumerate(frame_indices):
                frame_index = int(frame_index_value)
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = capture.read()
                if not ok:
                    continue
                height, width = frame.shape[:2]
                scale = min(
                    float(self.cached_frame_size) / width,
                    float(self.cached_frame_size) / height,
                    1.0,
                )
                if scale < 1.0:
                    frame = cv2.resize(
                        frame,
                        (
                            max(1, int(round(width * scale))),
                            max(1, int(round(height * scale))),
                        ),
                        interpolation=cv2.INTER_AREA,
                    )
                frame_path = output_dir / f"frame_{output_index:03d}.jpg"
                ok = cv2.imwrite(
                    str(frame_path),
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 90],
                )
                if not ok:
                    raise RuntimeError(f"无法保存候选片段帧：{frame_path}")
                paths.append(frame_path)
                actual_indices.append(frame_index)
                actual_times.append(frame_index / source_fps)
        finally:
            capture.release()

        if not paths:
            raise RuntimeError("候选片段未能解码出有效帧")
        return paths, actual_times, actual_indices, source_fps

    def detect(
        self,
        video_path: Path,
        output_dir: Path,
        sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del sample
        output_dir.mkdir(parents=True, exist_ok=True)
        total_started = time.perf_counter()

        scored = self.backend.score_video(
            video_path,
            self.model,
            self.device,
            self.sample_fps,
            self.batch_size,
            self.decoder,
            self.use_fp16,
            self.transport_mode == "cached_frames",
            self.cached_frame_size,
        )

        post_started = time.perf_counter()
        probs = self.np.asarray(scored["probs"], dtype=self.np.float32)
        window = self.smooth_window
        if window == 0:
            window = max(1, int(round(self.sample_fps * 0.6)))
        if window > 1 and len(probs) >= window:
            kernel = self.np.ones(window, dtype=self.np.float32) / window
            probs = self.np.convolve(probs, kernel, mode="same")
        peak_time, peak_prob, segments = self.backend.select_peak_segment(
            scored["times"],
            probs,
            self.threshold,
            scored["video_duration"],
            self.clip_ratio,
        )
        postprocess_ms = (time.perf_counter() - post_started) * 1000.0

        clip_paths: list[Path] = []
        clip_encoder = None
        selected_frame_times: list[float] = []
        selected_source_frame_indices: list[int] = []
        selected_source_fps: float | None = None
        export_ms = 0.0
        if segments:
            export_started = time.perf_counter()
            start, end = segments[0]
            if self.transport_mode == "cached_frames":
                import cv2

                cached = scored["sampled_frames_rgb"]
                times = self.np.asarray(scored["times"], dtype=self.np.float64)
                candidates = self.np.flatnonzero(
                    (times >= float(start)) & (times <= float(end))
                )
                if len(candidates) == 0:
                    candidates = self.np.asarray(
                        [int(self.np.argmin(self.np.abs(times - peak_time)))],
                        dtype=self.np.int64,
                    )
                positions = self.np.rint(
                    self.np.linspace(
                        0,
                        len(candidates) - 1,
                        self.output_frame_count,
                    )
                ).astype(self.np.int64)
                selected = candidates[positions]
                for output_index, frame_index in enumerate(selected):
                    frame_path = output_dir / f"frame_{output_index:02d}.jpg"
                    ok = cv2.imwrite(
                        str(frame_path),
                        cv2.cvtColor(
                            cached[int(frame_index)],
                            cv2.COLOR_RGB2BGR,
                        ),
                        [int(cv2.IMWRITE_JPEG_QUALITY), 90],
                    )
                    if not ok:
                        raise RuntimeError(f"无法保存复用帧：{frame_path}")
                    clip_paths.append(frame_path)
                    selected_frame_times.append(float(times[int(frame_index)]))
                clip_encoder = "cached_jpeg_frames"
            elif self.transport_mode == "segment_uniform_frames":
                (
                    clip_paths,
                    selected_frame_times,
                    selected_source_frame_indices,
                    selected_source_fps,
                ) = self._export_uniform_segment_frames(
                    video_path,
                    output_dir,
                    float(start),
                    float(end),
                )
                clip_encoder = "uniform_segment_jpeg_frames"
            else:
                clip_path = output_dir / "clip_00.mp4"
                clip_encoder = self.backend.export_clip(
                    video_path,
                    clip_path,
                    start,
                    end,
                    self.video_encoder,
                )
                clip_paths.append(clip_path)
            export_ms = (time.perf_counter() - export_started) * 1000.0

        frame_packaging_ms = (
            export_ms
            if self.transport_mode in {"cached_frames", "segment_uniform_frames"}
            else 0.0
        )
        clip_export_ms = (
            export_ms if self.transport_mode == "clip" else 0.0
        )
        timings = {
            "vehicle_decoder_setup": round(float(scored["decoder_setup_ms"]), 3),
            "vehicle_decode_preprocess": round(
                float(scored["decode_preprocess_ms"]), 3
            ),
            "vehicle_model_inference": round(
                float(scored["model_inference_ms"]), 3
            ),
            "vehicle_video_scoring": round(float(scored["scan_total_ms"]), 3),
            "vehicle_peak_postprocess": round(postprocess_ms, 3),
            "vehicle_frame_packaging": round(frame_packaging_ms, 3),
            "vehicle_clip_export": round(clip_export_ms, 3),
            "vehicle_accident_detection": round(
                (time.perf_counter() - total_started) * 1000.0,
                3,
            ),
        }
        return {
            "event_detected": bool(clip_paths),
            "clip_paths": clip_paths,
            "segments": [
                {
                    "start_seconds": round(float(start), 3),
                    "end_seconds": round(float(end), 3),
                }
                for start, end in segments
            ],
            "timings_ms": timings,
            "detector_metadata": {
                "name": "mobilenet_peak20",
                "peak_time_seconds": round(float(peak_time), 3),
                "peak_probability": round(float(peak_prob), 6),
                "threshold": self.threshold,
                "clip_ratio": self.clip_ratio,
                "sample_fps": self.sample_fps,
                "sampled_frame_count": int(scored["sampled_frame_count"]),
                "decoder": scored["decoder"],
                "clip_encoder": clip_encoder,
                "transport_mode": self.transport_mode,
                "transport_file_count": len(clip_paths),
                "cached_frame_size": scored.get("returned_frame_size"),
                "transport_frame_size": self.cached_frame_size,
                "transport_sample_fps": self.transport_sample_fps,
                "selected_frame_times_seconds": [
                    round(float(value), 3) for value in selected_frame_times
                ],
                "selected_source_frame_indices": selected_source_frame_indices,
                "selected_source_fps": selected_source_fps,
            },
        }


class OracleFixedClipDetector:
    """仅用于验证目标链路上界：依据清单标签选择预切好的10秒片段。"""

    def __init__(self, config: dict[str, Any]):
        self.detection_ms = float(config["vehicle"]["oracle_detection_ms"])
        self.clip_seconds = float(config["vehicle"]["oracle_clip_seconds"])

    def detect(
        self,
        video_path: Path,
        output_dir: Path,
        sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del video_path, output_dir
        if sample is None:
            raise ValueError("oracle_fixed_clip模式需要完整样本信息")

        timings: dict[str, float] = {}
        with record_time(timings, "vehicle_oracle_screening"):
            time.sleep(self.detection_ms / 1000.0)

        if sample["label"] == "normal":
            timings["vehicle_accident_detection"] = round(
                timings["vehicle_oracle_screening"],
                3,
            )
            return {
                "event_detected": False,
                "clip_paths": [],
                "segments": [],
                "timings_ms": timings,
                "oracle_upper_bound": True,
            }

        clip_value = str(sample.get("oracle_clip", "")).strip()
        if not clip_value:
            raise ValueError("事故样本缺少oracle_clip")
        clip_path = Path(clip_value).expanduser().resolve()
        if not clip_path.exists():
            raise FileNotFoundError(f"预切事故片段不存在：{clip_path}")

        start = float(sample.get("clip_start_seconds", 0.0))
        end = float(sample.get("clip_end_seconds", start + self.clip_seconds))
        actual_duration = end - start
        if abs(actual_duration - self.clip_seconds) > 0.05:
            raise ValueError(
                f"oracle片段必须为{self.clip_seconds:.3f}秒，"
                f"当前为{actual_duration:.3f}秒"
            )

        timings["vehicle_accident_detection"] = round(
            timings["vehicle_oracle_screening"],
            3,
        )
        return {
            "event_detected": True,
            "clip_paths": [clip_path],
            "segments": [
                {
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                }
            ],
            "timings_ms": timings,
            "oracle_upper_bound": True,
        }


def build_accident_detector(config: dict[str, Any]):
    if config["runtime"]["mock"]:
        return MockAccidentDetector()
    if config["vehicle"]["detector_mode"] == "oracle_fixed_clip":
        return OracleFixedClipDetector(config)
    if config["vehicle"]["detector_mode"] == "mobilenet_peak20":
        return MobileNetPeakDetector(config)
    return VideoMAEAccidentDetector(config)
