# syntax=docker/dockerfile:1
FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PATH="/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip ffmpeg libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv
RUN pip install --no-cache-dir \
        torch==2.1.2+cu121 torchvision==0.16.2+cu121 \
        --index-url https://download.pytorch.org/whl/cu121

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY fast_api/ ./fast_api/
COPY runtime_api/ ./runtime_api/
COPY utils/ ./utils/
COPY k8s/ ./k8s/
COPY common/ ./common/
COPY roadside/ ./roadside/
COPY vendor/ ./vendor/
COPY config.json config.mock.json ./
COPY --from=weights /qwen/ ./weights/qwen/
COPY --from=weights /compression/ ./weights/compression/

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9001
CMD ["python", "main.py"]
