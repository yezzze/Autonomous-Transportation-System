# syntax=docker/dockerfile:1.7

# VOGS uses CUDA extensions built against PyTorch 1.12 / CUDA 11.6.
FROM nvidia/cuda:11.6.2-cudnn8-devel-ubuntu20.04

ARG DEBIAN_FRONTEND=noninteractive
ARG CONDA_VERSION=py310_24.7.1-0
ARG TORCH_CUDA_ARCH_LIST="7.0;7.5;8.0;8.6+PTX"
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FORCE_CUDA=1 \
    TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} \
    CUDA_HOME=/usr/local/cuda \
    PATH=/opt/conda/bin:${PATH} \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake curl git libglib2.0-0 libgl1 \
        libsm6 libxext6 libxrender1 ninja-build wget \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://repo.anaconda.com/miniconda/Miniconda3-${CONDA_VERSION}-Linux-x86_64.sh" -o /tmp/miniconda.sh \
    && bash /tmp/miniconda.sh -b -p /opt/conda \
    && rm /tmp/miniconda.sh \
    && conda clean -afy

# Keep the Python/CUDA ABI aligned with the extensions shipped by VOGS.
RUN conda install -y -c pytorch -c conda-forge \
        python=3.10 \
        pytorch=1.12.0 \
        torchvision=0.13.0 \
        torchaudio=0.12.0 \
        cudatoolkit=11.6 \
    && conda clean -afy \
    && python -m pip install --upgrade "pip<25" "setuptools<70" wheel

WORKDIR /app

COPY requirements.txt requirements_GsSCE.txt ./

RUN python -m pip install -r requirements_GsSCE.txt -r requirements.txt \
    && python -m pip install \
        "a2a-sdk==1.1.2" \
        "fastapi==0.141.1" \
        "nats-py==2.15.0" \
        "pydantic==2.13.4" \
        "uvicorn==0.52.3" \
        "numpy==1.26.4" \
        "opencv-python-headless==4.8.1.78" \
        "spconv-cu116==2.3.6" \
        "cumm-cu116==0.4.11" \
        "openmim==0.3.9" \
        "https://data.pyg.org/whl/torch-1.12.0%2Bcu116/torch_scatter-2.1.0%2Bpt112cu116-cp310-cp310-linux_x86_64.whl" \
        "https://data.pyg.org/whl/torch-1.12.0%2Bcu116/torch_cluster-1.6.0%2Bpt112cu116-cp310-cp310-linux_x86_64.whl" \
    && mim install "mmengine==0.10.7" \
    && mim install "mmcv==2.1.0" \
    && mim install "mmdet==3.3.0" \
    && mim install "mmsegmentation==1.2.2" \
    && mim install "mmdet3d==1.4.0"

COPY . .

# Never reuse host-built extension binaries: compile all required extensions
# against the exact Python, PyTorch and CUDA versions in this image.
RUN find /app -type f -name '*.so' -delete \
    && python opencood/utils/setup.py build_ext --inplace \
    && python opencood/pcdet_utils/setup.py build_ext --inplace \
    && python -m pip install --no-build-isolation ./pointops \
    && cd /app/opencood/models/gaussian_modules/ops \
    && python setup.py build_ext --inplace \
    && cd /app \
    && python -m pip install --no-build-isolation ./opencood/models/gaussian_modules/localagg \
    && python -c "import torch, mmcv, spconv.pytorch, torch_scatter, torch_cluster; import opencood.models.gaussian_modules.ops.deformable_aggregation_ext; import local_aggregate; import opencood.models.gaussian_collab_sce; print('VOGS runtime dependencies built successfully')"

RUN groupadd --gid 1000 vogs \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash vogs \
    && mkdir -p /app/output_results \
    && chown -R vogs:vogs /app

ENV A2A_AGENT_URL=http://localhost:9001 \
    NATS_SERVER_URL=nats://nats:4222 \
    AGENT_ID=vogs-ego-agent \
    AGENT_MAX_CONCURRENT_TASKS=1 \
    LOAD_MODEL=ours \
    NUM_FRAMES=100 \
    TEST_DIR=/dataset/OPV2V/validate \
    OUTPUT_DIR=/app/output_results

USER vogs
EXPOSE 9001

CMD ["python", "main.py"]
