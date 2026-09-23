#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import cv2
import numpy as np
import timm
import torch


MEAN = np.asarray((0.485, 0.456, 0.406), dtype=np.float32).reshape(1, 3, 1, 1)
STD = np.asarray((0.229, 0.224, 0.225), dtype=np.float32).reshape(1, 3, 1, 1)


def build_model(checkpoint, device):
    try:
        model = timm.create_model(
            "mobilenetv3_small_100.lamb_in1k", pretrained=False, num_classes=2
        )
    except Exception:
        model = timm.create_model(
            "mobilenetv3_small_100", pretrained=False, num_classes=2
        )
    state = torch.load(checkpoint, map_location=device)
    if "model" in state:
        state = state["model"]
    model.load_state_dict(state)
    model = model.eval().to(device)
    if device.type == "cuda":
        model = model.to(memory_format=torch.channels_last)
    return model


def select_peak_segment(times, probs, threshold, video_duration, clip_ratio):
    """Select one fixed-ratio clip centered on the highest accident score."""
    peak_index = int(np.argmax(probs))
    peak_time = float(times[peak_index])
    peak_prob = float(probs[peak_index])
    if peak_prob < threshold:
        return peak_time, peak_prob, []

    clip_duration = min(video_duration, video_duration * clip_ratio)
    start = peak_time - clip_duration / 2.0
    end = start + clip_duration

    if start < 0.0:
        start = 0.0
        end = clip_duration
    if end > video_duration:
        end = video_duration
        start = max(0.0, end - clip_duration)

    return peak_time, peak_prob, [(round(start, 3), round(end, 3))]


def make_sample_indices(total_frames, source_fps, sample_fps):
    if total_frames <= 0:
        return np.asarray([], dtype=np.int64)
    duration = total_frames / source_fps
    sample_count = max(1, int(np.ceil(duration * sample_fps)))
    indices = np.rint(
        np.arange(sample_count, dtype=np.float64) * source_fps / sample_fps
    ).astype(np.int64)
    return np.unique(np.clip(indices, 0, total_frames - 1))


def open_decoder(video_path, decoder_name):
    if decoder_name in ("auto", "ffmpeg") and shutil.which("ffmpeg"):
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError("cannot open video: %s" % video_path)
        source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        capture.release()
        return "ffmpeg", str(video_path), source_fps, total_frames

    if decoder_name in ("auto", "decord"):
        try:
            from decord import VideoReader, cpu

            reader = VideoReader(str(video_path), ctx=cpu(0), num_threads=4)
            source_fps = float(reader.get_avg_fps() or 25.0)
            return "decord", reader, source_fps, len(reader)
        except Exception:
            if decoder_name == "decord":
                raise

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError("cannot open video: %s" % video_path)
    source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    return "opencv", capture, source_fps, total_frames


def close_decoder(decoder_name, reader):
    if decoder_name == "opencv":
        reader.release()


def decode_ffmpeg_video(
    video_path,
    sample_fps,
    size=224,
    fill=114,
    preserve_aspect_output=False,
):
    if preserve_aspect_output:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError("cannot probe video dimensions: %s" % video_path)
        source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        capture.release()
        if source_width <= 0 or source_height <= 0:
            raise RuntimeError("invalid video dimensions: %s" % video_path)
        scale = min(size / source_width, size / source_height)
        output_width = max(1, int(round(source_width * scale)))
        output_height = max(1, int(round(source_height * scale)))
        video_filter = (
            "fps=%.8f,scale=%d:%d:flags=bilinear"
            % (sample_fps, output_width, output_height)
        )
    else:
        output_width = size
        output_height = size
        color = "0x%02x%02x%02x" % (fill, fill, fill)
        video_filter = (
            "fps=%.8f," % sample_fps
            + "scale=%d:%d:force_original_aspect_ratio=decrease:flags=bilinear,"
            % (size, size)
            + "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=%s" % (size, size, color)
        )
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            video_filter,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    frame_bytes = output_width * output_height * 3
    if len(result.stdout) % frame_bytes != 0:
        raise RuntimeError("ffmpeg returned an incomplete raw frame")
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(
        -1,
        output_height,
        output_width,
        3,
    )


def decode_batch(decoder_name, reader, indices):
    if decoder_name == "decord":
        frames = reader.get_batch(indices.astype(np.int64)).asnumpy()
        return frames[..., :3]

    frames = []
    for frame_index in indices:
        reader.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
        ok, frame = reader.read()
        if not ok:
            raise RuntimeError("cannot decode frame %d" % frame_index)
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    return np.stack(frames)


def letterbox_batch(frames, size=224, fill=114):
    """Preserve each RGB frame's full field of view and return NCHW float32."""
    output = np.full((len(frames), size, size, 3), fill, dtype=np.uint8)
    for index, frame in enumerate(frames):
        height, width = frame.shape[:2]
        scale = min(size / width, size / height)
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        resized = cv2.resize(
            frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR
        )
        left = (size - resized_width) // 2
        top = (size - resized_height) // 2
        output[
            index,
            top : top + resized_height,
            left : left + resized_width,
        ] = resized

    output = output.transpose(0, 3, 1, 2).astype(np.float32) / 255.0
    return (output - MEAN) / STD


