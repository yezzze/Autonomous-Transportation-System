"""
语义压缩模块 - 基于 DynamicViT 的 Token 稀疏化
用于在 Vision Encoder 和云端 LLM 之间压缩视觉特征，减少传输带宽
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple, Optional


class SemanticCompressionModule(nn.Module):
    """
    语义压缩模块
    
    基于 DynamicViT (NeurIPS 2021) 的预测模块，用于评估每个视觉 token 的重要性
    并选择性地保留最重要的 token 进行传输。
    
    输入: [batch, num_patches, embed_dim] (Qwen2.5-VL: [1, 3328, 2048])
    输出: 
        - 训练时: (compressed_features, mask, scores, keep_indices)
        - 推理时: (compressed_features, keep_indices)
    """
    
    def __init__(self, embed_dim: int = 2048, target_keep_ratio: float = 0.3):
        """
        Args:
            embed_dim: 视觉特征的维度 (Qwen2.5-VL 为 2048)
            target_keep_ratio: 目标保留比例 (默认保留 30% 的 token)
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.target_keep_ratio = target_keep_ratio
        
        # ✅ 第三步：升级SCM模块架构 - 添加Depthwise Convolution增加空间感
        # 在MLP前面加DW-Conv层，让模型知道"周围的patch都在报错，那我这块肯定也是车"
        # 增加局部相关性是打败随机选择的王牌
        # 注意：LayerNorm在forward中应用（在transpose之前），Conv1d在transpose之后
        self.spatial_conv = nn.Sequential(
            # 1D Depthwise Convolution: 在patch序列上滑动，kernel_size=3捕获相邻patch的关系
            nn.Conv1d(
                in_channels=embed_dim,
                out_channels=embed_dim,
                kernel_size=3,
                padding=1,
                groups=embed_dim,  # Depthwise: 每个通道独立卷积
                bias=False
            ),
            nn.GELU(),
        )
        
        # MLP预测器（在空间卷积之后）
        self.predictor = nn.Sequential(
            nn.Linear(embed_dim, embed_dim // 2),
            nn.GELU(),
            nn.Linear(embed_dim // 2, embed_dim // 4),
            nn.GELU(),
            nn.Linear(embed_dim // 4, 2)  # 输出 [丢弃logits, 保留logits]
        )
        
        # 初始化权重：使用标准初始化，确保梯度能够有效传播
        # 之前 gain=0.001 太小，导致初始输出几乎为0，梯度信号太弱
        for m in self.spatial_conv.modules():
            if isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        for m in self.predictor.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight, gain=1.0)  # 标准初始化，允许模型从合理起点开始学习
                nn.init.constant_(m.bias, 0)
        
    def forward(self, vision_features: torch.Tensor, 
                training: bool = True,
                temperature: float = 1.0,
                hard: bool = True) -> Tuple[torch.Tensor, ...]:
        """
        前向传播
        
        Args:
            vision_features: 视觉特征 [batch, num_patches, embed_dim]
            training: 是否为训练模式
            temperature: Gumbel-Softmax 的温度参数（训练时使用）
            hard: 是否使用硬采样（hard=True）或软采样（hard=False）
                 - hard=True: 输出离散的 one-hot 向量，梯度通过 Gumbel-Softmax 的直通估计器传播
                 - hard=False: 输出连续的概率分布，提供平滑的梯度，对从零训练非常关键
            
        Returns:
            训练模式:
                - compressed_features: 压缩后的特征 [batch, num_keep, embed_dim]
                - mask: 保留 mask [batch, num_patches] (用于损失计算)
                - scores: 预测分数 [batch, num_patches, 2]
                - keep_indices: 保留的 token 索引 [batch, num_keep]
            推理模式:
                - compressed_features: 压缩后的特征 [batch, num_keep, embed_dim]
                - keep_indices: 保留的 token 索引 [batch, num_keep]
        """
        batch_size, num_patches, embed_dim = vision_features.shape
        
        # 关键修复：在计算前检查 vision_features 是否包含 NaN/Inf
        if torch.isnan(vision_features).any() or torch.isinf(vision_features).any():
            print(f"⚠️  警告：vision_features 包含 NaN/Inf，尝试修复")
            # 用零替换 NaN/Inf
            vision_features = torch.where(torch.isnan(vision_features) | torch.isinf(vision_features),
                                         torch.zeros_like(vision_features), vision_features)
            # 如果替换后仍然有问题，使用均匀分布作为后备
            if torch.isnan(vision_features).any() or torch.isinf(vision_features).any():
                print(f"⚠️  警告：修复失败，使用零特征")
                vision_features = torch.zeros_like(vision_features)
        
        # ✅ 第三步：应用空间卷积（Depthwise Conv）
        # 先对每个patch的特征进行LayerNorm
        normalized_features = F.layer_norm(vision_features, (embed_dim,))  # [batch, num_patches, embed_dim]
        
        # 将特征从 [batch, num_patches, embed_dim] 转换为 [batch, embed_dim, num_patches] 用于Conv1d
        features_for_conv = normalized_features.transpose(1, 2)  # [batch, embed_dim, num_patches]
        
        # 应用Depthwise Convolution
        spatial_features = self.spatial_conv(features_for_conv)  # [batch, embed_dim, num_patches]
        
        # 转换回 [batch, num_patches, embed_dim]
        spatial_features = spatial_features.transpose(1, 2)  # [batch, num_patches, embed_dim]
        
        # 检查spatial_features是否包含NaN/Inf
        if torch.isnan(spatial_features).any() or torch.isinf(spatial_features).any():
            print(f"⚠️  警告：spatial_features 包含 NaN/Inf，使用原始特征")
            spatial_features = vision_features
        
        # 1. 计算 Logits（使用经过空间卷积的特征）
        scores = self.predictor(spatial_features)
        
        # 修正 2: 强力数值裁剪，防止 NaN
        scores = torch.clamp(scores, min=-10.0, max=10.0)
        
        # 修正 3: 多重 NaN 检查
        if torch.isnan(scores).any() or torch.isinf(scores).any():
            scores = torch.where(torch.isnan(scores) | torch.isinf(scores), 
                                torch.zeros_like(scores), scores)
        
        if training:
            # 2. Gumbel-Softmax 采样 (Soft Mask)
            # 使用 raw logits (scores)，Gumbel-Softmax 会自己处理
            # hard=False 时使用 Soft Masking，提供平滑梯度，对从零训练非常关键
            gumbel_mask = F.gumbel_softmax(scores, hard=hard, tau=temperature, dim=-1)
            
            # 提取保留概率 (Mask)
            # [batch, num_patches]
            mask = gumbel_mask[:, :, 1]
            
            # 修正 3: NaN 安全检查
            if torch.isnan(mask).any():
                mask = torch.where(torch.isnan(mask), torch.zeros_like(mask), mask)
            
            # 3. 计算保留数量
            num_keep = max(1, int(num_patches * self.target_keep_ratio))
            
            # 4. 获取索引 (仅用于推理或硬切分任务，不用于梯度回传)
            # 即使是 Soft Mask，我们也计算 TopK 以便了解哪些被选中
            # 使用 mask 值作为排序依据
            _, keep_indices = torch.topk(mask, k=num_keep, dim=1)
            
            # 5. 生成压缩特征
            # 注意：在训练的 Task Loss 中，我们建议使用 Soft Masked Features (Full Length)
            # 而不是这里生成的 Hard Compressed Features (Short Length)
            # 这里返回的 compressed_features 主要用于兼容接口，或者如果 Task Loss 必须要短序列
            compressed_features = self._batch_index_select(vision_features, keep_indices)
            
            # 检查 compressed_features 是否有 NaN
            if torch.isnan(compressed_features).any() or torch.isinf(compressed_features).any():
                print(f"⚠️  警告：compressed_features 包含 NaN/Inf，使用原始特征的前 {num_keep} 个作为后备")
                compressed_features = vision_features[:, :num_keep, :]
            
            # 检查 mask 是否有 NaN/Inf
            if torch.isnan(mask).any() or torch.isinf(mask).any():
                print(f"⚠️  警告：mask 包含 NaN/Inf，使用目标比例")
                # 保持梯度连接：即使有NaN，也要连接到mask
                mask = torch.ones_like(mask) * self.target_keep_ratio
                # 确保mask有梯度（通过连接到scores）
                mask = mask + 0.0 * scores.mean()
            
            # 最后检查 compressed_features
            if torch.isnan(compressed_features).any() or torch.isinf(compressed_features).any():
                print(f"⚠️  警告：compressed_features 仍然包含 NaN/Inf，使用零特征")
                compressed_features = torch.zeros_like(compressed_features)
            
            return compressed_features, mask, scores, keep_indices
            
        else:
            # 推理时：直接选择 top-k token（硬选择，不可微分）
            num_keep = max(1, int(num_patches * self.target_keep_ratio))
            
            # 从 log probabilities 中提取保留概率（第二个维度）
            keep_scores = scores[:, :, 1]  # [batch, num_patches]
            
            # 选择 top-k
            _, keep_indices = torch.topk(keep_scores, k=num_keep, dim=1)
            
            # 提取保留的特征
            compressed_features = self._batch_index_select(vision_features, keep_indices)
            
            return compressed_features, keep_indices
    
    def _batch_index_select(self, x: torch.Tensor, idx: torch.Tensor) -> torch.Tensor:
        """
        批量索引选择（从 DynamicViT utils.py 移植）
        
        Args:
            x: [batch, num_patches, embed_dim]
            idx: [batch, num_keep] (每个样本的保留索引)
            
        Returns:
            selected: [batch, num_keep, embed_dim]
        """
        B, N, C = x.size()
        N_new = idx.size(1)
        
        # 为每个 batch 添加偏移量
        offset = torch.arange(B, dtype=torch.long, device=x.device).view(B, 1) * N
        idx = idx + offset
        
        # 展平并选择
        out = x.reshape(B * N, C)[idx.reshape(-1)].reshape(B, N_new, C)
        return out
    
    def compress_for_transmission(self, vision_features: torch.Tensor) -> dict:
        """
        压缩特征用于传输（推理模式）
        
        Args:
            vision_features: [batch, num_patches, embed_dim]
            
        Returns:
            dict: {
                'compressed_features': [batch, num_keep, embed_dim],
                'keep_indices': [batch, num_keep],
                'original_shape': (batch, num_patches, embed_dim),
                'compression_ratio': float
            }
        """
        self.eval()
        with torch.no_grad():
            compressed_features, keep_indices = self.forward(vision_features, training=False)
            
            batch_size, num_patches, embed_dim = vision_features.shape
            num_keep = compressed_features.shape[1]
            compression_ratio = num_keep / num_patches
            
            return {
                'compressed_features': compressed_features,
                'keep_indices': keep_indices,
                'original_shape': (batch_size, num_patches, embed_dim),
                'compression_ratio': compression_ratio
            }
    
    def compress_per_image(self, vision_features: torch.Tensor, 
                          image_grid_thw: torch.Tensor) -> dict:
        """
        按每张图片单独压缩（推理模式）
        
        Args:
            vision_features: [batch, total_patches, embed_dim] 例如 [1, 3328, 2048] (8张图)
            image_grid_thw: [batch, num_images, 3] 例如 [[1,32,52], [1,32,52], ...] (8张图)
            
        Returns:
            dict: {
                'compressed_features_list': List[[1, num_keep_i, embed_dim]], 每张图片的压缩特征
                'keep_indices_list': List[[1, num_keep_i]], 每张图片的保留索引
                'original_shapes': List[(1, num_patches_i, embed_dim)], 每张图片的原始形状
                'compression_ratio': float, 平均压缩率
                'per_image_info': List[dict], 每张图片的详细信息
            }
        """
        self.eval()
        import numpy as np
        
        with torch.no_grad():
            # 处理 image_grid_thw
            if isinstance(image_grid_thw, torch.Tensor):
                image_grid_thw_np = image_grid_thw.cpu().numpy()
            else:
                image_grid_thw_np = np.array(image_grid_thw)
            
            # 确保是2D数组
            if len(image_grid_thw_np.shape) == 1:
                image_grid_thw_np = image_grid_thw_np.reshape(1, -1)
            
            # 移除batch维度，得到 [num_images, 3]
            if len(image_grid_thw_np.shape) == 3:
                image_grid_thw_np = image_grid_thw_np[0]  # [num_images, 3]
            
            # 移除batch维度，得到 [total_patches, embed_dim]
            if len(vision_features.shape) == 3:
                vision_features_flat = vision_features.squeeze(0)  # [total_patches, embed_dim]
            else:
                vision_features_flat = vision_features
            
            # 根据 image_grid_thw 分割每张图片的特征
            compressed_features_list = []
            keep_indices_list = []
            original_shapes = []
            per_image_info = []
            
            start_idx = 0
            total_original_patches = 0
            total_compressed_patches = 0
            
            for i in range(len(image_grid_thw_np)):
                grid_thw = image_grid_thw_np[i]
                t, h, w = int(grid_thw[0]), int(grid_thw[1]), int(grid_thw[2])
                
                # 考虑 Qwen2.5-VL 的 2x2 pooling
                processed_h = max(h // 2, 1)
                processed_w = max(w // 2, 1)
                num_patches = t * processed_h * processed_w
                
                end_idx = start_idx + num_patches
                
                # 提取这张图片的特征 [num_patches, embed_dim]
                image_features = vision_features_flat[start_idx:end_idx].unsqueeze(0)  # [1, num_patches, embed_dim]
                
                # 对这张图片单独压缩
                compressed_result = self.compress_for_transmission(image_features)
                
                compressed_features_list.append(compressed_result['compressed_features'])
                keep_indices_list.append(compressed_result['keep_indices'])
                original_shapes.append(compressed_result['original_shape'])
                
                per_image_info.append({
                    'image_idx': i,
                    'original_patches': num_patches,
                    'compressed_patches': compressed_result['compressed_features'].shape[1],
                    'compression_ratio': compressed_result['compression_ratio']
                })
                
                total_original_patches += num_patches
                total_compressed_patches += compressed_result['compressed_features'].shape[1]
                
                start_idx = end_idx
            
            # 计算平均压缩率
            avg_compression_ratio = total_compressed_patches / total_original_patches if total_original_patches > 0 else 1.0
            
            return {
                'compressed_features_list': compressed_features_list,
                'keep_indices_list': keep_indices_list,
                'original_shapes': original_shapes,
                'compression_ratio': avg_compression_ratio,
                'per_image_info': per_image_info,
                'num_images': len(compressed_features_list)
            }


def decompress_features(compressed_features: torch.Tensor,
                       keep_indices: torch.Tensor,
                       original_shape: Tuple[int, int, int],
                       device: str = 'cuda') -> torch.Tensor:
    """
    在云端恢复压缩后的特征（单张图片）
    
    Args:
        compressed_features: [batch, num_keep, embed_dim] 压缩后的特征
        keep_indices: [batch, num_keep] 保留的 token 索引
        original_shape: (batch, num_patches, embed_dim) 原始形状
        device: 设备
        
    Returns:
        restored_features: [batch, num_patches, embed_dim] 恢复后的特征
        （未保留的位置用零填充）
    """
    batch_size, num_patches, embed_dim = original_shape
    num_keep = compressed_features.shape[1]
    
    # 创建全零的特征矩阵
    restored_features = torch.zeros(
        batch_size, num_patches, embed_dim,
        device=device, dtype=compressed_features.dtype
    )
    
    # 将压缩后的特征填回对应位置
    for i in range(batch_size):
        restored_features[i, keep_indices[i]] = compressed_features[i]
    
    return restored_features


def decompress_per_image(compressed_features_list: list,
                        keep_indices_list: list,
                        original_shapes: list,
                        image_grid_thw: torch.Tensor,
                        device: str = 'cuda') -> torch.Tensor:
    """
    在云端按每张图片恢复压缩后的特征，然后拼接
    
    Args:
        compressed_features_list: List[[1, num_keep_i, embed_dim]] 每张图片的压缩特征
        keep_indices_list: List[[1, num_keep_i]] 每张图片的保留索引
        original_shapes: List[(1, num_patches_i, embed_dim)] 每张图片的原始形状
        image_grid_thw: [batch, num_images, 3] 图像网格信息
        device: 设备
        
    Returns:
        restored_features: [batch, total_patches, embed_dim] 恢复后的完整特征
    """
    import numpy as np
    
    # 恢复每张图片的特征
    restored_images = []
    for i in range(len(compressed_features_list)):
        restored_image = decompress_features(
            compressed_features_list[i],
            keep_indices_list[i],
            original_shapes[i],
            device=device
        )
        restored_images.append(restored_image.squeeze(0))  # [num_patches_i, embed_dim]
    
    # 拼接所有图片的特征
    restored_features = torch.cat(restored_images, dim=0)  # [total_patches, embed_dim]
    
    # 添加batch维度
    restored_features = restored_features.unsqueeze(0)  # [1, total_patches, embed_dim]
    
    return restored_features

