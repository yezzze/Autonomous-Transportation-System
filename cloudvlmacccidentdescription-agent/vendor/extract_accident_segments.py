import os
import argparse
import subprocess
from typing import List, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from decord import VideoReader
from transformers import VideoMAEImageProcessor

from .detect_videoMAEv2 import Config, VideoAnomalyDetector
import cv2


def load_model_and_processor(cfg: Config, checkpoint_path: str, device: torch.device):
    processor = VideoMAEImageProcessor.from_pretrained(cfg.model_name,local_files_only=True)
    model = VideoAnomalyDetector(cfg).to(device)
    checkpoint = torch.load(checkpoint_path, map_location=device)
    state_dict = checkpoint
    if isinstance(checkpoint, dict):
        if isinstance(checkpoint.get("model_state_dict"), dict):
            state_dict = checkpoint["model_state_dict"]
        elif isinstance(checkpoint.get("state_dict"), dict):
            state_dict = checkpoint["state_dict"]
    if not isinstance(state_dict, dict):
        raise TypeError(f"检查点不包含有效state_dict: {checkpoint_path}")
    state_dict = {
        key.removeprefix("module."): value
        for key, value in state_dict.items()
        if isinstance(value, torch.Tensor)
    }
    model.load_state_dict(state_dict)
    model.eval()
    return model, processor




@torch.inference_mode()
def score_windows(vr, model, processor, window_frames, stride_frames, device, debug=False):
    total = len(vr)
    indices_list, scores = [], []

    i = 0
    while i + window_frames <= total:
        idx = np.arange(i, i + window_frames, dtype=np.int32)
        frames = vr.get_batch(idx).asnumpy()  # (T, H, W, C)
        if frames.dtype != np.uint8:
            frames = frames.astype(np.uint8)
        if frames.shape[-1] == 1:
            frames = np.repeat(frames, 3, axis=-1)
        elif frames.shape[-1] == 4:
            frames = frames[..., :3]

        inputs = processor(list(frames), return_tensors="pt")
        pix = inputs["pixel_values"]  # shape: (1, 16, 3, 224, 224)
        x = pix.to(device)  # → (1, 3, 16, 224, 224)

        if debug:
            print("[DEBUG] frames:", frames.shape, frames.dtype)
            print("[DEBUG] raw pixel_values shape:", tuple(pix.shape))
            print("[DEBUG] model input shape:", tuple(x.shape))

        logits = model(pixel_values=x)  # ✅ 显式传参
        prob_anomaly = F.softmax(logits, dim=1)[0, 1].item()

        indices_list.append((i, i + window_frames))
        scores.append(prob_anomaly)
        i += stride_frames

    # 可选：补尾
    if total >= window_frames and i < total and (total - window_frames) > 0 and (total - i) >= window_frames // 2:
        i = total - window_frames
        idx = np.arange(i, i + window_frames, dtype=np.int32)
        frames = vr.get_batch(idx).asnumpy()
        if frames.dtype != np.uint8:
            frames = frames.astype(np.uint8)
        if frames.shape[-1] == 1:
            frames = np.repeat(frames, 3, axis=-1)
        elif frames.shape[-1] == 4:
            frames = frames[..., :3]

        inputs = processor(list(frames), return_tensors="pt")
        pix = inputs["pixel_values"]
        x = pix.to(device)  # (1, 3, 16, 224, 224)

        logits = model(pixel_values=x)
        prob_anomaly = F.softmax(logits, dim=1)[0, 1].item()
        indices_list.append((i, i + window_frames))
        scores.append(prob_anomaly)

    return indices_list, scores


def merge_segments(
    indices_list: List[Tuple[int, int]],
    scores: List[float],
    fps: float,
    threshold: float,
    pad_sec: float,
) -> List[Tuple[float, float]]:
    # 合并连续超过阈值的窗口
    raw_segments: List[Tuple[int, int]] = []
    current = None
    for (s, e), p in zip(indices_list, scores):
        if p >= threshold:
            if current is None:
                current = [s, e]
            else:
                current[1] = e
        else:
            if current is not None:
                raw_segments.append((current[0], current[1]))
                current = None
    if current is not None:
        raw_segments.append((current[0], current[1]))

    # 转为秒并添加前后 padding，同时合并相邻片段
    merged: List[List[float]] = []
    for s, e in raw_segments:
        ss = max(0.0, s / fps - pad_sec)
        ee = e / fps + pad_sec
        if not merged or ss > merged[-1][1] + 0.3:  # 0.3s 间隙阈值
            merged.append([ss, ee])
        else:
            merged[-1][1] = max(merged[-1][1], ee)

    return [(round(s, 2), round(e, 2)) for s, e in merged]