def normalize_letterboxed_batch(frames):
    output = frames.transpose(0, 3, 1, 2).astype(np.float32) / 255.0
    return (output - MEAN) / STD


def warm_up_model(model, device, use_fp16, batch_size):
    if device.type != "cuda":
        return
    warmup_size = min(max(1, batch_size), 8)
    sample = torch.zeros((warmup_size, 3, 224, 224), device=device)
    sample = sample.to(memory_format=torch.channels_last)
    with torch.inference_mode(), torch.autocast(
        device_type="cuda", dtype=torch.float16, enabled=use_fp16
    ):
        model(sample)
    torch.cuda.synchronize()


def score_video(
    video_path,
    model,
    device,
    sample_fps,
    batch_size,
    decoder_name,
    use_fp16,
    return_frames=False,
    return_frame_size=224,
):
    scan_started = time.perf_counter()
    setup_started = time.perf_counter()
    actual_decoder, reader, source_fps, total_frames = open_decoder(
        video_path, decoder_name
    )
    decoder_setup_ms = (time.perf_counter() - setup_started) * 1000.0

    ffmpeg_frames = None
    indices = None
    if actual_decoder == "ffmpeg":
        stage_started = time.perf_counter()
        ffmpeg_frames = decode_ffmpeg_video(
            video_path,
            sample_fps,
            size=return_frame_size if return_frames else 224,
            preserve_aspect_output=return_frames and return_frame_size != 224,
        )
        decode_preprocess_ms = (time.perf_counter() - stage_started) * 1000.0
        times = (np.arange(len(ffmpeg_frames), dtype=np.float64) / sample_fps).tolist()
        sampled_frame_count = len(ffmpeg_frames)
    else:
        indices = make_sample_indices(total_frames, source_fps, sample_fps)
        if len(indices) == 0:
            close_decoder(actual_decoder, reader)
            raise RuntimeError("no frames available in video")
        times = (indices.astype(np.float64) / source_fps).tolist()
        sampled_frame_count = len(indices)
        decode_preprocess_ms = 0.0

    if sampled_frame_count == 0:
        close_decoder(actual_decoder, reader)
        raise RuntimeError("no sampled frames decoded from video")

    probs = []
    model_inference_ms = 0.0

    try:
        for offset in range(0, sampled_frame_count, batch_size):

            stage_started = time.perf_counter()
            if actual_decoder == "ffmpeg":
                frames = ffmpeg_frames[offset : offset + batch_size]
                if frames.shape[1:3] == (224, 224):
                    batch_array = normalize_letterboxed_batch(frames)
                else:
                    batch_array = letterbox_batch(frames)
            else:
                batch_indices = indices[offset : offset + batch_size]
                frames = decode_batch(actual_decoder, reader, batch_indices)
                batch_array = letterbox_batch(frames)
            tensor = torch.from_numpy(np.ascontiguousarray(batch_array))
            if device.type == "cuda":
                tensor = tensor.pin_memory().to(device, non_blocking=True)
                tensor = tensor.to(memory_format=torch.channels_last)
            else:
                tensor = tensor.to(device)
            if device.type == "cuda":
                torch.cuda.synchronize()
            decode_preprocess_ms += (time.perf_counter() - stage_started) * 1000.0

            stage_started = time.perf_counter()
            with torch.inference_mode(), torch.autocast(
                device_type=device.type,
                dtype=torch.float16,
                enabled=device.type == "cuda" and use_fp16,
            ):
                logits = model(tensor)
            batch_probs = torch.softmax(logits.float(), dim=1)[:, 1]
            probs.extend(batch_probs.cpu().numpy().tolist())
            if device.type == "cuda":
                torch.cuda.synchronize()
            model_inference_ms += (time.perf_counter() - stage_started) * 1000.0
    finally:
        close_decoder(actual_decoder, reader)

    scan_total_ms = (time.perf_counter() - scan_started) * 1000.0
    video_duration = total_frames / source_fps
    result = {
        "times": times,
        "probs": probs,
        "video_duration": video_duration,
        "source_fps": source_fps,
        "source_frame_count": total_frames,
        "sampled_frame_count": sampled_frame_count,
        "decoder": actual_decoder,
        "decoder_setup_ms": decoder_setup_ms,
        "decode_preprocess_ms": decode_preprocess_ms,
        "model_inference_ms": model_inference_ms,
        "scan_total_ms": scan_total_ms,
    }
    if return_frames:
        if actual_decoder != "ffmpeg" or ffmpeg_frames is None:
            raise RuntimeError(
                "return_frames requires the ffmpeg decoder so decoded frames can be reused"
            )
        result["sampled_frames_rgb"] = ffmpeg_frames
        result["returned_frame_size"] = list(ffmpeg_frames.shape[1:3])
    return result


