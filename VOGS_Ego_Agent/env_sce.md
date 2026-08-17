# VOGS-CP (`vogs_exp`) Environment Configuration Guide

This document provides step-by-step instructions for configuring the `vogs_exp` Conda environment on an Ubuntu machine to run the VOGS-CP codebase. The environment configuration combines dependencies from both the HEAL framework and the VOGS-CP extensions.

## Prerequisites
- Ubuntu OS
- Anaconda or Miniconda installed
- NVIDIA GPU with CUDA drivers compatible with **CUDA 11.6**

---

## 1. Create and Activate the Conda Environment
Create a new Conda environment with Python 3.8:
```bash
conda create -n vogs_exp python=3.8 -y
conda activate vogs_exp
```

## 2. Install PyTorch and CUDA Toolkit
Install PyTorch 1.12.0 along with the matching TorchVision, TorchAudio, and CUDA 11.6 toolkit:
```bash
conda install pytorch==1.12.0 torchvision==0.13.0 torchaudio==0.12.0 cudatoolkit=11.6 -c pytorch -c conda-forge -y
```

## 3. Install Core Python Dependencies
Install the basic Python packages from the provided requirements file (which is a union of HEAL and VOGS requirements):
```bash
pip install -r requirements_GsSCE.txt
```

## 4. Install Spconv and PyG (Torch Scatter/Cluster)
Install the CUDA 11.6 pre-built wheel of `spconv` and the PyTorch Geometric dependencies required for point cloud operations.
**Important Note:** To avoid installing CPU-only versions of `torch-scatter` and `torch-cluster`, we recommend downloading the pre-built `.whl` files directly and installing them to ensure CUDA support is properly linked.

```bash
pip install spconv-cu116==2.3.6

# Download and install CUDA-enabled torch-scatter and torch-cluster
wget https://data.pyg.org/whl/torch-1.12.0%2Bcu116/torch_scatter-2.1.0%2Bpt112cu116-cp38-cp38-linux_x86_64.whl
wget https://data.pyg.org/whl/torch-1.12.0%2Bcu116/torch_cluster-1.6.0%2Bpt112cu116-cp38-cp38-linux_x86_64.whl
pip install torch_scatter-2.1.0+pt112cu116-cp38-cp38-linux_x86_64.whl torch_cluster-1.6.0+pt112cu116-cp38-cp38-linux_x86_64.whl
```

## 5. Compile HEAL CUDA Extensions
Based on the HEAL framework setup, you need to compile the CUDA extensions for Bounding Box NMS and FPV-RCNN dependencies.
**Important Note:** PyTorch 1.12.0 has removed the legacy `THC/THC.h` headers. Before compiling, you must remove these includes from the C++ source files. Additionally, explicitly set the `TORCH_CUDA_ARCH_LIST` and `FORCE_CUDA` environment variables to avoid version mismatch errors.

```bash
# Remove deprecated THC/THC.h includes
sed -i 's/#include <THC\/THC.h>//g' opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cpp opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cu
sed -i 's/#include <THC\/THC.h>//g' opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cpp opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cu
sed -i 's/extern THCState \*state;/\/\/ extern THCState \*state;/g' opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cpp opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cpp

export FORCE_CUDA="1"
export TORCH_CUDA_ARCH_LIST="8.0;8.6"

# Compile bbx nms calculation cuda version
python opencood/utils/setup.py build_ext --inplace

# Compile dependencies for fpv-rcnn
python opencood/pcdet_utils/setup.py build_ext --inplace
```

## 6. Install OpenMMLab Dependencies
VOGS-CP additionally requires specific versions of OpenMMLab packages for deformable attention and other operations. We use `mim` for a clean installation:
```bash
pip install openmim
mim install mmengine==0.10.7
mim install mmcv==2.1.0
mim install mmdet==3.3.0
mim install mmsegmentation==1.2.2
mim install mmdet3d==1.4.0
```

**Note:** If you encounter `AttributeError: module 'cv2.dnn' has no attribute 'DictValue'` during OpenMMLab imports, ensure `opencv-python-headless>=4.6.0.66` is installed.

## 7. Compile VOGS-CP Local CUDA Extensions
There are two local custom CUDA modules introduced by VOGS-CP that need to be compiled (`ops` for deformable attention and `localagg` for Gaussian-to-voxel splatting):
```bash
# Compile deformable attention ops
cd opencood/models/gaussian_modules/ops
pip install -e .
cd ../../../../

# Compile gaussian-to-voxel splatting localagg
cd opencood/models/gaussian_modules/localagg
pip install -e .
cd ../../../../
```

## 8. Install OpenCOOD
Finally, install the core `OpenCOOD` framework in development mode so that the `opencood` module can be imported anywhere:
```bash
python setup.py develop
# or equivalently: pip install -e .
```

---

## Verification
To ensure the environment is set up correctly, you can run a quick check in Python:
```python
import torch
import spconv
import mmcv
import opencood

print("PyTorch version:", torch.__version__)
print("CUDA available:", torch.cuda.is_available())
print("Environment setup successful!")
```
