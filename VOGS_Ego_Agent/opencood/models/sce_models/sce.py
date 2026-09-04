import torch
import torch.nn as nn
import torch.nn.functional as F

class SCE(nn.Module):
    def __init__(self, in_channels=512, alpha=0.5, beta=0.5, pc_range=[-50, -50, -5, 50, 50, 3], voxel_size=0.5, occ_resolution=[200, 200, 16], empty_label=13, num_samples=128, initializer=None, initializer_img_downsample=None, **kwargs):
        super(SCE, self).__init__()
        self.alpha = alpha
        self.beta = beta
        
        if initializer is not None:
            if initializer.get('type') == 'ResNetSecondFPN':
                from opencood.models.gaussian_modules.resnet_secondfpn import ResNetSecondFPN
                init_args = initializer.copy()
                init_args.pop('type')
                self.load_from = init_args.pop('load_from', None)
                self.initialize_backbone = ResNetSecondFPN(**init_args)
            else:
                raise ValueError(f"Unknown initializer type: {initializer.get('type')}")
        else:
            self.initialize_backbone = None
            self.load_from = None
            
        self.initializer_img_downsample = initializer_img_downsample
        
        # Shared Encoder
        self.shared_encoder = nn.Sequential(
            nn.Conv2d(in_channels, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True)
        )
        
        # Branch 1 (Geometric Head)
        self.geom_head = nn.Sequential(
            nn.Conv2d(64, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 1, kernel_size=1)
        )
        
        # Branch 2 (Semantic Head)
        self.sem_head = nn.Sequential(
            nn.Conv2d(64, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 1, kernel_size=1)
        )

        self.num_samples = num_samples
        self.register_buffer("depth_bins", torch.linspace(1.0, 72.0, self.num_samples, dtype=torch.float), persistent=False)
        self.register_buffer("pc_start", torch.tensor(pc_range[:3], dtype=torch.float), persistent=False)
        self.pc_range = pc_range
        self.voxel_size = voxel_size
        self.occ_resolution = occ_resolution
        self.empty_label = empty_label

    def init_weights(self):
        if getattr(self, 'load_from', None) is not None and self.initialize_backbone is not None:
            import torch
            from opencood.misc.checkpoint_util import refine_load_from_sd
            print(f"Loading pretrained weights for SCE initializer from {self.load_from}")
            ckpt = torch.load(self.load_from, map_location='cpu')
            state_dict = ckpt.get('state_dict', ckpt)
            try:
                load_result = self.initialize_backbone.load_state_dict(state_dict, strict=False)
            except Exception as e:
                print(f"Refining state_dict for SCE initializer due to error: {e}")
                refined_state_dict = refine_load_from_sd(state_dict)
                load_result = self.initialize_backbone.load_state_dict(refined_state_dict, strict=False)
            print(f"SCE initializer load result: {load_result}")
            print("Pretrained Backbone for SCE initializer Loaded.")

    def forward(self, imgs, metas, **kwargs):
        """
        imgs: [B, N, C, H, W]
        metas: dict containing projection_mat, image_wh, occ_label, occ_cam_mask, etc.
        """
        if self.initialize_backbone is not None:
            b, n = imgs.shape[:2]
            initialize_input = imgs.flatten(0, 1)
            if self.initializer_img_downsample is not None:
                initialize_input = F.interpolate(
                    initialize_input, scale_factor=self.initializer_img_downsample, 
                    mode='bilinear', align_corners=True)
            feature_map = self.initialize_backbone(initialize_input)
            feature_map = feature_map.unflatten(0, (b, n))
        else:
            feature_map = kwargs.get('feature_map')
            if feature_map is None:
                raise ValueError("SCE needs feature_map if initialize_backbone is not provided.")

        B, N, C, H, W = feature_map.shape
        # reshape for conv2d
        x = feature_map.view(B * N, C, H, W)
        
        shared_feat = self.shared_encoder(x)
        
        complexity_map_geom = self.geom_head(shared_feat) # [B*N, 1, H, W]
        complexity_map_sem = self.sem_head(shared_feat) # [B*N, 1, H, W]
        
        complexity_map_geom = F.relu(complexity_map_geom)
        complexity_map_sem = F.relu(complexity_map_sem)
        
        complexity_map_geom = complexity_map_geom.view(B, N, 1, H, W)
        complexity_map_sem = complexity_map_sem.view(B, N, 1, H, W)
        
        # feature_sce = alpha * H_geom + beta * H_sem
        # [B, N, 1, H, W]
        feature_sce = self.alpha * complexity_map_geom + self.beta * complexity_map_sem 
        
        # --- Generate GT ---
        if kwargs.get("benchmarking", False) or "occ_label" not in metas:
            return feature_sce, complexity_map_geom, None, complexity_map_sem, None

        projection_mat = metas["projection_mat"].inverse()
        u = (torch.arange(W, dtype=feature_map.dtype, device=feature_map.device) + 0.5) / W
        v = (torch.arange(H, dtype=feature_map.dtype, device=feature_map.device) + 0.5) / H
        uv = torch.stack([u[None, :].expand(H, W), v[:, None].expand(H, W)], dim=-1) # [H, W, 2]
        uv = uv[None, None].expand(B, N, H, W, 2) * metas['image_wh'][:, :, None, None] # [B, N, H, W, 2]
        uvd = uv.unsqueeze(4).expand(B, N, H, W, self.num_samples, 2)
        uvd1 = torch.cat([uvd, torch.ones_like(uvd)], dim=-1) # [B, N, H, W, d, 4]
        uvd1[..., :3] = uvd1[..., :3] * self.depth_bins.view(1, 1, 1, 1, -1, 1)
        anchor_pts = projection_mat[:, :, None, None, None] @ uvd1[..., None]
        anchor_pts = anchor_pts.squeeze(-1)[..., :3] # [B, N, H, W, num_samples, 3]

        oob_mask = (anchor_pts[..., 0] < self.pc_range[0]) | (anchor_pts[..., 0] >= self.pc_range[3]) | \
                   (anchor_pts[..., 1] < self.pc_range[1]) | (anchor_pts[..., 1] >= self.pc_range[4]) | \
                   (anchor_pts[..., 2] < self.pc_range[2]) | (anchor_pts[..., 2] >= self.pc_range[5])
        
        anchor_idx = (anchor_pts - self.pc_start.view(1, 1, 1, 1, 1, 3)) / self.voxel_size
        anchor_idx = anchor_idx.to(torch.long)
        anchor_idx[..., 0].clamp_(0, self.occ_resolution[0] - 1)
        anchor_idx[..., 1].clamp_(0, self.occ_resolution[1] - 1)
        anchor_idx[..., 2].clamp_(0, self.occ_resolution[2] - 1)

        occupancy = metas["occ_label"] # [B, X, Y, Z]
        
        anchor_occ = torch.stack([occ[idx[..., 0], idx[..., 1], idx[..., 2]] for occ, idx in zip(occupancy, anchor_idx)])
        anchor_occ[oob_mask] = self.empty_label # [B, N, H, W, num_samples]

        # H_geom = Σ |O_k - O_{k-1}|
        # O_k = I(L_k != empty_label)
        O_k = (anchor_occ != self.empty_label).float() # [B, N, H, W, num_samples]
        O_diff = torch.abs(O_k[..., 1:] - O_k[..., :-1]) # [B, N, H, W, num_samples - 1]
        GT_geom = O_diff.sum(dim=-1, keepdim=True) # [B, N, H, W, 1]
        GT_geom = GT_geom.permute(0, 1, 4, 2, 3) # [B, N, 1, H, W]

        num_classes = int(self.empty_label) + 1
        
        valid_occ = anchor_occ.clamp(0, num_classes - 1).to(torch.long)
        
        occ_onehot = F.one_hot(valid_occ, num_classes=num_classes)
        
        ray_class_presence = occ_onehot.max(dim=4)[0]
        
        ray_class_presence[..., self.empty_label] = 0
        
        GT_sem = ray_class_presence.sum(dim=-1, keepdim=True).float() # [B, N, H, W, 1]
        GT_sem = GT_sem.permute(0, 1, 4, 2, 3) # [B, N, 1, H, W]

        return feature_sce, complexity_map_geom, GT_geom, complexity_map_sem, GT_sem
