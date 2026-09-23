from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from cloud.runtime import build_cloud_runtime
from common.config import ensure_work_dirs, load_config
from common.timing import elapsed_ms, record_time
from common.video import save_upload


def create_app(config: dict[str, Any]):
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    runtime = build_cloud_runtime(config)
    work_dir = ensure_work_dirs(config) / "cloud_uploads"

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "role": "cloud",
                "mock": bool(config["runtime"]["mock"]),
            }
        )

    @app.post("/v1/process-video")
    def process_video():
        started_ns = time.perf_counter_ns()
        timings: dict[str, float] = {}
        video_path: Path | None = None
        try:
            if "video" not in request.files:
                return jsonify({"error": "缺少video文件"}), 400
            sample_id = request.form.get("sample_id", "")
            with record_time(timings, "cloud_receive_and_save"):
                video_path, received_bytes = save_upload(
                    request.files["video"],
                    work_dir,
                )
            output, runtime_timings = runtime.process_video(video_path, sample_id)
            timings.update(runtime_timings)
            timings["cloud_total"] = elapsed_ms(started_ns)
            return jsonify(
                {
                    "sample_id": sample_id,
                    "output": output,
                    "timings_ms": timings,
                    "transport": {
                        "cloud_received_bytes": int(received_bytes),
                    },
                }
            )
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        finally:
            if video_path is not None:
                video_path.unlink(missing_ok=True)

    @app.post("/v1/process-tokens")
    def process_tokens():
        started_ns = time.perf_counter_ns()
        try:
            data = request.get_json(force=True)
            if not data or "tokens" not in data:
                return jsonify({"error": "缺少tokens字段"}), 400
            sample_id = str(data.get("sample_id", ""))
            output, timings = runtime.process_tokens(data["tokens"], sample_id)
            timings["cloud_total"] = elapsed_ms(started_ns)
            return jsonify(
                {
                    "sample_id": sample_id,
                    "output": output,
                    "timings_ms": timings,
                    "transport": {},
                }
            )
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动云端Qwen服务")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_config(args.config, role="cloud")
    app = create_app(config)
    app.run(
        host=config["cloud"]["host"],
        port=int(config["cloud"]["port"]),
        debug=False,
        threaded=False,
    )


if __name__ == "__main__":
    main()
