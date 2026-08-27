import os
import sys
import torch
import multiprocessing as mp

# 指定使用 GPU
os.environ["CUDA_VISIBLE_DEVICES"] = "0"

def run_collaborator(queue=None):
    agent_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VOGS_Collaborator_Agent")
    sys.path.insert(0, agent_dir)
    os.chdir(agent_dir)
    from fast_api.model_runtime import model_runtime as collab_runtime
    import pickle

    print("Loading Collaborator Model...")
    collab_runtime.load_model("Latency_Test/ours/collab")
    collab_runtime.opt.num_frames = 2

    # ===== 发送端统计（闭包）：在真正"发出去"的 mock_send 上，基于真实 payload 本身做累计 =====
    send_stats = {
        "frames": 0,                   # 调用 mock_send 的总帧数（含 warmup）
        "comm_frames": 0,              # record_len > 1 的有效协同帧数
        "raw_gaussians_sum": 0,        # 每帧打包的 3D 高斯锚点总数（means.shape[1]，未过滤）
        "baseline_sum": 0,             # ROI+opacity 过滤后 baseline 个数
        "actual_sum": 0,               # 过滤后 actual 个数（Collaborator 侧此处与 baseline 相同，未做带宽/zbuffer 过滤）
        "payload_bytes_sum": 0,        # pickle.dumps 真实传输字节数（和落盘文件大小一致）
    }

    def mock_send(payload):
        frame_id = payload.get("frame_id")
        # 1) Raw Anchors：真实打包进 payload 的高斯锚点数（和传输强绑定，完全从 payload 本身读取）
        #    collaborator_gaussian 通过 tensor_to_numpy(GaussianPrediction NamedTuple._asdict()) 转为 dict
        cg = payload.get("collaborator_gaussian", {})
        if isinstance(cg, dict) and "means" in cg and hasattr(cg["means"], "shape"):
            # means shape: (1, num_gaussian, 3) after tensor_to_numpy; axis=1 = anchor count
            shape = cg["means"].shape
            if len(shape) == 3:
                n_raw = int(shape[1])
            elif len(shape) == 2:
                # 某些配置下可能没有 batch 维
                n_raw = int(shape[0])
            else:
                n_raw = 0
            send_stats["raw_gaussians_sum"] += n_raw
        else:
            n_raw = 0

        # 2) Filtered (baseline/actual)：模型 forward 时按 ego 坐标 ROI+opacity 统一口径算好，直接读 payload 自带字段
        comm_stats = payload.get("comm_stats") or {}
        baseline = int(comm_stats.get("baseline_num", 0) or 0)
        actual = int(comm_stats.get("actual_num", 0) or 0)
        send_stats["baseline_sum"] += baseline
        send_stats["actual_sum"] += actual
        if baseline > 0 or actual > 0 or (payload.get("record_len") is not None
                                         and hasattr(payload["record_len"], "__len__")
                                         and len(payload["record_len"]) > 0
                                         and payload["record_len"].flat[0] > 1):
            send_stats["comm_frames"] += 1

        # 3) 真实字节数：完全等价于写盘/走网络时的大小，直接反映带宽
        raw_bytes = pickle.dumps(payload)
        n_bytes = len(raw_bytes)
        send_stats["payload_bytes_sum"] += n_bytes

        send_stats["frames"] += 1
        # 每帧打印一行，便于肉眼追踪（把三种统计口径一起打印，避免事后猜测）
        print(f"[Collaborator] Frame {frame_id:3d} | "
              f"Raw Anchors: {n_raw:5d} | "
              f"ROI+opacity Baseline: {baseline:5d} | "
              f"Actual (post-filter): {actual:5d} | "
              f"Payload: {n_bytes/1024:.1f} KB")

        with open(f"/tmp/payload_{frame_id}.pkl", "wb") as f:
            f.write(raw_bytes)

    print("Running Collaborator...")
    collab_result = collab_runtime.run_benchmark_sync(mock_send)

    # ========== 在发送端位置统一汇总输出 ==========
    nf = send_stats["frames"] if send_stats["frames"] > 0 else 1
    cf = send_stats["comm_frames"] if send_stats["comm_frames"] > 0 else 1
    avg_raw = send_stats["raw_gaussians_sum"] / nf
    avg_baseline = send_stats["baseline_sum"] / cf
    avg_actual = send_stats["actual_sum"] / cf
    ratio = (avg_actual / avg_baseline * 100) if avg_baseline > 0 else 0.0
    avg_kb = send_stats["payload_bytes_sum"] / nf / 1024.0
    total_mb = send_stats["payload_bytes_sum"] / (1024.0 * 1024.0)

    # 写回 collab_result，随后传给主进程汇总
    collab_result["Raw Anchors per Frame"] = float(f"{avg_raw:.1f}")
    collab_result["Baseline Gaussians"] = float(f"{avg_baseline:.1f}")
    collab_result["Actual Transmitted"] = float(f"{avg_actual:.1f}")
    collab_result["Ratio"] = f"{ratio:.2f}%"
    collab_result["Payload KB per Frame"] = float(f"{avg_kb:.1f}")
    collab_result["Total Payload MB"] = float(f"{total_mb:.2f}")
    collab_result["Sent Payload Frames"] = int(send_stats["frames"])
    collab_result["Valid Comm Frames"] = int(send_stats["comm_frames"])

    print()
    print("========== Collaborator (Transmitter) Summary ==========")
    print(f"  Sent Payload Frames            : {collab_result['Sent Payload Frames']}")
    print(f"  Valid Collaborative Frames     : {collab_result['Valid Comm Frames']}")
    print(f"  Raw Anchors per Frame          : {collab_result['Raw Anchors per Frame']}   (means.shape[1], 未过滤)")
    print(f"  Baseline Gaussians (ROI+opa)   : {collab_result['Baseline Gaussians']}   (ego 坐标 ROI + opacity>=0.01)")
    print(f"  Actual Transmitted             : {collab_result['Actual Transmitted']}   (发送端口径，此处 = Baseline)")
    print(f"  Ratio (Actual / Baseline)      : {collab_result['Ratio']}")
    print(f"  Payload KB per Frame           : {collab_result['Payload KB per Frame']}   (pickle.dumps 真实字节数)")
    print(f"  Total Transferred              : {collab_result['Total Payload MB']} MB")
    print("========================================================")

    # 传回到主进程
    if queue is not None:
        queue.put(("collab", collab_result))