def cut_segments_with_opencv(src: str, out_dir: str, segments: List[Tuple[float, float]], fps: float) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    outputs: List[str] = []

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频文件: {src}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    for i, (s, e) in enumerate(segments, 1):
        start_frame = max(0, int(round(s * fps)))
        end_frame = min(total_frames - 1, int(round(e * fps)))
        if end_frame <= start_frame:
            continue

        # 优先尝试写 mp4
        out_mp4 = os.path.join(out_dir, f"accident_{i:02d}.mp4")
        fourcc_mp4 = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(out_mp4, fourcc_mp4, fps, (width, height))

        # 如果 mp4 写入器创建失败，回退到 avi (MJPG)
        out_path = out_mp4
        if not writer.isOpened():
            out_avi = os.path.join(out_dir, f"accident_{i:02d}.avi")
            fourcc_avi = cv2.VideoWriter_fourcc(*'MJPG')
            writer = cv2.VideoWriter(out_avi, fourcc_avi, fps, (width, height))
            out_path = out_avi

        if not writer.isOpened():
            raise RuntimeError("无法创建视频写入器，可能缺少编码器。")

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        current = start_frame
        while current <= end_frame:
            ret, frame = cap.read()
            if not ret:
                break
            writer.write(frame)
            current += 1

        writer.release()
        outputs.append(out_path)

    cap.release()
    return outputs


def main():
    parser = argparse.ArgumentParser(description="使用已训练的异常检测模型，从长视频中截取事故片段")
    parser.add_argument("--video", required=True, help="输入视频路径")
    parser.add_argument("--checkpoint", default="./checkpoints/best_model.pth", help="模型权重路径")
    parser.add_argument("--out_dir", default="./accident_clips", help="输出片段目录")
    parser.add_argument("--window", type=int, default=16, help="滑窗帧数")
    parser.add_argument("--stride", type=int, default=8, help="滑窗步长(帧)")
    parser.add_argument("--threshold", type=float, default=0.6, help="异常概率阈值")
    parser.add_argument("--pad", type=float, default=2.0, help="片段前后扩展秒数")
    parser.add_argument("--debug", action="store_true", help="打印中间张量形状以定位错误")
    args = parser.parse_args()

    cfg = Config()
    device = cfg.device if hasattr(cfg, "device") else torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if not os.path.isfile(args.video):
        raise FileNotFoundError(f"找不到输入视频: {args.video}")
    if not os.path.isfile(args.checkpoint):
        raise FileNotFoundError(f"找不到模型权重: {args.checkpoint}")

    print("加载模型与处理器...")
    model, processor = load_model_and_processor(cfg, args.checkpoint, device)

    #print(">>> backbone.config =", getattr(model.backbone, "config", None))
    try:
        print(">>> backbone.num_channels =", model.backbone.config.num_channels)
    except Exception as e:
        print(">>> cannot read num_channels:", e)


    print(f"读取视频: {args.video}")
    vr = VideoReader(args.video)
    fps = vr.get_avg_fps() or 25.0

    print("滑窗打分...")
    idx_list, scores = score_windows(
        vr=vr,
        model=model,
        processor=processor,
        window_frames=args.window,
        stride_frames=args.stride,
        device=device,
        debug=args.debug,
    )



    print("生成片段...")
    segments = merge_segments(
        indices_list=idx_list,
        scores=scores,
        fps=fps,
        threshold=args.threshold,
        pad_sec=args.pad,
    )

    if not segments:
        print("未检测到事故片段。")
        return

    print("检测到片段(秒):", segments)
    print("导出片段(离线, OpenCV)...")
    outputs = cut_segments_with_opencv(args.video, args.out_dir, segments, fps)
    print("导出完成:")
    for p in outputs:
        print(p)


if __name__ == "__main__":
    main()