def available_ffmpeg_encoders():
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def export_clip(video_path, clip_path, start, end, encoder_name):
    if encoder_name == "auto":
        encoders = available_ffmpeg_encoders()
        candidates = ["h264_nvenc", "libx264"] if "h264_nvenc" in encoders else ["libx264"]
    else:
        candidates = [encoder_name]

    last_error = None
    duration = max(0.001, end - start)
    for encoder in candidates:
        codec_args = (
            ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
            if encoder == "h264_nvenc"
            else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        )
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            "%.3f" % start,
            "-i",
            str(video_path),
            "-t",
            "%.3f" % duration,
            "-map",
            "0:v:0",
            "-an",
            *codec_args,
            "-pix_fmt",
            "yuv420p",
            str(clip_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return encoder
        last_error = result.stderr

    raise RuntimeError("ffmpeg clip export failed: %s" % last_error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--out", default="./clips")
    parser.add_argument("--sample-fps", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--decoder",
        choices=("auto", "ffmpeg", "decord", "opencv"),
        default="auto",
    )
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument(
        "--clip-ratio",
        type=float,
        default=0.2,
        help="output duration as a fraction of the original video",
    )
    parser.add_argument(
        "--smooth-window",
        type=int,
        default=0,
        help="probability smoothing samples; 0 chooses about 0.6 seconds automatically",
    )
    parser.add_argument("--no-fp16", action="store_true")
    parser.add_argument(
        "--video-encoder",
        choices=("auto", "h264_nvenc", "libx264"),
        default="auto",
    )
    args = parser.parse_args()
    if not 0.0 < args.clip_ratio <= 1.0:
        parser.error("--clip-ratio must be in the interval (0, 1]")
    if args.sample_fps <= 0.0:
        parser.error("--sample-fps must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.smooth_window < 0:
        parser.error("--smooth-window cannot be negative")

    smooth_window = args.smooth_window
    if smooth_window == 0:
        smooth_window = max(1, int(round(args.sample_fps * 0.6)))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    use_fp16 = device.type == "cuda" and not args.no_fp16
    model = build_model(args.checkpoint, device)
    warm_up_model(model, device, use_fp16, args.batch_size)

    scored = score_video(
        args.video,
        model,
        device,
        args.sample_fps,
        args.batch_size,
        args.decoder,
        use_fp16,
    )
    times = scored.pop("times")
    probs = scored.pop("probs")

    postprocess_started = time.perf_counter()
    if smooth_window > 1:
        kernel = np.ones(smooth_window) / smooth_window
        probs = np.convolve(probs, kernel, mode="same")

    top = sorted(zip(times, probs), key=lambda item: -item[1])[:5]
    peak_time, max_prob, segments = select_peak_segment(
        times,
        probs,
        args.threshold,
        scored["video_duration"],
        args.clip_ratio,
    )
    postprocess_ms = (time.perf_counter() - postprocess_started) * 1000.0

    print("runtime decoder=%s batch_size=%d fp16=%s" % (scored["decoder"], args.batch_size, use_fp16))
    print("top_probs=%s" % [(round(t, 3), round(float(p), 4)) for t, p in top])
    print("max_accident_prob=%.4f peak_time=%.3f" % (max_prob, peak_time))
    print(
        "scan_total_ms=%.1f decode_preprocess_ms=%.1f model_inference_ms=%.1f sampled_frames=%d"
        % (
            scored["scan_total_ms"],
            scored["decode_preprocess_ms"],
            scored["model_inference_ms"],
            scored["sampled_frame_count"],
        )
    )

    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_export_started = time.perf_counter()
    clip_encoder = None
    for index, (start, end) in enumerate(segments):
        clip_path = output_dir / ("clip_%02d.mp4" % index)
        clip_encoder = export_clip(
            args.video, clip_path, start, end, args.video_encoder
        )
        print("segment start=%.3f end=%.3f encoder=%s -> %s" % (start, end, clip_encoder, clip_path))
    clip_export_ms = (time.perf_counter() - clip_export_started) * 1000.0
    total_ms = scored["scan_total_ms"] + postprocess_ms + clip_export_ms
    print(
        "postprocess_ms=%.1f clip_export_ms=%.1f total_ms=%.1f"
        % (postprocess_ms, clip_export_ms, total_ms)
    )

    result = {
        "segments": segments,
        "peak_time": peak_time,
        "max_accident_prob": max_prob,
        "video_duration": scored["video_duration"],
        "clip_ratio": args.clip_ratio,
        "sample_fps": args.sample_fps,
        "smooth_window": smooth_window,
        "batch_size": args.batch_size,
        "fp16": use_fp16,
        "clip_encoder": clip_encoder,
        "postprocess_ms": postprocess_ms,
        "clip_export_ms": clip_export_ms,
        "total_ms": total_ms,
        **scored,
    }
    with (output_dir / "segments.json").open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)


if __name__ == "__main__":
    main()
