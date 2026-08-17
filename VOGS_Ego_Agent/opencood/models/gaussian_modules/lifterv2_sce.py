"""
提示：在此实现lifter_sce, 目前的代码完全从gaussian_fuse复制
"""

import torch, torch.nn as nn, math, os
import numpy as np
from einops import rearrange
# from mmseg.registry import MODELS
# from .base_lifter import BaseLifter
from .gaussian_utils import safe_inverse_sigmoid
# from ..utils.sampler import DistributionSampler
from opencood.models.utils.sampler import DistributionSampler
from .resnet_secondfpn import ResNetSecondFPN

try:
    from pointops.functions.pointops import furthestsampling as farthest_point_sampling
except:
    print("farthest_point_sampling import error.")


class GaussianLifterV2Sce(nn.Module):
    def __init__(
        self,
        num_anchor,
        embed_dims,
        anchor_grad=True,
        feat_grad=True,
        semantics=False,
        semantic_dim=None,
        include_opa=True,
        xyz_activation="sigmoid",
        scale_activation="sigmoid",

        num_samples=64,
        pc_range=[-50, -50, -5, 50, 50, 3],
        voxel_size=0.5,
        occ_resolution=[200, 200, 16],
        empty_label=17,
        anchors_per_pixel=1,
        random_sampling=True,
        sce_thresh_high=0.7,
        sce_thresh_low=0.3,
        sce_anchors_high=5,
        sce_anchors_med=2,
        sce_anchors_low=1,
        projection_in=None,
        initializer=None,
        initializer_img_downsample=None,
        pretrained_path=None,
        deterministic=True,
        random_samples=0,
        **kwargs,
    ):
        super().__init__()
        self.embed_dims = embed_dims
        self.xyz_act = xyz_activation
        self.scale_act = scale_activation
        self.include_opa = include_opa
        self.semantics = semantics
        self.semantic_dim = semantic_dim

        self.random_samples = random_samples
        if random_samples > 0:
            self.random_anchors = self.init_random_anchors()
                    
        scale = torch.ones(num_anchor, 3, dtype=torch.float) * 0.5
        if scale_activation == "sigmoid":
            scale = safe_inverse_sigmoid(scale)

        rots = torch.zeros(num_anchor, 4, dtype=torch.float)
        rots[:, 0] = 1

        if include_opa:
            opacity = safe_inverse_sigmoid(0.5 * torch.ones((num_anchor, 1), dtype=torch.float))
        else:
            opacity = torch.ones((num_anchor, 0), dtype=torch.float)

        if semantics:
            assert semantic_dim is not None
        else:
            semantic_dim = 0
        semantic = torch.randn(num_anchor, semantic_dim, dtype=torch.float)
        anchor = torch.cat([scale, rots, opacity, semantic], dim=-1)

        self.num_anchor = num_anchor
        self.anchor = nn.Parameter(
            torch.tensor(anchor, dtype=torch.float32),
            requires_grad=anchor_grad,
        )
        self.instance_feature = nn.Parameter(
            torch.zeros([num_anchor + random_samples, self.embed_dims]),
            requires_grad=feat_grad,
        )
        projection_in = embed_dims * 4 if projection_in is None else projection_in
        self.projection = nn.Sequential(
            nn.ReLU(),
            nn.Linear(projection_in, num_samples + 1),
        )
        self.sampler = DistributionSampler()
        self.num_samples = num_samples
        self.register_buffer("depth_bins", torch.linspace(
            1.0, 72.0, self.num_samples, dtype=torch.float), persistent=False)
        self.register_buffer("pc_start", torch.tensor(
            pc_range[:3], dtype=torch.float), persistent=False)
        self.pc_range = pc_range
        self.voxel_size = voxel_size
        self.occ_resolution = occ_resolution
        self.empty_label = empty_label
        self.anchors_per_pixel = anchors_per_pixel
        self.sce_thresh_high = sce_thresh_high
        self.sce_thresh_low = sce_thresh_low
        self.sce_anchors_high = sce_anchors_high
        self.sce_anchors_med = sce_anchors_med
        self.sce_anchors_low = sce_anchors_low
        self.random_sampling = random_sampling
        
        # Modified initializer building
        if initializer is not None:
            # self.initialize_backbone = MODELS.build(initializer)
            if initializer.get('type') == 'ResNetSecondFPN':
                init_args = initializer.copy()
                init_args.pop('type')
                self.initialize_backbone = ResNetSecondFPN(**init_args)
            else:
                raise ValueError(f"Unknown initializer type: {initializer.get('type')}")
        else:
            self.initialize_backbone = None
            
        self.initializer_img_downsample = initializer_img_downsample
        
        self.pretrained_path = pretrained_path
        self.deterministic = deterministic
        if pretrained_path is not None:
            ckpt = torch.load(pretrained_path, map_location='cpu')
            ckpt = ckpt.get("state_dict", ckpt)
            if 'instance_feature' in ckpt:
                del ckpt['instance_feature']
            if 'anchor' in ckpt:
                del ckpt['anchor']
            print(self.load_state_dict(ckpt, strict=False))
            print("Gaussian Initializer Weight Loaded Successfully.")

    def init_random_anchors(self):
        num_anchor = self.random_samples
        
        xyz = torch.rand(num_anchor, 3, dtype=torch.float)
        if self.xyz_act == "sigmoid":
            xyz = safe_inverse_sigmoid(xyz)
        
        scale = torch.ones(num_anchor, 3, dtype=torch.float) * 0.5
        if self.scale_act == "sigmoid":
            scale = safe_inverse_sigmoid(scale)

        rots = torch.zeros(num_anchor, 4, dtype=torch.float)
        rots[:, 0] = 1

        if self.include_opa:
            opacity = safe_inverse_sigmoid(0.5 * torch.ones((num_anchor, 1), dtype=torch.float))
        else:
            opacity = torch.ones((num_anchor, 0), dtype=torch.float)

        if self.semantics:
            semantic_dim = self.semantic_dim
            assert semantic_dim is not None
        else:
            semantic_dim = 0
        semantic = torch.randn(num_anchor, semantic_dim, dtype=torch.float)
        anchor = torch.cat([xyz, scale, rots, opacity, semantic], dim=-1)
        anchor = nn.Parameter(anchor, True)
        return anchor

    def init_weights(self):
        if self.pretrained_path is not None:
            return
        if self.instance_feature.requires_grad:
            torch.nn.init.xavier_uniform_(self.instance_feature.data, gain=1)

    def forward(self, metas, **kwargs):
        #图像特征提取
        #如果配置了初始化主干网络，则对输入图像进行特征提取，例如使用 ResNetSecondFPN 提取2D特征图
        if "secondfpn_out" in kwargs and kwargs["secondfpn_out"] is not None:
            secondfpn_out = kwargs["secondfpn_out"]
        elif self.initialize_backbone is not None:
            # Adapting input for ResNetSecondFPN if needed
            # In V1/opencood, input is typically 'imgs'
            if "imgs" in kwargs:
                imgs = kwargs["imgs"]
            else:
                # If inputs are passed differently, need to check
                # Assuming kwargs["imgs"] is available as it is used below
                imgs = kwargs.get("imgs")

            b, n = imgs.shape[:2]
            initialize_input = imgs.flatten(0, 1)
            if self.initializer_img_downsample is not None:
                initialize_input = nn.functional.interpolate(
                    initialize_input, scale_factor=self.initializer_img_downsample, 
                    mode='bilinear', align_corners=True)
            secondfpn_out = self.initialize_backbone(initialize_input)
            secondfpn_out = secondfpn_out.unflatten(0, (b, n))
        else:
            secondfpn_out = kwargs["secondfpn_out"]
        
        b, n, _, h, w = secondfpn_out.shape
        feature = rearrange(secondfpn_out, 'b n c h w -> b n h w c')
        
        # 深度/占用分布预测：
        # 预测每条相机射线(每个特征图像素)上 num_samples 个深度 bin 的概率分布，外加 1 个 empty 状态
        logits = self.projection(feature) # b, n, h, w, d + 1

        #视锥射线投射 (生成 3D 候选点)：
        projection_mat = metas["projection_mat"].inverse() # img2lidar 逆投影矩阵
        u = (torch.arange(w, dtype=feature.dtype, device=feature.device) + 0.5) / w
        v = (torch.arange(h, dtype=feature.dtype, device=feature.device) + 0.5) / h
        uv = torch.stack([
            u[None, :].expand(h, w), v[:, None].expand(h, w)], dim=-1) # h, w, 2
        uv = uv[None, None].expand(b, n, h, w, 2) * metas['image_wh'][:, :, None, None] # b, n, h, w, 2
        uvd = uv.unsqueeze(4).expand(b, n, h, w, self.num_samples, 2)
        uvd1 = torch.cat([uvd, torch.ones_like(uvd)], dim=-1) # b, n, h, w, d, 4
        # 结合预设的 depth_bins 将像素坐标扩展到 3D 空间
        uvd1[..., :3] = uvd1[..., :3] * self.depth_bins.view(1, 1, 1, 1, -1, 1)
        # 投影到雷达/世界坐标系下，得到 3D 候选锚点
        anchor_pts = projection_mat[:, :, None, None, None] @ uvd1[..., None]
        anchor_pts = anchor_pts.squeeze(-1)[..., :3]
        
        #匹配体素网格，生成 Ground Truth (用于训练)
        if kwargs.get("benchmarking", False):
            anchor_gt = None
        else:
            # 判断锚点是否越出点云范围 (Out of Bound)
            oob_mask = (anchor_pts[..., 0] < self.pc_range[0]) | (anchor_pts[..., 0] >= self.pc_range[3]) | \
                       (anchor_pts[..., 1] < self.pc_range[1]) | (anchor_pts[..., 1] >= self.pc_range[4]) | \
                       (anchor_pts[..., 2] < self.pc_range[2]) | (anchor_pts[..., 2] >= self.pc_range[5])
            # 将 3D 坐标转换为体素索引
            anchor_idx = (anchor_pts - self.pc_start.view(1, 1, 1, 1, 1, 3)) / self.voxel_size
            anchor_idx = anchor_idx.to(torch.long)
            anchor_idx[..., 0].clamp_(0, self.occ_resolution[0] - 1)
            anchor_idx[..., 1].clamp_(0, self.occ_resolution[1] - 1)
            anchor_idx[..., 2].clamp_(0, self.occ_resolution[2] - 1)

            # 根据体素索引从 occ_label 获取真实占用状态
            occupancy = metas["occ_label"]
            valid_mask = metas["occ_cam_mask"]
            anchor_occ = torch.stack([occ[idx[..., 0], idx[..., 1], idx[..., 2]] for occ, idx in zip(occupancy, anchor_idx)])
            anchor_occ[oob_mask] = self.empty_label
            anchor_valid = torch.stack([occ[idx[..., 0], idx[..., 1], idx[..., 2]] for occ, idx in zip(valid_mask, anchor_idx)])
            anchor_valid[oob_mask] = False
            
            # 生成监督信号：该点是否被占用且有效
            anchor_gt = (anchor_occ != self.empty_label) & anchor_valid
            anchor_gt = torch.cat([anchor_gt, ~torch.any(anchor_gt, dim=-1, keepdim=True)], dim=-1)
        
        # 步骤5: 基于预测概率进行 3D 锚点采样
        pdfs = torch.softmax(logits, dim=-1)
        deterministic = getattr(self, 'deterministic', True)
        
        # [点级过滤机制(软采样)]: 基于概率分布在 depth 维度进行采样
        if "feature_sce" in kwargs and kwargs["feature_sce"] is not None:
            # [SceGaussian AI 提示] 核心改变逻辑：
            # 基于SCE模块输出的复杂度分数(feature_sce)进行动态锚点分配。
            # 将每个射线的复杂度划分为高、中、低三个层级，并对各层级设定不同的采样点数量(sce_anchors_high/med/low)。
            # 为了使用统一的sampler函数，我们统一采集 max_anchors 个点，然后利用 keep_mask 将多余的点标记为无效。
            
            # feature_sce: [B, N, 1, H, W]
            feature_sce = kwargs["feature_sce"].squeeze(2) # [B, N, H, W]
            
            # 动态阈值：排名前30%（即分位数0.7）为高，排名后30%（分位数0.3）为低
            thresh_high = torch.quantile(feature_sce, self.sce_thresh_high)
            thresh_med = torch.quantile(feature_sce, self.sce_thresh_low)
            
            mask_high = feature_sce >= thresh_high
            mask_med = (feature_sce >= thresh_med) & (feature_sce < thresh_high)
            mask_low = feature_sce < thresh_med
            
            max_anchors = self.sce_anchors_high
            index, pdf_i = self.sampler.sample(pdfs, deterministic, max_anchors)
            
            keep_mask = torch.zeros_like(index, dtype=torch.bool)
            
            mask_high_exp = mask_high.unsqueeze(-1).expand(-1, -1, -1, -1, max_anchors).clone()
            mask_high_exp[..., self.sce_anchors_high:] = False
            keep_mask[mask_high_exp] = True
            
            mask_med_exp = mask_med.unsqueeze(-1).expand(-1, -1, -1, -1, max_anchors).clone()
            mask_med_exp[..., self.sce_anchors_med:] = False
            keep_mask[mask_med_exp] = True
            
            mask_low_exp = mask_low.unsqueeze(-1).expand(-1, -1, -1, -1, max_anchors).clone()
            mask_low_exp[..., self.sce_anchors_low:] = False
            keep_mask[mask_low_exp] = True
            
            index[~keep_mask] = self.num_samples
            
            disable_mask = (pdfs.argmax(dim=-1, keepdim=True) == self.num_samples).expand(
                -1, -1, -1, -1, max_anchors)
            disable_mask = disable_mask | (~keep_mask)
            
            sampled_sce = feature_sce.unsqueeze(-1).expand(-1, -1, -1, -1, max_anchors)
        else:
            index, pdf_i = self.sampler.sample(pdfs, deterministic, self.anchors_per_pixel)
            disable_mask = (pdfs.argmax(dim=-1, keepdim=True) == self.num_samples).expand(
                -1, -1, -1, -1, self.anchors_per_pixel)
            sampled_sce = torch.zeros((b, n, h, w, self.anchors_per_pixel), device=pdfs.device)

        sampled_anchor = self.sampler.gather(index.clamp(max=(self.num_samples-1)), anchor_pts) # size: b, n, h, w, anchors_per_pixel, 3
        
        #3D 点云过滤、补齐与降采样 (最远点采样 FPS)
        anchor_xyz = []
        anchor_sce = []
        for i in range(b):
            # 获取当前 batch 有效的采样锚点
            cur_sampled_anchor = sampled_anchor[i][~disable_mask[i]]
            cur_sampled_sce = sampled_sce[i][~disable_mask[i]]
            
            cur_oob_mask = (cur_sampled_anchor[..., 0] < self.pc_range[0]) | (cur_sampled_anchor[..., 0] >= self.pc_range[3]) | \
                   (cur_sampled_anchor[..., 1] < self.pc_range[1]) | (cur_sampled_anchor[..., 1] >= self.pc_range[4]) | \
                   (cur_sampled_anchor[..., 2] < self.pc_range[2]) | (cur_sampled_anchor[..., 2] >= self.pc_range[5])
            scan = cur_sampled_anchor[~cur_oob_mask]
            scan_sce = cur_sampled_sce[~cur_oob_mask]
            
            if scan.shape[0] == 0:
                scan = torch.zeros(self.num_anchor, 3, device=anchor_pts.device)
                scan_sce = torch.zeros(self.num_anchor, device=anchor_pts.device)
                anchor_xyz.append(scan)
                anchor_sce.append(scan_sce)
                continue
            
            if self.random_sampling: #此分支当前模型配置下永远不进入，只使用后面的fps最远点采样
                if scan.shape[0] < self.num_anchor:
                    # 如果点数不够，则复制已有经过加噪扰动生成新点进行补齐
                    multi = int(math.ceil(self.num_anchor * 1.0 / scan.shape[0])) - 1
                    scan_ = scan.repeat(multi, 1)
                    scan_sce_ = scan_sce.repeat(multi)
                    
                    scan_ = scan_ + torch.randn_like(scan_) * 0.1
                    
                    choice_idx = np.random.choice(scan_.shape[0], self.num_anchor - scan.shape[0], False)
                    scan_ = scan_[choice_idx]
                    scan_sce_ = scan_sce_[choice_idx]
                    
                    scan_[:, 0].clamp_(self.pc_range[0], self.pc_range[3])
                    scan_[:, 1].clamp_(self.pc_range[1], self.pc_range[4])
                    scan_[:, 2].clamp_(self.pc_range[2], self.pc_range[5])
                    scan = torch.cat([scan, scan_], 0)
                    scan_sce = torch.cat([scan_sce, scan_sce_], 0)
                else:
                    # 点数过多，随机采样
                    choice_idx = np.random.choice(scan.shape[0], self.num_anchor, False)
                    scan = scan[choice_idx]
                    scan_sce = scan_sce[choice_idx]
            else:
                if scan.shape[0] < self.num_anchor:
                    # 不足时同样复制和加噪补齐
                    multi = int(math.ceil(self.num_anchor * 1.0 / scan.shape[0])) - 1
                    scan_ = scan.repeat(multi, 1)
                    scan_sce_ = scan_sce.repeat(multi)
                    
                    scan_ = scan_ + torch.randn_like(scan_) * 0.1
                    scan_[:, 0].clamp_(self.pc_range[0], self.pc_range[3])
                    scan_[:, 1].clamp_(self.pc_range[1], self.pc_range[4])
                    scan_[:, 2].clamp_(self.pc_range[2], self.pc_range[5])
                    scan = torch.cat([scan, scan_], 0)
                    scan_sce = torch.cat([scan_sce, scan_sce_], 0)
                
                if kwargs.get("benchmarking", False):
                    perm = torch.randperm(scan.shape[0], device=scan.device)
                    scan = scan[perm]
                    scan_sce = scan_sce[perm]
                    num_subsets = 3
                    sublens = torch.linspace(0, scan.shape[0], num_subsets + 1, dtype=torch.int, device=scan.device)[1:]
                    new_sublens = torch.linspace(0, self.num_anchor, num_subsets + 1, dtype=torch.int, device=scan.device)[1:]
                    scanidx = farthest_point_sampling(scan, sublens, new_sublens)
                else:
                    # 执行fps最远点采样（核心采样）
                    scanidx = farthest_point_sampling(
                        scan, 
                        torch.tensor([scan.shape[0]], device=scan.device, dtype=torch.int),
                        torch.tensor([self.num_anchor], device=scan.device, dtype=torch.int))
                scan = scan[scanidx.long(), :]
                scan_sce = scan_sce[scanidx.long()]
            
            anchor_xyz.append(scan)
            anchor_sce.append(scan_sce)

            if os.environ.get("DEBUG", 'false') == 'true':
                prefix = 'kitti-'
                #### save pred scan
                np.save(f'{prefix}pred_scan.npy', scan.detach().cpu().numpy())
                #### save gt scan
                np.save('gt_scan_occ.npy', anchor_occ.detach().cpu().numpy())
                np.save('gt_scan_pts.npy', anchor_pts.detach().cpu().numpy())
                #### save gt occupancy
                np.save('gt_occ.npy', metas['occ_label'].detach().cpu().numpy())
                np.save('gt_pts.npy', metas['occ_xyz'].detach().cpu().numpy())
                breakpoint()
        
        #高斯球参数组装
        anchor_xyz = torch.stack(anchor_xyz)
        anchor_sce = torch.stack(anchor_sce).unsqueeze(-1) # [B, num_anchor, 1]
        
        # 归一化中心坐标到 [0, 1] 范围
        anchor_xyz[..., 0] = (anchor_xyz[..., 0] - self.pc_range[0]) / (self.pc_range[3] - self.pc_range[0])
        anchor_xyz[..., 1] = (anchor_xyz[..., 1] - self.pc_range[1]) / (self.pc_range[4] - self.pc_range[1])
        anchor_xyz[..., 2] = (anchor_xyz[..., 2] - self.pc_range[2]) / (self.pc_range[5] - self.pc_range[2])

        if self.xyz_act == "sigmoid":
            xyz = safe_inverse_sigmoid(anchor_xyz)
            
        # 将生成的 3D 中心点与可学习的高斯参数(尺度、旋转、不透明度、语义特征等)拼接
        anchor = torch.cat([
            xyz, torch.tile(self.anchor[None], (b, 1, 1))], dim=-1)
        
        # 补充随机采样以防核心采样不稳定
        if self.random_samples > 0:
            random_anchors = torch.tile(self.random_anchors[None], (b, 1, 1))
            anchor = torch.cat([anchor, random_anchors], dim=1)
            
            # [SceGaussian AI 提示] 随机采样的高斯的复杂度改为当前fps最远点采样得到的高斯的复杂度范围内的随机值
            min_sce = anchor_sce.min(dim=1, keepdim=True)[0]
            max_sce = anchor_sce.max(dim=1, keepdim=True)[0]
            random_sce = min_sce + (max_sce - min_sce) * torch.rand((b, self.random_samples, 1), device=anchor_sce.device)
            
            anchor_sce = torch.cat([anchor_sce, random_sce], dim=1)

        instance_feature = torch.tile(
            self.instance_feature[None], (b, 1, 1)
        )

        #向模型返回：
        return {
            'rep_features': instance_feature, #初始化的高斯球高纬query编码，size:[1, 12800, 128]
            'representation': anchor,         #初始化的高斯球，size:[1, 12800, 24]
            'GsSCE': anchor_sce,              #初始化的高斯球复杂度，size:[1, 12800, 1]
            'anchor_init': anchor[0].clone(),
            'pixel_logits': logits,          #每个特征图的每个像素的128个占用预测（另外一个是整个射线是否被占用），size:[1, 4， 108， 200， 129]
            'pixel_gt': anchor_gt,           #pixel_logits对应的ground truth，size:[1, 4， 108， 200， 129]
        }
