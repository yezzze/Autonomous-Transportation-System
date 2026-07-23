import argparse
import hashlib
import queue
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import grpc

import agent_pb2
import agent_pb2_grpc
from runtime_api import frame_transport_pb2, frame_transport_pb2_grpc


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="127.0.0.1:50051")
    parser.add_argument("--size-mib", type=int, default=10)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=128)
    parser.add_argument("--upload-timeout-sec", type=float, default=10.0)
    parser.add_argument("--infer-timeout-sec", type=float, default=30.0)
    args = parser.parse_args()

    if args.size_mib <= 0 or args.fps <= 0 or args.duration_sec <= 0:
        raise ValueError("size, fps and duration must be positive")

    chunk_size = 1024 * 1024
    frame_size = args.size_mib * chunk_size
    frame_count = int(args.fps * args.duration_sec)
    full_chunks, final_chunk_size = divmod(frame_size, chunk_size)
    chunk_payload = b"x" * chunk_size
    final_payload = b"x" * final_chunk_size
    digest = hashlib.sha256()
    for _ in range(full_chunks):
        digest.update(chunk_payload)
    if final_payload:
        digest.update(final_payload)
    frame_sha256 = digest.hexdigest()

    options = [
        ("grpc.max_send_message_length", chunk_size + 64 * 1024),
        ("grpc.max_receive_message_length", chunk_size + 64 * 1024),
    ]
    channel = grpc.insecure_channel(args.target, options=options)
    grpc.channel_ready_future(channel).result(timeout=10)
    stub = agent_pb2_grpc.AgentServiceStub(channel)
    frame_stub = frame_transport_pb2_grpc.FrameTransportStub(channel)
    completed = queue.Queue()

    def frame_chunks(frame_id):
        chunk_index = 0
        for _ in range(full_chunks):
            yield frame_transport_pb2.FrameChunk(
                frame_id=frame_id,
                chunk_index=chunk_index,
                data=chunk_payload,
                content_type="application/octet-stream" if chunk_index == 0 else "",
                total_size=frame_size if chunk_index == 0 else 0,
                sha256=frame_sha256 if chunk_index == 0 else "",
            )
            chunk_index += 1
        if final_payload:
            yield frame_transport_pb2.FrameChunk(
                frame_id=frame_id,
                chunk_index=chunk_index,
                data=final_payload,
            )

    def error_result(index, stage, started_at, scheduled_at, exc):
        code = exc.code().name if isinstance(exc, grpc.RpcError) else type(exc).__name__
        details = exc.details() if isinstance(exc, grpc.RpcError) else str(exc)
        return {
            "index": index,
            "ok": False,
            "stage": stage,
            "code": code,
            "details": details,
            "total_sec": time.perf_counter() - started_at,
            "schedule_lag_sec": started_at - scheduled_at,
        }

    def submit_frame(index, scheduled_at):
        started_at = time.perf_counter()
        frame_id = f"load-{uuid.uuid4().hex}"
        try:
            upload_begin = time.perf_counter()
            frame_ref = frame_stub.UploadFrame(
                frame_chunks(frame_id),
                timeout=args.upload_timeout_sec,
                wait_for_ready=True,
            )
            upload_sec = time.perf_counter() - upload_begin
        except Exception as exc:
            completed.put(error_result(index, "upload", started_at, scheduled_at, exc))
            return

        infer_future = stub.Infer.future(
            agent_pb2.InferRequest(text="frame load", frame=frame_ref),
            timeout=args.infer_timeout_sec,
            wait_for_ready=True,
        )

        def inference_done(call):
            try:
                response = call.result()
                valid = str(frame_size) in response.result and frame_sha256 in response.result
                completed.put(
                    {
                        "index": index,
                        "ok": valid,
                        "stage": "done" if valid else "verify",
                        "code": "OK" if valid else "INVALID_RESULT",
                        "details": "" if valid else response.result,
                        "upload_sec": upload_sec,
                        "total_sec": time.perf_counter() - started_at,
                        "schedule_lag_sec": started_at - scheduled_at,
                    }
                )
            except Exception as exc:
                result = error_result(
                    index,
                    "infer",
                    started_at,
                    scheduled_at,
                    exc,
                )
                result["upload_sec"] = upload_sec
                completed.put(result)

        infer_future.add_done_callback(inference_done)

    load_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for index in range(frame_count):
            scheduled_at = load_start + index / args.fps
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            executor.submit(submit_frame, index, scheduled_at)

    results = []
    deadline = (
        load_start
        + args.duration_sec
        + args.upload_timeout_sec
        + args.infer_timeout_sec
        + 10
    )
    while len(results) < frame_count:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        try:
            result = completed.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        results.append(result)
        if len(results) % 50 == 0:
            successes = sum(1 for item in results if item["ok"])
            print(
                f"progress completed={len(results)}/{frame_count} "
                f"success={successes}",
                flush=True,
            )

    channel.close()
    missing = frame_count - len(results)
    success = [item for item in results if item["ok"]]
    failed = [item for item in results if not item["ok"]]
    upload_latencies = [item["upload_sec"] for item in success]
    total_latencies = [item["total_sec"] for item in success]
    elapsed = time.perf_counter() - load_start
    error_counts = {}
    for item in failed:
        key = f"{item['stage']}:{item['code']}"
        error_counts[key] = error_counts.get(key, 0) + 1
    if missing:
        error_counts["client:MISSING_RESULT"] = missing

    print("=== frame load result ===")
    print(
        f"requested={frame_count} success={len(success)} failed={len(failed)} "
        f"missing={missing} elapsed_sec={elapsed:.3f}"
    )
    print(
        f"input_rate={args.fps:.2f}fps frame_mib={args.size_mib} "
        f"offered_mib_per_sec={args.fps * args.size_mib:.2f}"
    )
    if success:
        print(
            "upload_sec "
            f"avg={statistics.fmean(upload_latencies):.4f} "
            f"p50={percentile(upload_latencies, 0.50):.4f} "
            f"p95={percentile(upload_latencies, 0.95):.4f} "
            f"p99={percentile(upload_latencies, 0.99):.4f}"
        )
        print(
            "total_sec "
            f"avg={statistics.fmean(total_latencies):.4f} "
            f"p50={percentile(total_latencies, 0.50):.4f} "
            f"p95={percentile(total_latencies, 0.95):.4f} "
            f"p99={percentile(total_latencies, 0.99):.4f}"
        )
        print(f"completed_fps={len(success) / elapsed:.3f}")
    print(f"errors={error_counts}")
    for item in failed[:10]:
        print(
            f"sample_error index={item['index']} stage={item['stage']} "
            f"code={item['code']} details={item['details']}"
        )

    if len(success) != frame_count:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
