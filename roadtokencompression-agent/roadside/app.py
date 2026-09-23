from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from common.codec import json_size_bytes
from common.config import ensure_work_dirs, load_config
from common.network import simulate_transfer
from common.timing import elapsed_ms, record_time
from common.video import save_upload
from roadside.runtime import build_road_runtime


def create_app(config: dict[str, Any]):
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    runtime = build_road_runtime(config)
    work_dir = ensure_work_dirs(config) / "road_uploads"
    cloud_url = config["network"]["cloud_url"].rstrip("/")
    timeout = int(config["runtime"]["request_timeout_seconds"])
    simulate_network = bool(config["network"]["simulate"])
    include_return_path = bool(config["network"]["include_return_path"])
    wifi_config = config["network"]["wifi"]
    wired_config = config["network"]["wired"]

    def maybe_simulate(
        timings: dict[str, float],
        key: str,
        num_bytes: int,
        link_config: dict[str, Any],
    ) -> None:
        if simulate_network:
            simulate_transfer(timings, key, num_bytes, link_config)

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "role": "roadside",
                "mock": bool(config["runtime"]["mock"]),
                "compression": bool(config["road"]["use_compression"]),
                "keep_ratio": float(config["road"]["token_keep_ratio"]),
            }
        )

    @app.post("/v1/baseline")
    def baseline():
        started_ns = time.perf_counter_ns()
        timings: dict[str, float] = {}
        video_path: Path | None = None
        try:
            if "video" not in request.files:
                return jsonify({"error": "缺少video文件"}), 400
            sample_id = request.form.get("sample_id", "")
            with record_time(timings, "road_receive_and_save"):
                video_path, received_bytes = save_upload(
                    request.files["video"],
                    work_dir,
                )
            maybe_simulate(
                timings,
                "sim_wifi_vehicle_to_road",
                received_bytes,
                wifi_config,
            )
            maybe_simulate(
                timings,
                "sim_wired_road_to_cloud",
                received_bytes,
                wired_config,
            )
            with record_time(timings, "road_forward_to_cloud"):
                with video_path.open("rb") as handle:
                    response = requests.post(
                        f"{cloud_url}/v1/process-video",
                        data={"sample_id": sample_id},
                        files={
                            "video": (
                                video_path.name,
                                handle,
                                "application/octet-stream",
                            )
                        },
                        timeout=timeout,
                    )
                response.raise_for_status()
            if include_return_path:
                maybe_simulate(
                    timings,
                    "sim_wired_cloud_to_road",
                    len(response.content),
                    wired_config,
                )
            result = response.json()
            result.setdefault("transport", {}).update(
                {
                    "vehicle_to_road_bytes": int(received_bytes),
                    "road_to_cloud_bytes": int(received_bytes),
                }
            )
            result["strategy"] = "baseline"
            if include_return_path:
                maybe_simulate(
                    timings,
                    "sim_wifi_road_to_vehicle",
                    json_size_bytes(result),
                    wifi_config,
                )
            result.setdefault("timings_ms", {}).update(timings)
            result["timings_ms"]["road_total"] = elapsed_ms(started_ns)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        finally:
            if video_path is not None:
                video_path.unlink(missing_ok=True)

    @app.post("/v1/proposed")
    def proposed():
        started_ns = time.perf_counter_ns()
        timings: dict[str, float] = {}
        request_dir = Path(tempfile.mkdtemp(prefix="road_clips_", dir=work_dir))
        try:
            uploads = request.files.getlist("clips")
            if not uploads:
                return jsonify({"error": "缺少clips文件"}), 400
            sample_id = request.form.get("sample_id", "")

            clip_paths: list[Path] = []
            received_bytes = 0
            with record_time(timings, "road_receive_and_save"):
                for upload in uploads:
                    path, size = save_upload(upload, request_dir)
                    clip_paths.append(path)
                    received_bytes += size
            maybe_simulate(
                timings,
                "sim_wifi_vehicle_to_road",
                received_bytes,
                wifi_config,
            )

            payload, model_timings, token_metrics = runtime.encode_clips(clip_paths)
            timings.update(model_timings)
            outbound = {"sample_id": sample_id, "tokens": payload}
            road_to_cloud_bytes = json_size_bytes(outbound)
            maybe_simulate(
                timings,
                "sim_wired_road_to_cloud",
                road_to_cloud_bytes,
                wired_config,
            )

            with record_time(timings, "road_forward_to_cloud"):
                response = requests.post(
                    f"{cloud_url}/v1/process-tokens",
                    json=outbound,
                    timeout=timeout,
                )
                response.raise_for_status()
            if include_return_path:
                maybe_simulate(
                    timings,
                    "sim_wired_cloud_to_road",
                    len(response.content),
                    wired_config,
                )

            result = response.json()
            result.setdefault("transport", {}).update(
                {
                    "vehicle_to_road_bytes": int(received_bytes),
                    "road_to_cloud_bytes": int(road_to_cloud_bytes),
                }
            )
            result["token_metrics"] = token_metrics
            result["strategy"] = "proposed"
            if include_return_path:
                maybe_simulate(
                    timings,
                    "sim_wifi_road_to_vehicle",
                    json_size_bytes(result),
                    wifi_config,
                )
            result.setdefault("timings_ms", {}).update(timings)
            result["timings_ms"]["road_total"] = elapsed_ms(started_ns)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动路侧计算服务")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_config(args.config, role="road")
    app = create_app(config)
    app.run(
        host=config["road"]["host"],
        port=int(config["road"]["port"]),
        debug=False,
        threaded=False,
    )


if __name__ == "__main__":
    main()