def run_ego(queue=None):
    agent_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VOGS_Ego_Agent")
    sys.path.insert(0, agent_dir)
    os.chdir(agent_dir)
    from fast_api.model_runtime import model_runtime as ego_runtime
    import pickle

    print("Loading Ego Model...")
    ego_runtime.load_model("Latency_Test/ours/collab")
    ego_runtime.opt.num_frames = 2

    # We need a counter because mock_receive is called per frame
    frame_counter = [0]

    def mock_receive():
        frame_id = frame_counter[0]
        frame_counter[0] += 1
        print(f"[Ego] Received payload for frame {frame_id}")
        with open(f"/tmp/payload_{frame_id}.pkl", "rb") as f:
            payload = pickle.load(f)
        return payload

    print("Running Ego...")
    ego_result = ego_runtime.run_benchmark_sync(mock_receive)
    # Ego 端不再打印通信量字段，只展示性能和检测结果（避免干扰）
    print("========== Ego Summary ==========")
    for k, v in ego_result.items():
        if k in ("Baseline Gaussians", "Actual Transmitted", "Ratio"):
            continue
        print(f"  {k}: {v}")
    print("==================================")
    if queue is not None:
        queue.put(("ego", ego_result))


def main():
    mp.set_start_method('spawn')
    result_queue = mp.Queue()

    p_collab = mp.Process(target=run_collaborator, args=(result_queue,))
    p_ego = mp.Process(target=run_ego, args=(result_queue,))

    p_collab.start()
    p_collab.join()

    p_ego.start()
    p_ego.join()

    # 主进程汇总输出
    collab_summary = None
    ego_summary = None
    while not result_queue.empty():
        try:
            tag, data = result_queue.get_nowait()
            if tag == "collab":
                collab_summary = data
            else:
                ego_summary = data
        except Exception:
            break

    print("\n============== Final Summary ==============")
    if collab_summary is not None:
        print("[Collaborator (Transmitter) Side]")
        print(f"  Sent Payload Frames             : {collab_summary.get('Sent Payload Frames', 0)}")
        print(f"  Valid Collaborative Frames      : {collab_summary.get('Valid Comm Frames', 0)}")
        print(f"  Raw Anchors per Frame           : {collab_summary.get('Raw Anchors per Frame', 0.0)}   (means.shape[1], 未过滤)")
        print(f"  Baseline Gaussians (ROI+opa)    : {collab_summary.get('Baseline Gaussians', 0.0)}   (ego 坐标 ROI + opacity>=0.01)")
        print(f"  Actual Transmitted              : {collab_summary.get('Actual Transmitted', 0.0)}")
        print(f"  Ratio (Actual / Baseline)       : {collab_summary.get('Ratio', '0.00%')}")
        print(f"  Payload KB per Frame            : {collab_summary.get('Payload KB per Frame', 0.0)}   (pickle.dumps 真实字节数)")
        print(f"  Total Transferred               : {collab_summary.get('Total Payload MB', 0.0)} MB")
    else:
        print("[Collaborator (Transmitter) Side] No result.")
    if ego_summary is not None:
        print("\n[Ego Side - Detection / Latency]")
        for k, v in ego_summary.items():
            if k in ("Baseline Gaussians", "Actual Transmitted", "Ratio",
                     "Raw Anchors per Frame", "Payload KB per Frame",
                     "Total Payload MB", "Sent Payload Frames", "Valid Comm Frames"):
                continue
            print(f"  {k}: {v}")
    else:
        print("\n[Ego Side - Detection / Latency] No result.")
    print("============================================")
    print("Simulation finished!")

if __name__ == "__main__":
    main()
