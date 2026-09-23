from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from common.config import ensure_work_dirs, load_config
from common.prompts import NORMAL_ADVICE, NORMAL_DESCRIPTION
from common.timing import elapsed_ms, record_time
from vehicle.accident_detector import build_accident_detector


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    manifest_path = Path(path).expanduser().resolve()
    with manifest_path.open("r", encoding="utf-8") as handle:
        samples = json.load(handle)
    if not isinstance(samples, list) or not samples:
        raise ValueError("测试清单必须是非空JSON数组")

    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError(f"第{index + 1}条样本不是对象")
        sample_id = str(sample.get("id", "")).strip()
        if not sample_id or sample_id in seen_ids:
            raise ValueError(f"样本ID为空或重复：{sample_id!r}")
        seen_ids.add(sample_id)
        video = Path(str(sample.get("video", ""))).expanduser()
        if not video.is_absolute():
            video = manifest_path.parent / video
        if not video.exists():
            raise FileNotFoundError(f"样本视频不存在：{video}")
        label = str(sample.get("label", "")).lower()
        if label not in {"accident", "normal"}:
            raise ValueError(f"{sample_id}的label必须为accident或normal")
        validated.append(
            {
                "id": sample_id,
                "video": str(video.resolve()),
                "label": label,
                "reference": str(sample.get("reference", "")).strip(),
                "oracle_clip": str(sample.get("oracle_clip", "")).strip(),
                "clip_start_seconds": sample.get("clip_start_seconds"),
                "clip_end_seconds": sample.get("clip_end_seconds"),
            }
        )
    return validated


class BenchmarkRunner:
    def __init__(self, config: dict[str, Any], need_detector: bool):
        self.config = config
        self.road_url = config["network"]["road_url"].rstrip("/")
        self.timeout = int(config["runtime"]["request_timeout_seconds"])
        self.work_dir = ensure_work_dirs(config) / "vehicle_runs"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.detector = build_accident_detector(config) if need_detector else None

    def check_health(self) -> None:
        response = requests.get(f"{self.road_url}/health", timeout=10)
        response.raise_for_status()

    def run_baseline(
        self,
        sample: dict[str, Any],
        repeat: int,
    ) -> dict[str, Any]:
        video_path = Path(sample["video"])
        started_ns = time.perf_counter_ns()
        timings: dict[str, float] = {}
        with record_time(timings, "vehicle_request_round_trip"):
            with video_path.open("rb") as handle:
                response = requests.post(
                    f"{self.road_url}/v1/baseline",
                    data={"sample_id": sample["id"]},
                    files={
                        "video": (
                            video_path.name,
                            handle,
                            "application/octet-stream",
                        )
                    },
                    timeout=self.timeout,
                )
            response.raise_for_status()
        result = response.json()
        result.setdefault("timings_ms", {}).update(timings)
        result["timings_ms"]["vehicle_total"] = elapsed_ms(started_ns)
        return self._record(sample, repeat, "baseline", result)

    def run_proposed(
        self,
        sample: dict[str, Any],
        repeat: int,
    ) -> dict[str, Any]:
        if self.detector is None:
            raise RuntimeError("被测架构未初始化事故检测模型")

        started_ns = time.perf_counter_ns()
        timings: dict[str, float] = {}
        request_dir = Path(
            tempfile.mkdtemp(prefix=f"{sample['id']}_", dir=self.work_dir)
        )
        try:
            detection = self.detector.detect(
                Path(sample["video"]),
                request_dir / "clips",
                sample=sample,
            )
            timings.update(detection["timings_ms"])

            if not detection["event_detected"]:
                result = {
                    "sample_id": sample["id"],
                    "strategy": "proposed",
                    "output": {
                        "event_detected": False,
                        "description": NORMAL_DESCRIPTION,
                        "advice": NORMAL_ADVICE,
                        "regulations_count": 0,
                    },
                    "timings_ms": timings,
                    "transport": {
                        "vehicle_to_road_bytes": 0,
                        "road_to_cloud_bytes": 0,
                    },
                    "token_metrics": {
                        "original_visual_tokens": 0,
                        "retained_visual_tokens": 0,
                        "token_keep_ratio_actual": 0.0,
                        "description_frame_count": 0,
                    },
                    "detected_segments": detection["segments"],
                    "detector_metadata": detection.get("detector_metadata", {}),
                    "oracle_upper_bound": bool(
                        detection.get("oracle_upper_bound", False)
                    ),
                }
                result["timings_ms"]["vehicle_total"] = elapsed_ms(started_ns)
                return self._record(sample, repeat, "proposed", result)

            clip_paths = [Path(path) for path in detection["clip_paths"]]
            with record_time(timings, "vehicle_request_round_trip"):
                with ExitStack() as stack:
                    files = [
                        (
                            "clips",
                            (
                                path.name,
                                stack.enter_context(path.open("rb")),
                                "application/octet-stream",
                            ),
                        )
                        for path in clip_paths
                    ]
                    response = requests.post(
                        f"{self.road_url}/v1/proposed",
                        data={"sample_id": sample["id"]},
                        files=files,
                        timeout=self.timeout,
                    )
                response.raise_for_status()

            result = response.json()
            result.setdefault("timings_ms", {}).update(timings)
            result["timings_ms"]["vehicle_total"] = elapsed_ms(started_ns)
            result["detected_segments"] = detection["segments"]
            result["detector_metadata"] = detection.get("detector_metadata", {})
            result["oracle_upper_bound"] = bool(
                detection.get("oracle_upper_bound", False)
            )
            return self._record(sample, repeat, "proposed", result)
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    @staticmethod
    def _record(
        sample: dict[str, Any],
        repeat: int,
        strategy: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "sample_id": sample["id"],
            "video": sample["video"],
            "label": sample["label"],
            "reference": sample["reference"],
            "repeat": int(repeat),
            "strategy": strategy,
            "status": "ok",
            **result,
        }


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def error_record(
    sample: dict[str, Any],
    repeat: int,
    strategy: str,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "sample_id": sample["id"],
        "video": sample["video"],
        "label": sample["label"],
        "reference": sample["reference"],
        "repeat": int(repeat),
        "strategy": strategy,
        "status": "error",
        "error": f"{type(exc).__name__}: {exc}",
    }


