#!/bin/bash
set -e
eval "$(conda shell.bash hook)"

conda activate vogs_dist

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
    
    # Clean up THC/THC.h in all pcdet_utils
    find opencood/pcdet_utils -type f \( -name "*.cpp" -o -name "*.cu" -o -name "*.h" \) -exec sed -i 's/.*THC\/THC\.h.*//g' {} +
    
    # Replace THCState
    find opencood/pcdet_utils -type f \( -name "*.cpp" -o -name "*.cu" -o -name "*.h" \) -exec sed -i 's/extern THCState \*state;/\/\/ extern THCState \*state;/g' {} +
    find opencood/pcdet_utils -type f \( -name "*.cpp" -o -name "*.cu" -o -name "*.h" \) -exec sed -i 's/THCState_getCurrentStream(state)/at::cuda::getCurrentCUDAStream()/g' {} +

    python opencood/utils/setup.py build_ext --inplace
    python opencood/pcdet_utils/setup.py build_ext --inplace

    cd opencood/models/gaussian_modules/ops
    python setup.py build_ext --inplace
    cd ../../../../

    cd opencood/models/gaussian_modules/localagg
    python setup.py build_ext --inplace
    cd ../../../../

    if [ -f "setup.py" ]; then
        python setup.py develop
    fi
    cd /home/sxy/Autonomous-Transportation-System
}

compile_opencood "/home/sxy/Autonomous-Transportation-System/VOGS_Collaborator_Agent"
compile_opencood "/home/sxy/Autonomous-Transportation-System/VOGS_Ego_Agent"

echo "ALL DONE!"
