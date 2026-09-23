import os
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from transformers import VideoMAEImageProcessor, VideoMAEModel, AutoConfig
from decord import VideoReader
from sklearn.metrics import accuracy_score, f1_score
import matplotlib.pyplot as plt
from transformers import VideoMAEModel, VideoMAEConfig


# 配置参数
class Config:
    # 数据参数
    dataset_path = "./TAD-benchmark" # 结构：train/normal, train/anomaly, test/...
    sample_rate = 2                   # 帧采样率
    num_frames = 16                   # 每个视频片段帧数
    input_size = 224
    
    # 模型参数
    model_name = "./videomae-base-finetuned-kinetics"
    num_classes = 2                   # 正常/异常二分类
    freeze_backbone = True             # 是否冻结主干网络
    hidden_dim = 768
    
    # 训练参数
    batch_size = 8
    epochs = 15
    lr = 1e-4
    weight_decay = 1e-5
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # 路径参数
    checkpoint_dir = "./checkpoints"
    
    def __init__(self):
        # 自动检测模型路径
        possible_paths = [
            "./videomae-base-finetuned-kinetics",
            os.path.join(os.path.dirname(__file__), "videomae-base-finetuned-kinetics"),
            "/home/wl/code/videomae-base-finetuned-kinetics",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                self.model_name = path
                break
    
config = Config()

# 确保检查点目录存在
os.makedirs(config.checkpoint_dir, exist_ok=True)


# 延迟初始化处理器（避免导入时出错）
_processor = None

def get_processor():
    """延迟加载处理器"""
    global _processor
    if _processor is None:
        try:
            _processor = VideoMAEImageProcessor.from_pretrained(
                config.model_name, local_files_only=True
            )
        except:
            # 如果local_files_only失败，尝试不使用它
            _processor = VideoMAEImageProcessor.from_pretrained(config.model_name)
    return _processor

# 为了向后兼容，创建一个类来模拟processor的行为
class ProcessorProxy:
    def __call__(self, *args, **kwargs):
        return get_processor()(*args, **kwargs)
    
    def __getattr__(self, name):
        return getattr(get_processor(), name)

processor = ProcessorProxy()

# 修正后的数据集类
class TrafficAnomalyDataset(Dataset):
    def __init__(self, root_dir, mode='train'):
        self.video_paths = []
        self.labels = []
        self.class_map = {"normal": 0, "anomaly": 1, "accident": 1}
        
        data_dir = os.path.join(root_dir, mode)
        if not os.path.exists(data_dir):
            raise FileNotFoundError(f"数据目录不存在: {data_dir}")
        
        for label_name in os.listdir(data_dir):
            label_dir = os.path.join(data_dir, label_name)
            if not os.path.isdir(label_dir):
                continue
            
            # 确定标签：normal=0, 其他（accident相关）=1
            if label_name == "normal":
                label = 0
            elif "accident" in label_name.lower():
                label = 1
            else:
                # 对于其他文件夹，默认视为异常
                label = 1
            
            # 遍历子目录中的视频文件
            for video_file in os.listdir(label_dir):
                if video_file.endswith(('.mp4', '.avi')):
                    self.video_paths.append(os.path.join(label_dir, video_file))
                    self.labels.append(label)

    def __len__(self):
        return len(self.video_paths)

    def __getitem__(self, idx):
        # 读取视频
        vr = VideoReader(self.video_paths[idx])
        total_frames = len(vr)
        
        # 均匀采样帧
        indices = np.linspace(0, total_frames-1, num=config.num_frames, dtype=np.int32)
        frames = vr.get_batch(indices).asnumpy()  # (T, H, W, C)
        #print("frames.shape:",frames.shape)frames.shape: (16, 1080, 1920, 3)
        # 使用处理器的正确方法
        inputs = processor(
            list(frames),  # 转换为帧列表
            return_tensors="pt"
        )  # 输出形状: (1, T, C, H, W)
        pixel_values = inputs['pixel_values'].squeeze(0)  # 原始形状 (T, C, H, W)
        #pixel_values.shape: torch.Size([16, 3, 224, 224])
        #print("pixel_values.shape:",pixel_values.shape)
        # pixel_values = pixel_values.permute(1, 0, 2, 3)  # 调整为 (C, T, H, W)
        return pixel_values, self.labels[idx]
       

# 自定义模型
class VideoAnomalyDetector(nn.Module):
    def __init__(self, config):
        super().__init__()
        # self.backbone = VideoMAEModel.from_pretrained(
        #     config.model_name,
        #     trust_remote_code=True,
        # )
        # 尝试加载配置
        try:
            model_config = VideoMAEConfig.from_pretrained(
                config.model_name, local_files_only=True
            )
        except:
            try:
                # 如果本地文件损坏，尝试从 HuggingFace 下载
                print(f"⚠️  本地配置文件不可用，尝试从 HuggingFace 下载...")
                model_config = VideoMAEConfig.from_pretrained("MCG-NJU/videomae-base-finetuned-kinetics")
            except:
                # 如果都失败，使用默认配置
                print(f"⚠️  无法加载配置，使用默认配置...")
                model_config = VideoMAEConfig()
        
        model_config.num_channels = 3  # 强制写明
       
        # 尝试加载模型
        try:
            self.backbone = VideoMAEModel.from_pretrained(
                config.model_name,
                config=model_config,
                trust_remote_code=True,
                local_files_only=True,
            )
        except Exception as e:
            try:
                # 如果本地文件损坏，尝试从 HuggingFace 下载
                print(f"⚠️  本地模型文件不可用，尝试从 HuggingFace 下载...")
                self.backbone = VideoMAEModel.from_pretrained(
                    "MCG-NJU/videomae-base-finetuned-kinetics",
                    config=model_config,
                    trust_remote_code=True,
                )
            except Exception as e2:
                # 如果都失败，只使用配置创建模型（不加载预训练权重）
                # 因为我们后面会加载checkpoint，所以这应该没问题
                print(f"⚠️  无法加载预训练权重，使用配置创建模型（将使用checkpoint权重）...")
                print(f"   错误信息: {str(e2)}")
                self.backbone = VideoMAEModel(config=model_config)

        self.backbone.config.num_channels = 3 
        self.backbone.config.num_channels = 3
        
        # 冻结主干参数
        if config.freeze_backbone:
            for param in self.backbone.parameters():
                param.requires_grad = False
                
        # 分类头
        self.classifier = nn.Sequential(
            nn.Linear(config.hidden_dim, 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, config.num_classes)
        )
        
    # def forward(self, x):
    #     # x形状: (B, C, T, H, W)
    #     #print(x.shape) torch.Size([8, 3, 16, 224, 224])
    #     print(">>> INPUT to model shape:", x.shape)
    #     outputs = self.backbone(x)
    #     #print(outputs.shape) torch.Size([8, 768])
    #      # 处理所有可能的输出类型
  
    #     return self.classifier(outputs)

    def forward(self, pixel_values):
        # 输入应为 (B, T, C, H, W)
        cfg = getattr(self.backbone, "config", None)
        
        # 如果意外传成了 (B, C, T, H, W)，则自动修正
        if cfg is not None and pixel_values.ndim == 5:
            if pixel_values.shape[1] == cfg.num_channels and pixel_values.shape[2] != cfg.num_channels:
                pixel_values = pixel_values.permute(0, 2, 1, 3, 4).contiguous()

            # 如果仍不匹配，提前报错
            assert pixel_values.shape[2] == cfg.num_channels, \
                f"Expect channels={cfg.num_channels} at dim=2, got {tuple(pixel_values.shape)}"

        # 关键！传入 backbone 时用关键字参数
        out = self.backbone(pixel_values=pixel_values)

        # 兼容 HuggingFace 的输出格式
        if isinstance(out, (tuple, list)):
            out = out[0]
        elif hasattr(out, "last_hidden_state"):
            out = out.last_hidden_state.mean(dim=1)

        return self.classifier(out)


# 训练函数
def train_epoch(model, dataloader, criterion, optimizer, device):
    model.train()
    total_loss = 0.0
    preds, targets = [], []
    
    for inputs, labels in dataloader:
        inputs = inputs.to(device).float()  # (B, C, T, H, W)
        #inputs = inputs.permute(0, 2, 1, 3, 4).to(device).float()  # → (B, C, T, H, W)
        labels = labels.to(device)
        
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        
        total_loss += loss.item() * inputs.size(0)
        _, batch_preds = torch.max(outputs, 1)
        preds.extend(batch_preds.cpu().numpy())
        targets.extend(labels.cpu().numpy())
    
    avg_loss = total_loss / len(dataloader.dataset)
    accuracy = accuracy_score(targets, preds)
    return avg_loss, accuracy

    

# 验证函数
def validate(model, dataloader, criterion, device):
    model.eval()
    total_loss = 0.0
    preds, targets = [], []
    
    with torch.no_grad():
        for inputs, labels in dataloader:
            inputs = inputs.to(device).float()
            #inputs = inputs.permute(0, 2, 1, 3, 4).to(device).float()  # → (B, C, T, H, W)
            labels = labels.to(device)
            
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            
            total_loss += loss.item() * inputs.size(0)
            _, batch_preds = torch.max(outputs, 1)
            preds.extend(batch_preds.cpu().numpy())
            targets.extend(labels.cpu().numpy())
    
    avg_loss = total_loss / len(dataloader.dataset)
    accuracy = accuracy_score(targets, preds)
    f1 = f1_score(targets, preds)
    return avg_loss, accuracy, f1

# 训练流程
def main():
    # 初始化数据
    train_dataset = TrafficAnomalyDataset(config.dataset_path, 'train')
    val_dataset = TrafficAnomalyDataset(config.dataset_path, 'test')
    
    train_loader = DataLoader(
        train_dataset,
        batch_size=config.batch_size,
        shuffle=True,
        num_workers=4,
        pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=config.batch_size,
        num_workers=2,
        pin_memory=True
    )
    
    # 初始化模型
    model = VideoAnomalyDetector(config).to(config.device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config.lr,
        weight_decay=config.weight_decay
    )
    
    best_f1 = 0.0
    train_losses, val_metrics = [], []
    
    # 训练循环
    for epoch in range(config.epochs):
        train_loss, train_acc = train_epoch(
            model, train_loader, criterion, optimizer, config.device
        )
        val_loss, val_acc, val_f1 = validate(
            model, val_loader, criterion, config.device
        )
        
        # 记录指标
        train_losses.append(train_loss)
        val_metrics.append((val_loss, val_acc, val_f1))
        
        # 保存最佳模型
        if val_f1 > best_f1:
            best_f1 = val_f1
            torch.save(
                model.state_dict(),
                os.path.join(config.checkpoint_dir, "best_model.pth")
            )
        
        print(f"Epoch {epoch+1}/{config.epochs}")
        print(f"Train Loss: {train_loss:.4f} | Acc: {train_acc:.4f}")
        print(f"Val Loss: {val_loss:.4f} | Acc: {val_acc:.4f} | F1: {val_f1:.4f}")
        print("-" * 60)
    
    # 可视化训练过程
    plt.figure(figsize=(12, 5))
    plt.subplot(1, 2, 1)
    plt.plot(train_losses, label='Train Loss')
    plt.plot([x[0] for x in val_metrics], label='Val Loss')
    plt.legend()
    
    plt.subplot(1, 2, 2)
    plt.plot([x[1] for x in val_metrics], label='Val Acc')
    plt.plot([x[2] for x in val_metrics], label='Val F1')
    plt.legend()
    plt.savefig("./training_metrics.png")

if __name__ == "__main__":
    # 训练模型
    main()
