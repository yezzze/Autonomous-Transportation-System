# MobileNet 车端事故片段筛选器

此目录保存当前端—边—云链路实际调用的车端推理代码。

```text
完整视频
  → 按配置帧率抽帧
  → 等比例缩放并填充至 224×224
  → MobileNetV3-Small 帧级事故二分类
  → 定位全视频事故概率最高时刻
  → 以峰值为中心保留固定比例时长
  → 按传输帧率输出采样帧
```

当前权重位于工程根目录的：

```text
weights/mobilenet/model_best.pth
```

当前通过测试的参数：

```text
扫描帧率              2 fps
推理批量              32
精度                  FP16（GPU）
片段长度              原视频 50%
上传采样帧率          1 fps
上传帧最长边          320 px
```

独立推理示例：

```bash
python mobilenet_accident_detector/scripts/03_infer_video.py \
  --checkpoint weights/mobilenet/model_best.pth \
  --video data/green14/vru_over20_dada_2000_vru_10.mp4 \
  --out outputs/mobilenet_demo \
  --sample-fps 2 \
  --batch-size 32 \
  --decoder auto \
  --threshold 0 \
  --clip-ratio 0.5 \
  --smooth-window 0 \
  --video-encoder auto
```

当前交接目录只包含推理实现，没有原始训练数据和训练脚本。需要重新训练时，请向原训练负责人索取训练工程，或基于 `TRAINING_AND_WEIGHTS.md` 重新建立训练流程。训练和推理必须使用一致的 Letterbox 预处理。
