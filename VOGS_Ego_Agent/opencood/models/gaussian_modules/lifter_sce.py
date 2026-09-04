import torch
import torch.nn as nn
from opencood.models.gaussian_modules.gaussian_utils import safe_inverse_sigmoid
import inspect

class GaussianLifterSce(nn.Module):
    def __init__(
        self,
        type='v1',
        #num_anchor=25600,
        num_anchor=12800,
        embed_dims=128,
        anchor_grad=False,
        feat_grad=True,
        semantics=True,
        semantic_dim=13,
        include_opa=True,
        pts_init=False,
        pc_range=[-20.0, -20.0, -2.3, 20.0, 20.0, 0.9],
        **kwargs
    ):
        super().__init__()
        self.embed_dims = embed_dims
        self.pts_init = pts_init
        self.pc_range = pc_range
        assert not (pts_init and anchor_grad)

        xyz = torch.rand(num_anchor, 3, dtype=torch.float)
        xyz = safe_inverse_sigmoid(xyz)

        scale = torch.rand_like(xyz)
        scale = safe_inverse_sigmoid(scale)

        rots = torch.zeros(num_anchor, 4, dtype=torch.float)
        rots[:, 0] = 1

        # Initial opacity 0.5
        if include_opa:
            opacity = safe_inverse_sigmoid(0.5 * torch.ones((num_anchor, 1), dtype=torch.float))
        else:
            opacity = torch.ones((num_anchor, 0), dtype=torch.float)

        if semantics:
            assert semantic_dim is not None
        else:
            semantic_dim = 0
        semantic = torch.randn(num_anchor, semantic_dim, dtype=torch.float)

        anchor = torch.cat([xyz, scale, rots, opacity, semantic], dim=-1)

        self.num_anchor = num_anchor
        self.anchor = nn.Parameter(
            anchor.clone().detach().float(),
            requires_grad=anchor_grad
        )
        # fixed copy for reset/visualization
        self.anchor_init = anchor.clone().detach().float()
        self.instance_feature = nn.Parameter(
            torch.zeros([self.anchor.shape[0], self.embed_dims]),
            requires_grad=feat_grad,
        )

    def init_weights(self):
        with torch.no_grad():
            self.anchor.copy_(self.anchor_init)
        if self.instance_feature.requires_grad:
            torch.nn.init.xavier_uniform_(self.instance_feature.data, gain=1)

    def forward(self, imgs, metas, **kwargs):
        batch_size = imgs.shape[0] if imgs is not None else 1
        instance_feature = torch.tile(
            self.instance_feature[None], (batch_size, 1, 1)
        )
        if self.pts_init:
            if self.xyz_act == "sigmoid":
                xyz = safe_inverse_sigmoid(metas['anchor_points'])
            anchor = torch.cat([
                xyz, torch.tile(self.anchor[None, :, 3:], (batch_size, 1, 1))], dim=-1)
        else:
            anchor = torch.tile(self.anchor[None], (batch_size, 1, 1))
            
        GsSCE = None
        if "feature_sce" in kwargs and kwargs["feature_sce"] is not None:
            feature_sce = kwargs["feature_sce"] # [B, N, 1, H, W]
            
            xyz_norm = torch.sigmoid(anchor[..., :3])
            
            pc_start = torch.tensor(self.pc_range[:3], device=xyz_norm.device)
            pc_max = torch.tensor(self.pc_range[3:], device=xyz_norm.device)
            xyz_real = pc_start + xyz_norm * (pc_max - pc_start) # [B, num_anchor, 3]
            
            B, num_anchor, _ = xyz_real.shape
            _, N, _, H, W = feature_sce.shape
            
            xyz_homo = torch.cat([xyz_real, torch.ones_like(xyz_real[..., :1])], dim=-1).unsqueeze(1).expand(B, N, num_anchor, 4)
            
            projection_mat = metas["projection_mat"]
            
            proj_pts = projection_mat.unsqueeze(2) @ xyz_homo.unsqueeze(-1)
            proj_pts = proj_pts.squeeze(-1) # [B, N, num_anchor, 4]
            
            d = proj_pts[..., 2]
            u = proj_pts[..., 0] / (d + 1e-6)
            v = proj_pts[..., 1] / (d + 1e-6)
            
            img_w = metas["image_wh"][..., 0].unsqueeze(2) # [B, N, 1]
            img_h = metas["image_wh"][..., 1].unsqueeze(2) # [B, N, 1]
            
            valid_mask = (d > 0) & (u >= 0) & (u < img_w) & (v >= 0) & (v < img_h) # [B, N, num_anchor]
            
            u_norm = (u / img_w) * 2.0 - 1.0
            v_norm = (v / img_h) * 2.0 - 1.0
            grid = torch.stack([u_norm, v_norm], dim=-1) # [B, N, num_anchor, 2]
            
            import torch.nn.functional as F
            feat_flat = feature_sce.view(B * N, 1, H, W)
            grid_flat = grid.view(B * N, num_anchor, 1, 2)
            
            sampled_feat = F.grid_sample(feat_flat, grid_flat, mode='bilinear', padding_mode='zeros', align_corners=False)
            sampled_feat = sampled_feat.view(B, N, num_anchor) # [B, N, num_anchor]
            
            sampled_feat = sampled_feat * valid_mask.float()
            
            GsSCE, _ = sampled_feat.max(dim=1) # [B, num_anchor]
            GsSCE = GsSCE.unsqueeze(-1) # [B, num_anchor, 1]
            
        return {
            'rep_features': instance_feature, # Gaussian queries
            'representation': anchor,         # Gaussian properties
            'GsSCE': GsSCE,                   # Gaussian SCE complexities
            'anchor_init': self.anchor_init if not self.training else self.anchor_init.clone()
        }
