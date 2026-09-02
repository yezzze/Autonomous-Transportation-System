# VOGS-Distributed (`vogs_dist`) 环境配置指南

# 前置条件

- Ubuntu 20.04 / 22.04（或兼容的 Linux x86\_64 发行版）
- 已安装 [Anaconda 或 Miniconda](https://docs.conda.io/en/latest/miniconda.html)
- NVIDIA GPU + **与 CUDA 11.6 兼容的驱动**（`nvidia-smi` 中 CUDA Version ≥ 11.6）
- 编译 CUDA 扩展需要系统级 `gcc/g++ ≥ 7.5`（推荐 9.x），并具备 `cmake / ninja / build-essential`

```bash
# 可选：安装系统级编译依赖
sudo apt update && sudo apt install -y build-essential cmake ninja-build \
    libglib2.0-0 libsm6 libxrender1 libxext6 git wget curl
```

> **工作目录约定**：下文命令默认在项目根目录执行，即 `cd /path/to/Autonomous-Transportation-System`。
> 涉及路径时会明确指出相对 `VOGS_opencood/`、`VOGS_Ego_Agent/`、`VOGS_Collaborator_Agent/` 的位置。

***

## 1. 创建并激活 Conda 环境（Python 3.10）

```bash
conda create -n vogs_dist python=3.10 -y
conda activate vogs_dist
```

> 全程请确保 `python --version` 输出 `3.10.x`。

***

## 2. 安装 PyTorch 1.12.0 + CUDA 11.6 Toolkit

与 `vogs_exp` 的 PyTorch 版本保持一致（仅由 conda 自动选择 py3.10 的构建版本）：

```bash
conda install pytorch==1.12.0 torchvision==0.13.0 torchaudio==0.12.0 \
    cudatoolkit=11.6 -c pytorch -c conda-forge -y
```

安装完成后立即校验：

```bash
python -c "import torch; \
    print('Python', __import__('sys').version.split()[0]); \
    print('PyTorch', torch.__version__); \
    print('CUDA available:', torch.cuda.is_available()); \
    print('cuDNN ver:', torch.backends.cudnn.version())"
```

预期输出：

```
Python 3.10.x
PyTorch 1.12.0
CUDA available: True
cuDNN ver: 8302  (即 cuDNN 8.3.2)
```

***

## 3. 安装通用 Python 依赖

复用 `VOGS_opencood/requirements_GsSCE.txt`（该文件是 HEAL 框架 + VOGS-CP 的基础依赖合集）：

```bash
pip install -r VOGS_opencood/requirements_GsSCE.txt
```

> 说明：本机实际冻结中 `Shapely==1.8.5.post1`、`opencv-python-headless==4.8.1.78`、
> `numpy==1.26.4`。如果 `requirements_GsSCE.txt` 指定的版本在 Python 3.10 下出现 wheel 冲突，
> 可改用下面的兜底版本（与 vogs\_dist 实际冻结一致）：
>
> ```bash
> pip install "numpy==1.26.4" "Shapely==1.8.5.post1" \
>     "opencv-python-headless==4.8.1.78" "opencv-python==4.8.1.78"
> ```

***

## 4. 安装 spconv + PyG（torch-scatter / torch-cluster）预编译 CUDA Wheel

同样地，必须安装 CUDA 11.6 的预编译版本，避免退化为 CPU 版本导致后续编译或运行失败。

```bash
pip install spconv-cu116==2.3.6 cumm-cu116==0.4.11

# 下载 PyTorch Geometric 官方托管的 cp310 + pt112cu116 预编译 wheel
wget https://data.pyg.org/whl/torch-1.12.0%2Bcu116/torch_scatter-2.1.0%2Bpt112cu116-cp310-cp310-linux_x86_64.whl
wget https://data.pyg.org/whl/torch-1.12.0%2Bcu116/torch_cluster-1.6.0%2Bpt112cu116-cp310-cp310-linux_x86_64.whl

pip install torch_scatter-2.1.0+pt112cu116-cp310-cp310-linux_x86_64.whl \
            torch_cluster-1.6.0+pt112cu116-cp310-cp310-linux_x86_64.whl
```

校验：

```bash
python -c "import spconv.pytorch; import torch_scatter; import torch_cluster; \
    print('spconv OK'); print('torch_scatter', torch_scatter.__version__); \
    print('torch_cluster', torch_cluster.__version__)"
```

应输出 `torch_scatter 2.1.0+pt112cu116` 与 `torch_cluster 1.6.0+pt112cu116`。

***

## 5. 编译 HEAL 框架自带 CUDA 扩展

> **关键注意**：PyTorch 1.12 已移除旧版 `THC/THC.h` 头，编译前必须先把源码中的这些 include 去掉；
> 另外显式设置 `FORCE_CUDA=1` 与 `TORCH_CUDA_ARCH_LIST` 避免回退 CPU。

**关于在哪些目录编译**：`vogs_dist` 的架构是每个分布式 Agent 子目录（`VOGS_Ego_Agent/`、
`VOGS_Collaborator_Agent/`、以及可选的 `VOGS_opencood/`）**各自携带一份** **`opencood/`** **源码树**，
运行时通过当前工作目录导入。因此编译需要在**所有用到的 Agent 目录**里各跑一次；或者你可以只在
`VOGS_opencood/` 中编译一次，然后把编译产物（`opencood/**/*.so`）拷贝到两个 Agent 的 `opencood/` 目录。

下面以「在单个目录编译」为例，把 `<ROOT>` 替换成你要编译的根目录之一：
`VOGS_opencood`、`VOGS_Ego_Agent` 或 `VOGS_Collaborator_Agent`。

```bash
# 进入目标 Agent 或 VOGS_opencood 根目录
cd <ROOT>

# 1) 删除所有对已废弃 THC/THC.h 的 include
sed -i 's/#include <THC\/THC.h>//g' \
    opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cpp \
    opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cu
sed -i 's/#include <THC\/THC.h>//g' \
    opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cpp \
    opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cu
sed -i 's/extern THCState \*state;/\/\/ extern THCState *state;/g' \
    opencood/pcdet_utils/pointnet2/pointnet2_batch/src/*.cpp \
    opencood/pcdet_utils/pointnet2/pointnet2_stack/src/*.cpp

# 2) 声明强制 CUDA 编译 + 根据你的 GPU 算力声明架构
#    A100: 8.0 ; 3080/3090: 8.6 ; 4090: 8.9 ; V100: 7.0 ; T4: 7.5
export FORCE_CUDA="1"
export TORCH_CUDA_ARCH_LIST="8.0;8.6"

# 3) 编译 bbx nms 计算的 CUDA 版本（输出到 opencood/utils/*.so）
python opencood/utils/setup.py build_ext --inplace

# 4) 编译 fpv-rcnn / pcdet_utils 全家桶（输出到 opencood/pcdet_utils/**/*.so）
python opencood/pcdet_utils/setup.py build_ext --inplace

# 编译完回到项目根
cd -
```

> 若你采用「**一次编译 + 拷贝**」策略，典型命令为：
>
> ```bash
> # 在 VOGS_opencood/ 编译完毕后：
> rsync -av VOGS_opencood/opencood/utils/*.so            VOGS_Ego_Agent/opencood/utils/
> rsync -av VOGS_opencood/opencood/utils/*.so            VOGS_Collaborator_Agent/opencood/utils/
> rsync -av VOGS_opencood/opencood/pcdet_utils/**/*.so   VOGS_Ego_Agent/opencood/pcdet_utils/
> rsync -av VOGS_opencood/opencood/pcdet_utils/**/*.so   VOGS_Collaborator_Agent/opencood/pcdet_utils/
> ```

编译成功后，预期会出现若干 `.cpython-310-x86_64-linux-gnu.so` 文件：

```
opencood/utils/box_overlaps.cpython-310-x86_64-linux-gnu.so
opencood/pcdet_utils/iou3d_nms/iou3d_nms_cuda.cpython-310-*.so
opencood/pcdet_utils/pointnet2/pointnet2_batch/pointnet2_batch_cuda.cpython-310-*.so
opencood/pcdet_utils/pointnet2/pointnet2_stack/pointnet2_stack_cuda.cpython-310-*.so
opencood/pcdet_utils/roiaware_pool3d/roiaware_pool3d_cuda.cpython-310-*.so
```

***

## 6. 安装 OpenMMLab 全家桶

VOGS-CP 模型依赖 OpenMMLab 的 2D/3D 检测与分割库，版本**必须和下述精确一致**，
否则会出现算子接口不兼容：

```bash
pip install "openmim==0.3.9"

mim install "mmengine==0.10.7"
mim install "mmcv==2.1.0"
mim install "mmdet==3.3.0"
mim install "mmsegmentation==1.2.2"
mim install "mmdet3d==1.4.0"
```

> **常见问题**：导入 mmcv 时出现
> `AttributeError: module 'cv2.dnn' has no attribute 'DictValue'`
> 通常是因为系统混用了 `opencv-python` 与 `opencv-python-headless`；
> 重新安装正确版本即可：
>
> ```bash
> pip uninstall -y opencv-python opencv-python-headless
> pip install "opencv-python-headless>=4.6.0.66" "opencv-python>=4.6.0.66"
> ```

校验：

```bash
python -c "import mmcv, mmdet, mmdet3d, mmengine, mmseg; \
    print('mmcv', mmcv.__version__); print('mmdet', mmdet.__version__); \
    print('mmdet3d OK'); print('mmseg', mmseg.__version__)"
```

***

## 7. 编译 VOGS-CP 本地 CUDA 扩展（Deformable Attention + LocalAgg）

这一步编译 VOGS 引入的两类高斯化感知专用 CUDA 算子：

- **`ops/`**：Deformable Attention Aggregation（`deformable-aggregation-ext`）
- **`localagg/`**：Gaussian-to-Voxel Splatting（`local-aggregate`）

> 与 Step 5 同理，每个 Agent 子目录里都有 `opencood/models/gaussian_modules/{ops,localagg}`。
> 你可以在所有子目录各跑一次，或在 `VOGS_opencood/` 编译后把生成的 `.so` 同步过去。
> （本步骤会把编译产物同时**注册到 site-packages**，所以只需编译一次任一目录即可全局 import；
> 但 Agent 子目录「就地」也要有一份同名 `.so` 以保证相对 import 时命中的是同架构的 Py3.10 版本。）

```bash
# 仍然在你要编译的那个 <ROOT>（VOGS_opencood/ 或任一 Agent 目录）中执行
cd <ROOT>

# 7.1 编译 deformable attention ops（会生成 deformable_aggregation_ext.*.so）
cd opencood/models/gaussian_modules/ops
pip install -e .
cd -   # 回到 <ROOT>

# 7.2 编译 gaussian-to-voxel splatting localagg（会生成 local_aggregate/_C.*.so 并注册包）
cd opencood/models/gaussian_modules/localagg
pip install -e .
cd -   # 回到 <ROOT>

# 回到项目根
cd -
```

> 如果两个 Agent 子目录未就地编译，请使用 `rsync` 把 `gaussian_modules/ops/*.so` 和
> `gaussian_modules/localagg/local_aggregate/_C.*.so` 同步到两个 Agent 相同路径。

***

## 8. （可选）全局安装 OpenCOOD 包

`vogs_dist` 中**分布式 Agent 运行本身并不依赖全局安装的** **`opencood`** **包**（它们依靠 cwd 下的
`opencood/` 子目录导入）。但如果你还要使用 `VOGS_opencood/` 下的单机训练 / 评估脚本
（例如 `opencood/tools/train.py`、`test.py`），则执行一次：

```bash
cd VOGS_opencood
python setup.py develop   # 等价于 pip install -e .
cd -
```

这会让 Python 在任意目录下 `import opencood` 都指向 `VOGS_opencood/opencood/`。

***

## 9. 安装分布式接口专用依赖

这是 `vogs_dist` 相比 `vogs_exp` **唯一额外需要安装的软件栈**。包含：

- **FastAPI + Uvicorn**：每个 Agent 对外暴露的 A2A HTTP 服务端
- **nats-py**：Agent 之间基于 NATS JetStream 的高速异步协作特征传输
- **a2a-sdk**：Google A2A 协议 SDK（Agent 之间 `SendMessage` / JSON-RPC / SSE 任务状态流）
- **Pydantic v2**：FastAPI 与 A2A 消息的数据模型
- **httpx / httpcore**：测试脚本中对 Agent 发 HTTP 请求的异步客户端
- **其他异步 / 序列化 / 加锁辅助库**

下面按「主包 + 精确锁定的传递依赖」完整安装，以保证与参考环境字节级一致：

```bash
pip install \
    "nats-py==2.15.0" \
    "fastapi==0.141.1" \
    "uvicorn==0.52.3" \
    "starlette==1.6.0" \
    "httpx==0.28.1" \
    "httpcore==1.0.9" \
    "json-rpc==1.15.0" \
    "sse-starlette==3.4.8" \
    "pydantic==2.13.4" \
    "pydantic_core==2.46.4" \
    "annotated-types==0.8.0" \
    "annotated-doc==0.0.5" \
    "typing-inspection==0.4.4" \
    "anyio==4.14.2" \
    "h11==0.16.0" \
    "sniffio==1.3.1" \
    "janus==2.0.0" \
    "aiologic==0.17.1" \
    "nest-asyncio==1.6.0" \
    "portalocker==4.1.0" \
    "culsans==0.11.0" \
    "a2a-sdk==1.1.2" \
    "google-api-core==2.34.0" \
    "googleapis-common-protos==1.75.1" \
    "proto-plus==1.28.3" \
    "pytokens==0.4.1" \
    "wadler_lindig==0.1.7"
```

> 说明：`a2a-sdk` 在 PyPI 上由 Google 发布，会自动依赖上述大部分包。但为避免上游
> 大版本漂移带来兼容性问题（例如 pydantic v1/v2 混用），强烈建议按上述版本锁死。

分布式栈校验：

```bash
python -c "
import nats;           print('nats-py      :', nats.__version__)
import fastapi;        print('fastapi      :', fastapi.__version__)
import uvicorn;        print('uvicorn      :', uvicorn.__version__)
import starlette;      print('starlette    :', starlette.__version__)
import pydantic;       print('pydantic     :', pydantic.__version__)
import httpx;          print('httpx        :', httpx.__version__)
import a2a;            print('a2a (a2a-sdk):', 'import OK')
from a2a.server.agent_execution import AgentExecutor
from a2a.types import AgentCard, AgentInterface, AgentSkill
print('a2a.server / a2a.types: import OK')
"
```

***

## 10. 获取 NATS Server 可执行文件

分布式运行需要 [NATS Server](https://github.com/nats-io/nats-server) 作为消息中间件（JetStream 模式）。
`local_distributed_test_v3.py` 默认会在 `/tmp/nats-server` 启动该二进制，安装方式：

```bash
# 方式一：官方一键安装脚本（会放到 /usr/local/bin/nats-server，然后按需软链到 /tmp）
curl -sfL https://nats-io.github.io/nsc/nats-server-install.sh | sh
sudo cp /usr/local/bin/nats-server /tmp/nats-server 2>/dev/null || \
    cp $(which nats-server) /tmp/nats-server

# 方式二：直接下载指定版本并放到 /tmp（本机参考版本 v2.10.11）
wget -qO /tmp/nats-server https://github.com/nats-io/nats-server/releases/download/v2.10.11/nats-server-v2.10.11-linux-amd64/nats-server
chmod +x /tmp/nats-server

# 验证
/tmp/nats-server --version   # 应输出 nats-server: v2.10.x
```

> 提示：v2.10.x 中已经默认启用 JetStream 内置域（本项目使用默认域即可，无需配置多域）。

***

## 综合验证

把下面这段保存为 `verify_env.py` 并执行，若全部 `OK` 说明环境已经与 `vogs_dist` 功能等价：

```bash
cat > /tmp/verify_vogs_dist.py <<'PYEOF'
import sys, os, importlib

def ok(name, expr=True):
    try:
        assert bool(expr), expr
        print(f"  [OK] {name}")
    except Exception as e:
        print(f"  [FAIL] {name}: {e!r}")
        sys.exit(1)

print("== 基础运行时 ==")
ok(f"Python 3.10.x: {sys.version.split()[0]}", sys.version_info[:2] == (3,10))

import torch
ok(f"torch==1.12.0, CUDA={torch.cuda.is_available()}", torch.__version__.startswith("1.12.0"))

import spconv.pytorch
ok("spconv.pytorch import")

import torch_scatter, torch_cluster
ok(f"torch_scatter=={torch_scatter.__version__}", torch_scatter.__version__.startswith("2.1.0"))
ok(f"torch_cluster=={torch_cluster.__version__}", torch_cluster.__version__.startswith("1.6.0"))

print("== OpenMMLab ==")
import mmcv, mmdet, mmdet3d, mmengine, mmseg
ok(f"mmcv=={mmcv.__version__}",    mmcv.__version__    == "2.1.0")
ok(f"mmengine=={mmengine.__version__}", mmengine.__version__ == "0.10.7")
ok(f"mmdet=={mmdet.__version__}",  mmdet.__version__   == "3.3.0")
ok(f"mmsegmentation=={mmseg.__version__}", mmseg.__version__ == "1.2.2")
ok("mmdet3d import")

print("== 分布式 Agent 栈 ==")
import nats, fastapi, uvicorn, pydantic, httpx, a2a
ok(f"nats-py=={nats.__version__}",         nats.__version__     == "2.15.0")
ok(f"fastapi=={fastapi.__version__}",      fastapi.__version__  == "0.141.1")
ok(f"uvicorn=={uvicorn.__version__}",      uvicorn.__version__.startswith("0.52"))
ok(f"pydantic=={pydantic.__version__}",    pydantic.__version__ == "2.13.4")
ok(f"httpx=={httpx.__version__}",          httpx.__version__    == "0.28.1")
ok("a2a (a2a-sdk) import")
from a2a.server.agent_execution import AgentExecutor
ok("a2a.server.agent_execution.AgentExecutor")
from a2a.types import AgentCard, AgentInterface, AgentSkill, AgentCapabilities
ok("a2a.types data models")

print("== Agent 本地 opencood + CUDA .so（需要在 Agent 子目录 cwd 下运行） ==")
# 这里传入任一 Agent 目录作为 cwd，示例使用 VOGS_Ego_Agent
AGENT_DIR = os.environ.get("AGENT_DIR", os.path.join(os.path.dirname(__file__), "..", "VOGS_Ego_Agent"))
if os.path.isdir(AGENT_DIR):
    os.chdir(AGENT_DIR)
    import opencood
    ok(f"opencood import (cwd={os.getcwd()})", "opencood" in opencood.__file__)

    # CUDA 扩展模块
    import importlib.util
    so_list = [
        "opencood.utils.box_overlaps",
        "opencood.pcdet_utils.iou3d_nms.iou3d_nms_cuda",
        "opencood.pcdet_utils.pointnet2.pointnet2_batch.pointnet2_batch_cuda",
        "opencood.pcdet_utils.pointnet2.pointnet2_stack.pointnet2_stack_cuda",
        "deformable_aggregation_ext",
        "local_aggregate._C",
    ]
    for mod in so_list:
        try:
            importlib.import_module(mod)
            ok(f"{mod} .so load")
        except Exception as e:
            ok(f"{mod} .so load", False)
else:
    print(f"  (skip opencood check, AGENT_DIR not found: {AGENT_DIR})")

print("\n🎉 vogs_dist environment verification PASSED!")
PYEOF

python /tmp/verify_vogs_dist.py
```

***

## 常见排错速查

| 症状                                                                                 | 解决方式                                                                                                                       |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'opencood'`                                  | 运行模型 / 分布式脚本时需把 cwd 设为 Agent 目录（`VOGS_Ego_Agent` / `VOGS_Collaborator_Agent`），或执行 Step 8 的 `python setup.py develop` 做全局安装 |
| 编译 `setup.py build_ext` 报 `fatal error: THC/THC.h: No such file or directory`      | 漏执行 Step 5 开头的 `sed` 删除语句；重新运行 sed 后再 `rm -rf build dist *.egg-info` 清理                                                    |
| `THCudaCheck FAIL file=… error=804 : device-side assert triggered` 或 CUDA MISMATCH | 通常是旧版 `.so`（cp38）与 Python 3.10 混用，重新编译所有 CUDA 扩展并删除 `*.cpython-38-*.so`                                                    |
| NATS 报 `nats: no responders available for request`                                 | （本地测试）使用默认 JetStream 域启动 NATS Server，不要在客户端传 `domain=…` 参数；并确认 `/tmp/nats-store-fix`（若存在）可以清空后重启 NATS                      |
| `AttributeError: module 'cv2.dnn' has no attribute 'DictValue'`                    | 见 Step 6 的 opencv 版本修复                                                                                                     |
| 分布式接口 `from a2a.server…` 失败                                                        | 确认 `a2a-sdk==1.1.2` 且其传递依赖 `pydantic>=2`、`httpx`、`json-rpc` 都已安装；如果 `import a2a_sdk` 失败是正常的，实际模块名是 `import a2a`            |
| pip 安装 `pydantic_core` 时编译失败                                                       | 直接安装 PyPI 提供的二进制 wheel；如果 `pip>=23` 仍失败，升级 pip 再重试，或手动指定 `--only-binary :all:`                                             |

***

## 版本对照速查表（与参考冻结一致）

| 组件                                              | 版本                                     | 安装方式                                      |
| ----------------------------------------------- | -------------------------------------- | ----------------------------------------- |
| Python                                          | **3.10.x**                             | conda create                              |
| PyTorch / CUDA Toolkit                          | **1.12.0 / 11.6.2**                    | conda (pytorch + conda-forge)             |
| torchvision / torchaudio                        | 0.13.0 / 0.12.0                        | conda                                     |
| spconv-cu116 / cumm-cu116                       | 2.3.6 / 0.4.11                         | pip                                       |
| torch-scatter / torch-cluster (cu116+pt112)     | 2.1.0+pt112cu116 / 1.6.0+pt112cu116    | PyG prebuilt .whl                         |
| mmengine / mmcv / mmdet / mmseg / mmdet3d       | 0.10.7 / 2.1.0 / 3.3.0 / 1.2.2 / 1.4.0 | mim                                       |
| deformable-aggregation-ext / local-aggregate    | 0.0.0 (就地编译)                           | `pip install -e .` 于 `ops/` 和 `localagg/` |
| **nats-py**                                     | **2.15.0**                             | **pip**                                   |
| **fastapi / uvicorn / starlette**               | **0.141.1 / 0.52.3 / 1.6.0**           | **pip**                                   |
| **httpx / httpcore**                            | **0.28.1 / 1.0.9**                     | **pip**                                   |
| **pydantic / pydantic\_core**                   | **2.13.4 / 2.46.4**                    | **pip**                                   |
| **a2a-sdk**                                     | **1.1.2**                              | **pip (PyPI Google)**                     |
| **json-rpc / sse-starlette / aiologic / janus** | **1.15.0 / 3.4.8 / 0.17.1 / 2.0.0**    | **pip**                                   |
| **nest-asyncio / portalocker / culsans**        | **1.6.0 / 4.1.0 / 0.11.0**             | **pip**                                   |
| NATS Server binary                              | v2.10.x（测试用 v2.10.11）                  | 二进制下载到 `/tmp/nats-server`                 |

