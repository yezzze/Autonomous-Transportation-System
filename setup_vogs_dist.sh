#!/bin/bash
set -e
eval "$(conda shell.bash hook)"

conda activate vogs_dist

echo "Installing OpenMMLab..."
pip install openmim
mim install mmengine==0.10.7
mim install mmcv==2.1.0
mim install mmdet==3.3.0
mim install mmsegmentation==1.2.2
mim install mmdet3d==1.4.0

export FORCE_CUDA="1"
export TORCH_CUDA_ARCH_LIST="8.0;8.6"
export CC=gcc-11
export CXX=g++-11

compile_opencood() {
    local DIR=$1
    echo "========================================="
    echo "Compiling in $DIR..."
    echo "========================================="
    cd $DIR
    rm -rf build opencood/utils/*.so opencood/pcdet_utils/*.so opencood/models/gaussian_modules/ops/build opencood/models/gaussian_modules/localagg/build
    
    sed -i 's/#include <THC\/THC.h>//g' opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cpp opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cu 2>/dev/null || true
    sed -i 's/#include <THC\/THC.h>//g' opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cpp opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cu 2>/dev/null || true
    sed -i 's/extern THCState \*state;/\/\/ extern THCState \*state;/g' opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cpp opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cpp 2>/dev/null || true

    python opencood/utils/setup.py build_ext --inplace
    python opencood/pcdet_utils/setup.py build_ext --inplace

    cd opencood/models/gaussian_modules/ops
    pip install . --no-build-isolation
    cd ../../../../

    cd opencood/models/gaussian_modules/localagg
    pip install . --no-build-isolation
    cd ../../../../

    python setup.py develop
    cd /home/sxy/Autonomous-Transportation-System
}

compile_opencood "/home/sxy/Autonomous-Transportation-System/VOGS_opencood"
compile_opencood "/home/sxy/Autonomous-Transportation-System/VOGS_Collaborator_Agent"
compile_opencood "/home/sxy/Autonomous-Transportation-System/VOGS_Ego_Agent"

echo "ALL DONE!"