def strategy_order(strategy: str, repeat: int) -> list[str]:
    if strategy != "both":
        return [strategy]
    return ["baseline", "proposed"] if repeat % 2 == 0 else ["proposed", "baseline"]


def main() -> None:
    parser = argparse.ArgumentParser(description="运行车—路—云双链路基准测试")
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--strategy",
        choices=("baseline", "proposed", "both"),
        default="both",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args()

    config = load_config(
        args.config,
        role="vehicle" if args.strategy != "baseline" else "common",
    )
    samples = load_manifest(args.manifest)
    output_path = Path(args.output).expanduser().resolve()
    repeats = args.repeats or int(config["benchmark"]["repeats"])
    runner = BenchmarkRunner(config, need_detector=args.strategy != "baseline")
    runner.check_health()

    metadata_path = output_path.with_suffix(output_path.suffix + ".meta.json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "config": config,
                "manifest": str(Path(args.manifest).resolve()),
                "strategy": args.strategy,
                "repeats": repeats,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not args.skip_warmup:
        warmup_runs = int(config["benchmark"]["warmup_runs"])
        first_sample = samples[0]
        for warmup_index in range(warmup_runs):
            for current in strategy_order(args.strategy, warmup_index):
                if current == "baseline":
                    runner.run_baseline(first_sample, -1)
                else:
                    runner.run_proposed(first_sample, -1)

    for repeat in range(repeats):
        ordered_samples = samples if repeat % 2 == 0 else list(reversed(samples))
        for sample in ordered_samples:
            for current in strategy_order(args.strategy, repeat):
                try:
                    if current == "baseline":
                        record = runner.run_baseline(sample, repeat)
                    else:
                        record = runner.run_proposed(sample, repeat)
                except Exception as exc:
                    record = error_record(sample, repeat, current, exc)
                    append_record(output_path, record)
                    print(
                        f"[ERROR] {sample['id']} {current}: {record['error']}",
                        flush=True,
                    )
                    if args.fail_fast:
                        raise
                    continue
                append_record(output_path, record)
                latency = record["timings_ms"]["vehicle_total"]
                print(
                    f"[OK] repeat={repeat} sample={sample['id']} "
                    f"strategy={current} latency_ms={latency:.3f}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
