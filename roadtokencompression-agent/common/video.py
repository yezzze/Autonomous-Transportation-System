from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import numpy as np

if TYPE_CHECKING:
    from PIL import Image


VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def load_image_frame(image_path: str | Path, max_size: int) -> Image.Image:
    from PIL import Image

    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    scale = min(float(max_size) / width, float(max_size) / height, 1.0)
    if scale < 1.0:
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale)))
        )
    return image


def uniform_sample_frames(
    video_path: str | Path,
    num_frames: int,
    max_size: int,
) -> list[Image.Image]:
    import cv2
    from PIL import Image

    path = str(video_path)
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{path}")

    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        capture.release()
        raise RuntimeError(f"视频没有可读取帧：{path}")

    frame_count = max(1, min(int(num_frames), total_frames))
    indices = np.linspace(0, total_frames - 1, frame_count, dtype=np.int64)
    frames: list[Image.Image] = []
    try:
        for index in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
            ok, frame = capture.read()
            if not ok:
                continue
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            width, height = image.size
            scale = min(float(max_size) / width, float(max_size) / height, 1.0)
            if scale < 1.0:
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale)))
                )
            frames.append(image)
    finally:
        capture.release()

    if not frames:
        raise RuntimeError(f"无法从视频读取有效帧：{path}")
    return frames


def sample_frames_from_clips(
    clip_paths: Iterable[str | Path],
    total_frames: int,
    max_size: int,
) -> list[Image.Image]:
    paths = [Path(path) for path in clip_paths]
    if not paths:
        return []
    per_clip = max(1, int(np.ceil(total_frames / len(paths))))
    frames: list[Image.Image] = []
    for path in paths:
        if path.suffix.lower() in IMAGE_SUFFIXES:
            frames.append(load_image_frame(path, max_size))
        else:
            frames.extend(uniform_sample_frames(path, per_clip, max_size))
    if len(frames) > total_frames:
        indices = np.linspace(0, len(frames) - 1, total_frames, dtype=np.int64)
        frames = [frames[int(index)] for index in indices]
    return frames[:total_frames]


def save_upload(file_storage, directory: str | Path) -> tuple[Path, int]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_storage.filename or "upload.mp4").suffix or ".mp4"
    handle = tempfile.NamedTemporaryFile(
        prefix="upload_",
        suffix=suffix,
        dir=directory,
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    file_storage.save(path)
    return path, path.stat().st_size
