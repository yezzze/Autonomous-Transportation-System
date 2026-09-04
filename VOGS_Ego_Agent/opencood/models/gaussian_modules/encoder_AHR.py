
import torch
import torch.nn as nn
from torch import Tensor

import numpy as np
from functools import partial
import spconv.pytorch as spconv

from typing import List, Optional, NamedTuple
from mmengine.model import xavier_init, constant_init

from opencood.models.gaussian_modules.ops.deformable_aggregation import DeformableAggregationFunction as DAF

import inspect
import torch.nn.functional as F

from opencood.models.gaussian_modules.gaussian_utils import (
    safe_sigmoid,
    cartesian,
    reverse_cartesian,
    linear_relu_ln,
    get_rotation_from_quaternion,
    Scale,
    GaussianPrediction
)

from opencood.models.sce_models.kmeans_1d import kmeans_1d

class GaussianEncoderAHR(nn.Module):
    def __init__(
        self,
        anchor_encoder=dict(),
        norm_layer=dict(normalized_shape=128),
        ffn=dict(),
        deformable_model=dict(),
        refine_layer=dict(),
        spconv_layer=dict(),
        num_decoder=4,
        operation_order: Optional[List[str]] = None,
        use_ahr=False,
        ahr_thresh_high=0.7,
        ahr_thresh_low=0.3,
        enable_cee=False,
        train_cee=False,
        cee_args=None,
        **kwargs
    ):
        super().__init__()

        self.num_decoder = num_decoder
        self.anchor_encoder = SparseGaussian3DEncoder(**anchor_encoder)
        self.train_cee = train_cee
        
        base_operation_order = [
            "identity",
            "deformable",
            "add",
            "norm",

            "identity",
            "ffn",
            "add",
            "norm",

            "identity",
            "spconv",
            "add",
            "norm",

            "identity",
            "ffn",
            "add",
            "norm",

            "refine",
        ]
        
        self.enable_cee = enable_cee

        self.operation_order = base_operation_order if not operation_order else operation_order.copy()
        if "cee" not in self.operation_order:
            self.operation_order.append("cee")
        self.operation_order *= num_decoder
        self.num_decoder = num_decoder
        self.use_ahr = use_ahr
        self.ahr_thresh_high = ahr_thresh_high
        self.ahr_thresh_low = ahr_thresh_low
        self.op_config_map = {
            # "norm": dict(type="LN", normalized_shape=embed_dims),
            "norm": lambda: nn.LayerNorm(**norm_layer),
            "ffn": lambda: AsymmetricFFN(**ffn),
            "deformable": lambda: DeformableFeatureAggregation(**deformable_model),
            "refine": lambda: SparseGaussian3DRefinementModuleV2(**refine_layer),
            "spconv": lambda: SparseConv3D(**spconv_layer),
        }
        
        from opencood.models.sce_models.cee import CEE
        self.op_config_map["cee"] = lambda: CEE(**(cee_args or {}))
        self.layers = nn.ModuleList()
        for i, op in enumerate(self.operation_order):
            cfg_or_module = self.op_config_map.get(op)
            if isinstance(cfg_or_module, dict):
                module = MODELS.build(cfg_or_module)
                assert False
            else:
                module = cfg_or_module() if callable(cfg_or_module) else cfg_or_module
            # print(f"Layer {i}, op: {op}, id: {id(module)}")
            self.layers.append(module)

    def init_weights(self):
        for i, op in enumerate(self.operation_order):
            if self.layers[i] is None:
                continue
            elif op != "refine":
                for p in self.layers[i].parameters():
                    if p.dim() > 1:
                        nn.init.xavier_uniform_(p)
        for m in self.modules():
            if hasattr(m, "init_weight"):
                m.init_weight()

    def forward(
        self,
        representation,
        rep_features,
        ms_img_feats=None,
        metas=None,
        GsSCE=None,
        **kwargs
    ):
        B = rep_features.shape[0]
        
        if B == 1:
            return self._forward_single(
                representation=representation,
                rep_features=rep_features,
                ms_img_feats=ms_img_feats,
                metas=metas,
                GsSCE=GsSCE,
                **kwargs
            )
        else:
            batch_results = []
            
            feature_maps = ms_img_feats
            if isinstance(feature_maps, torch.Tensor):
                feature_maps = [feature_maps]
            
            for b in range(B):
                b_rep = representation[b:b+1]
                b_feat = rep_features[b:b+1]
                b_gssce = GsSCE[b:b+1] if GsSCE is not None else None
                
                b_ms_img_feats = None
                if feature_maps is not None:
                    b_ms_img_feats = [fm[b:b+1] for fm in feature_maps]
                
                b_metas = None
                if metas is not None:
                    b_metas = {}
                    for k, v in metas.items():
                        if isinstance(v, torch.Tensor) and v.shape[0] == B:
                            b_metas[k] = v[b:b+1]
                        else:
                            b_metas[k] = v
                
                b_out = self._forward_single(
                    representation=b_rep,
                    rep_features=b_feat,
                    ms_img_feats=b_ms_img_feats,
                    metas=b_metas,
                    GsSCE=b_gssce,
                    **kwargs
                )
                batch_results.append(b_out)
            
            final_anchor = torch.cat([res['final_anchor'] for res in batch_results], dim=0)
            final_instance_feature = torch.cat([res['final_instance_feature'] for res in batch_results], dim=0)
            final_GsSCE = torch.cat([res['GsSCE'] for res in batch_results], dim=0) if GsSCE is not None else None
            
            prediction = []
            
            refine_layer = self.layers[-1] if getattr(self, "enable_cee", False) else self.layers[-2]
            if not hasattr(refine_layer, "get_xyz"):
                for module in reversed(self.layers):
                    if hasattr(module, "get_xyz"):
                        refine_layer = module
                        break
                        
            xyz_phy = refine_layer.get_xyz(final_anchor[..., :3])
            scale_val = safe_sigmoid(final_anchor[..., 3:6])
            scale_phy = refine_layer.scale_range[0] + (refine_layer.scale_range[1] - refine_layer.scale_range[0]) * scale_val
            
            anchor_sem = final_anchor[..., 11:]
            if refine_layer.semantics_activation == 'softmax':
                semantics_val = anchor_sem.softmax(dim=-1)
            elif refine_layer.semantics_activation == 'softplus':
                semantics_val = F.softplus(anchor_sem)
            else:
                semantics_val = anchor_sem
                
            merged_gaussian = GaussianPrediction(
                means=xyz_phy,
                scales=scale_phy,
                rotations=final_anchor[..., 6:10],
                opacities=safe_sigmoid(final_anchor[..., 10:11]) if refine_layer.include_opa else None,
                semantics=semantics_val,
                original_means=None,
                delta_means=None
            )
            prediction = [{'gaussian': merged_gaussian}]

            return {
                'representation': prediction,
                'final_anchor': final_anchor,
                'final_instance_feature': final_instance_feature,
                'GsSCE': final_GsSCE
            }

    def _forward_single(
        self,
        representation,
        rep_features,
        ms_img_feats=None,
        metas=None,
        GsSCE=None,
        **kwargs
    ):
        if GsSCE is not None:
            GsSCE = GsSCE.detach()

        feature_maps = ms_img_feats
        if isinstance(feature_maps, torch.Tensor):
            feature_maps = [feature_maps]
        instance_feature = rep_features  # Gaussian Queries      [1, num_anchor, 128]
        anchor = representation  # Gaussian Properties   [1, num_anchor, 24]
        anchor_embed = self.anchor_encoder(anchor)

        prediction = []
        layer_idx = 0

        finished_list = []

        for i, op in enumerate(self.operation_order):
            if op == 'spconv':
                instance_feature = self.layers[i](
                    instance_feature,
                    anchor
                )
            elif op == "norm" or op == "ffn":
                instance_feature = self.layers[i](instance_feature)
            elif op == "identity":
                identity = instance_feature
            elif op == "add":
                instance_feature = instance_feature + identity
            # cannot do this deformable attention if using lidar
            elif op == "deformable":
                instance_feature = self.layers[i](
                    instance_feature,
                    anchor,
                    anchor_embed,
                    feature_maps,
                    metas,
                )
            elif op == "refine":
                anchor, gaussian, delta_dict = self.layers[i](
                    instance_feature,
                    anchor,
                    anchor_embed,
                )

                prediction.append({
                    'gaussian': gaussian
                })
                
                current_delta_dict = delta_dict
                        
                if i != len(self.operation_order) - 1:
                    anchor_embed = self.anchor_encoder(anchor)
                    
            elif op == "cee":
                if self.enable_cee and GsSCE is not None:
                    GsSCE = self.layers[i](GsSCE, current_delta_dict)
                    
                layer_idx += 1
                
                if getattr(self, "use_ahr", False) and GsSCE is not None:
                    exit_mask = None
                    labels_float = None
                    B, num_anchor_current = GsSCE.shape[:2]
                    
                    if layer_idx == 3:
                        labels_float = kmeans_1d(GsSCE, k=3) # [B, N]
                        labels = labels_float.long()
                        exit_mask = (labels == 0)      # [B, N]
                    
                    elif layer_idx == 4:

                        labels_float = kmeans_1d(GsSCE, k=2) # [B, N_remaining]
                        labels = labels_float.long()

                        exit_mask = (labels == 0)      # [B, N_remaining]
                        
                    if exit_mask is not None:
                        
                        if self.training:
                            ste_hook = labels_float - labels_float.detach()
                            hook_multiplier = 1.0 if self.train_cee else 1e-7
                            ste_multiplier = 1.0 + ste_hook.unsqueeze(-1) * hook_multiplier
                            
                            instance_feature = instance_feature * ste_multiplier
                            anchor = anchor * ste_multiplier
                            anchor_embed = anchor_embed * ste_multiplier

                        exited_feature = instance_feature[exit_mask].unsqueeze(0) # [1, N_exit, C]
                        exited_anchor = anchor[exit_mask].unsqueeze(0)
                        exited_GsSCE = GsSCE[exit_mask].unsqueeze(0)
                        exited_embed = anchor_embed[exit_mask].unsqueeze(0)
                            
                        finished_list.append({
                            'instance_feature': exited_feature,
                            'anchor': exited_anchor,
                            'GsSCE': exited_GsSCE,
                            'anchor_embed': exited_embed
                        })
                        
                        keep_mask = ~exit_mask
                        
                        instance_feature = instance_feature[keep_mask].unsqueeze(0)
                        anchor = anchor[keep_mask].unsqueeze(0)
                        GsSCE = GsSCE[keep_mask].unsqueeze(0)
                        anchor_embed = anchor_embed[keep_mask].unsqueeze(0)
                        
            else:
                raise NotImplementedError(f"{op} is not supported.")

        finished_list.append({
            'instance_feature': instance_feature,
            'anchor': anchor,
            'GsSCE': GsSCE,
            'anchor_embed': anchor_embed
        })
        
        final_instance_feature = torch.cat([item['instance_feature'] for item in finished_list], dim=1)
        final_anchor = torch.cat([item['anchor'] for item in finished_list], dim=1)
        
        if GsSCE is not None:
            final_GsSCE = torch.cat([item['GsSCE'] for item in finished_list], dim=1)
        else:
            final_GsSCE = None
        
        refine_layer = self.layers[-1] if self.enable_cee else self.layers[-2]
        if not hasattr(refine_layer, "get_xyz"):
            # fallback find
            for module in reversed(self.layers):
                if hasattr(module, "get_xyz"):
                    refine_layer = module
                    break
        
        xyz_phy = refine_layer.get_xyz(final_anchor[..., :3])
        
        scale_val = safe_sigmoid(final_anchor[..., 3:6])
        scale_phy = refine_layer.scale_range[0] + (refine_layer.scale_range[1] - refine_layer.scale_range[0]) * scale_val
        
        anchor_sem = final_anchor[..., 11:]
        if refine_layer.semantics_activation == 'softmax':
            semantics_val = anchor_sem.softmax(dim=-1)
        elif refine_layer.semantics_activation == 'softplus':
            semantics_val = F.softplus(anchor_sem)
        else:
            semantics_val = anchor_sem
            
        final_gaussian = GaussianPrediction(
            means=xyz_phy,  # world
            scales=scale_phy,
            rotations=final_anchor[..., 6:10],
            opacities=safe_sigmoid(final_anchor[..., 10:11]) if refine_layer.include_opa else None,
            semantics=semantics_val,
            original_means=None,
            delta_means=None
        )
        
        if prediction:
            prediction[-1] = {'gaussian': final_gaussian}

        return {
            'representation': prediction,
            'final_anchor': final_anchor,
            'final_instance_feature': final_instance_feature,
            'GsSCE': final_GsSCE
        }

class SparseGaussian3DEncoder(nn.Module):
    def __init__(
        self,
        embed_dims: int = 256,
        include_opa=True,
        semantics=False,
        semantic_dim=None
    ):
        super().__init__()
        self.embed_dims = embed_dims
        self.include_opa = include_opa
        self.semantics = semantics

        def embedding_layer(input_dims):
            return nn.Sequential(*linear_relu_ln(embed_dims, 1, 2, input_dims))

        self.xyz_fc = embedding_layer(3)
        self.scale_fc = embedding_layer(3)
        self.rot_fc = embedding_layer(4)
        if include_opa:
            self.opacity_fc = embedding_layer(1)
        if semantics:
            assert semantic_dim is not None
            self.semantics_fc = embedding_layer(semantic_dim)
            self.semantic_start = 10 + int(include_opa)
        else:
            semantic_dim = 0
        self.semantic_dim = semantic_dim
        self.output_fc = embedding_layer(self.embed_dims)

    def forward(self, box_3d: torch.Tensor):
        xyz_feat = self.xyz_fc(box_3d[..., :3])
        scale_feat = self.scale_fc(box_3d[..., 3:6])
        rot_feat = self.rot_fc(box_3d[..., 6:10])
        if self.include_opa:
            opacity_feat = self.opacity_fc(box_3d[..., 10:11])
        else:
            opacity_feat = 0.
        if self.semantics:
            semantic_feat = self.semantics_fc(
                box_3d[..., self.semantic_start: (self.semantic_start + self.semantic_dim)])
        else:
            semantic_feat = 0.

        output = xyz_feat + scale_feat + rot_feat + opacity_feat + semantic_feat
        output = self.output_fc(output)
        return output

class AsymmetricFFN(nn.Module):
    def __init__(
        self,
        in_channels=128,
        pre_norm=False,
        embed_dims=128,
        feedforward_channels=4,
        num_fcs=2,
        ffn_drop=0.1,
        dropout_layer=None,
        add_identity=False,
    ):
        super(AsymmetricFFN, self).__init__()
        assert num_fcs >= 2, (
            "num_fcs should be no less " f"than 2. got {num_fcs}."
        )
        self.in_channels = in_channels
        self.embed_dims = embed_dims
        self.feedforward_channels = embed_dims * feedforward_channels
        self.num_fcs = num_fcs
        self.activate = nn.ReLU(inplace=True)

        layers = []
        if in_channels is None:
            in_channels = embed_dims
        self.pre_norm = None
        if pre_norm:
            # self.pre_norm = build_norm_layer(pre_norm, in_channels)[1]
            self.pre_norm = nn.LayerNorm(in_channels)

        # Feedforward layers
        for _ in range(num_fcs - 1):
            layers.append(
                nn.Sequential(
                    nn.Linear(in_channels, self.feedforward_channels),
                    self.activate,
                    nn.Dropout(ffn_drop),
                )
            )
            in_channels = self.feedforward_channels

        # Final projection to embed_dims
        layers.append(nn.Linear(self.feedforward_channels, embed_dims))
        layers.append(nn.Dropout(ffn_drop))
        self.layers = nn.Sequential(*layers)
        # self.dropout_layer = (
        #     build_dropout(dropout_layer)
        #     if dropout_layer
        #     else torch.nn.Identity()
        # )
        self.dropout_layer = nn.Identity()
        self.add_identity = add_identity
        if self.add_identity:
            self.identity_fc = (
                torch.nn.Identity()
                if in_channels == embed_dims
                else nn.Linear(self.in_channels, embed_dims)
            )

    def forward(self, x, identity=None):
        if self.pre_norm is not None:
            x = self.pre_norm(x)
        out = self.layers(x)
        if not self.add_identity:
            return self.dropout_layer(out)
        if identity is None:
            identity = x
        identity = self.identity_fc(identity)
        return identity + self.dropout_layer(out)

class SparseConv3D(nn.Module):
    def __init__(
            self,
            in_channels=128,
            embed_channels=128,
            pc_range=[-20.0, -20.0, -2.3, 20.0, 20.0, 0.9],
            grid_size=[1.0, 1.0, 1.0],
            use_out_proj=False,
            kernel_size=5,
            use_multi_layer=False,
    ):
        super().__init__()

        if use_multi_layer:
            self.layer = spconv.SparseSequential(
                spconv.SubMConv3d(in_channels, embed_channels, kernel_size, 1, (kernel_size - 1) // 2),
                nn.LayerNorm(embed_channels),
                nn.ReLU(True),
                spconv.SubMConv3d(embed_channels, embed_channels, kernel_size, 1, (kernel_size - 1) // 2),
                nn.LayerNorm(embed_channels),
                nn.ReLU(True),
                spconv.SubMConv3d(embed_channels, embed_channels, kernel_size, 1, (kernel_size - 1) // 2),
                nn.LayerNorm(embed_channels),
                nn.ReLU(True),
            )
        else:
            self.layer = spconv.SubMConv3d(
                in_channels,
                embed_channels,
                kernel_size=kernel_size,
                padding=(kernel_size - 1) // 2,
                bias=False
            )
        if use_out_proj:
            self.output_proj = nn.Linear(embed_channels, embed_channels)
        else:
            self.output_proj = nn.Identity()
        self.get_xyz = partial(cartesian, pc_range=pc_range)
        self.register_buffer('pc_range', torch.tensor(pc_range, dtype=torch.float))
        self.register_buffer('grid_size', torch.tensor(grid_size, dtype=torch.float))

    def forward(self, instance_feature, anchor):
        # anchor: b, g, 11
        # instance_feature: b, g, c
        bs, g, _ = instance_feature.shape

        # sparsify
        anchor_xyz = anchor[..., :3]
        anchor_xyz = self.get_xyz(anchor_xyz).flatten(0, 1)

        # indices = anchor_xyz - anchor_xyz.min(0, keepdim=True)[0]
        indices = anchor_xyz - self.pc_range[None, :3]
        indices = indices / self.grid_size[None, :]  # bg, 3
        indices = indices.to(torch.int32)
        batched_indices = torch.cat([
            torch.arange(bs, device=indices.device, dtype=torch.int32).reshape(
                bs, 1, 1).expand(-1, g, -1).flatten(0, 1), indices], dim=-1)

        # spatial_shape = indices.max(0)[0]
        spatial_shape = (self.pc_range[3:] - self.pc_range[:3]) / self.grid_size
        spatial_shape = spatial_shape.to(torch.int32)

        input = spconv.SparseConvTensor(
            instance_feature.flatten(0, 1),  # bg, c
            indices=batched_indices,  # bg, 4
            spatial_shape=spatial_shape,
            batch_size=bs
        )

        output = self.layer(input)
        output = output.features.unflatten(0, (bs, g))

        return self.output_proj(output)

class SparseGaussian3DKeyPointsGenerator(nn.Module):
    def __init__(
            self,
            embed_dims=128,
            num_learnable_pts=6,
            learnable_fixed_scale=6,
            fix_scale=[
                [0, 0, 0],
                [0.45, 0, 0],
                [-0.45, 0, 0],
                [0, 0.45, 0],
                [0, -0.45, 0],
                [0, 0, 0.45],
                [0, 0, -0.45],
            ],
            pc_range=[-20.0, -20.0, -2.3, 20.0, 20.0, 0.9],
            scale_range=[0.01, 3.2],
    ):
        super(SparseGaussian3DKeyPointsGenerator, self).__init__()
        self.embed_dims = embed_dims
        self.num_learnable_pts = num_learnable_pts
        self.learnable_fixed_scale = learnable_fixed_scale
        if fix_scale is None:
            fix_scale = ((0.0, 0.0, 0.0),)
        self.fix_scale = np.array(fix_scale)
        self.num_pts = len(self.fix_scale) + num_learnable_pts
        if num_learnable_pts > 0:
            self.learnable_fc = nn.Linear(self.embed_dims, num_learnable_pts * 3)

        self.pc_range = pc_range
        self.scale_range = scale_range
        # self.xyz_act = xyz_activation
        # self.scale_act = scale_activation

    def init_weight(self):
        if self.num_learnable_pts > 0:
            xavier_init(self.learnable_fc, distribution="uniform", bias=0.0)

    def forward(
            self,
            anchor,
            instance_feature=None,
    ):
        bs, num_anchor = anchor.shape[:2]
        # (3, 6400, 10)
        fix_scale = anchor.new_tensor(self.fix_scale)
        # (7, 3)
        scale = fix_scale[None, None].tile([bs, num_anchor, 1, 1])
        # (3, 6400, 7, 3)
        if self.num_learnable_pts > 0 and instance_feature is not None:
            learnable_scale = (
                    safe_sigmoid(self.learnable_fc(instance_feature)
                                 .reshape(bs, num_anchor, self.num_learnable_pts, 3))
                    - 0.5
                # (-0.5,0.5)
            )
            scale = torch.cat([scale, learnable_scale * self.learnable_fixed_scale], dim=-2)

        # (3, 6400, 7 + 6, 3)

        gs_scales = anchor[..., None, 3:6]
        gs_scales = safe_sigmoid(gs_scales)
        gs_scales = self.scale_range[0] + (self.scale_range[1] - self.scale_range[0]) * gs_scales
        key_points = scale * gs_scales

        rots = anchor[..., 6:10]
        rotation_mat = get_rotation_from_quaternion(rots).transpose(-1, -2)

        key_points = torch.matmul(
            rotation_mat[:, :, None], key_points[..., None]
        ).squeeze(-1)

        xyz = anchor[..., :3]
        xyz = safe_sigmoid(xyz)

        xxx = xyz[..., 0] * (self.pc_range[3] - self.pc_range[0]) + self.pc_range[0]
        yyy = xyz[..., 1] * (self.pc_range[4] - self.pc_range[1]) + self.pc_range[1]
        zzz = xyz[..., 2] * (self.pc_range[5] - self.pc_range[2]) + self.pc_range[2]
        xyz = torch.stack([xxx, yyy, zzz], dim=-1)

        key_points = key_points + xyz.unsqueeze(2)

        # each gaussian has fixed (7) plus learnable (6) key points
        return key_points

class DeformableFeatureAggregation(nn.Module):
    def __init__(
        self,
        embed_dims: int = 128,
        num_groups: int = 4,
        num_levels: int = 4,
        num_cams: int = 4,
        proj_drop: float = 0.0,
        attn_drop: float = 0.15,
        kps_generator: dict = dict(),
        use_deformable_func=True,
        use_camera_embed=True,
        residual_mode="none",
    ):
        super(DeformableFeatureAggregation, self).__init__()
        if embed_dims % num_groups != 0:
            raise ValueError(
                f"embed_dims must be divisible by num_groups, "
                f"but got {embed_dims} and {num_groups}"
            )
        self.group_dims = int(embed_dims / num_groups)
        self.embed_dims = embed_dims
        self.num_levels = num_levels
        self.num_groups = num_groups
        self.num_cams = num_cams
        self.use_deformable_func = use_deformable_func and DAF is not None
        assert self.use_deformable_func
        self.attn_drop = attn_drop
        self.residual_mode = residual_mode
        self.proj_drop = nn.Dropout(proj_drop)
        self.kps_generator = SparseGaussian3DKeyPointsGenerator(**kps_generator)
        self.num_pts = self.kps_generator.num_pts
        self.output_proj = nn.Linear(embed_dims, embed_dims)

        if use_camera_embed:
            self.camera_encoder = nn.Sequential(
                *linear_relu_ln(embed_dims, 1, 2, 12)
            )
            self.weights_fc = nn.Linear(
                embed_dims, num_groups * num_levels * self.num_pts
            )
        else:
            self.camera_encoder = None
            self.weights_fc = nn.Linear(
                embed_dims, num_groups * num_cams * num_levels * self.num_pts
            )

    def init_weight(self):
        constant_init(self.weights_fc, val=0.0, bias=0.0)
        xavier_init(self.output_proj, distribution="uniform", bias=0.0)

    def forward(
            self,
            instance_feature: torch.Tensor,
            anchor: torch.Tensor,
            anchor_embed: torch.Tensor,
            feature_maps: List[torch.Tensor],
            metas: dict,
            **kwargs: dict,
    ):
        bs, num_anchor = instance_feature.shape[:2]
        key_points = self.kps_generator(anchor, instance_feature)
        # temp_key_points_list = (
        #     feature_queue
        # ) = meta_queue = temp_anchor_embeds = []
        # Properly initialize queues (empty and independent)
        temp_key_points_list = []
        feature_queue = []
        meta_queue = []
        temp_anchor_embeds = []

        if self.use_deformable_func:
            feature_maps = DAF.feature_maps_format(feature_maps)

        for (
                temp_feature_maps,
                temp_metas,
                temp_key_points,
                temp_anchor_embed,
        ) in zip(
            feature_queue[::-1] + [feature_maps],
            meta_queue[::-1] + [metas],
            temp_key_points_list[::-1] + [key_points],
            temp_anchor_embeds[::-1] + [anchor_embed],
        ):
            # This for loop is only executed once.
            # Compute attention weights between instance features and anchor embeddings
            weights, weight_mask = self._get_weights(
                instance_feature, temp_anchor_embed, metas
            )
            if self.use_deformable_func:
                # Reshape attention weights for deformable aggregation
                weights = (
                    weights.permute(0, 1, 4, 2, 3, 5)
                    .contiguous()
                    .reshape(
                        bs,
                        num_anchor,
                        self.num_pts,
                        self.num_cams,
                        self.num_levels,
                        self.num_groups,
                    )
                )
                weight_mask = (
                    weight_mask.permute(0, 1, 4, 2, 3, 5)
                    .contiguous()
                    .reshape(
                        bs,
                        num_anchor,
                        self.num_pts,
                        self.num_cams,
                        self.num_levels,
                        self.num_groups,
                    )
                )
                # Project 3D keypoints to image plane for sampling
                points_2d, mask = self.project_points(
                    temp_key_points,
                    temp_metas["projection_mat"],
                    temp_metas.get("image_wh"),
                )
                points_2d = points_2d.permute(0, 2, 3, 1, 4).reshape(
                    bs, num_anchor * self.num_pts, self.num_cams, 2)

                # Combine mask with predicted weight mask
                mask = mask.permute(0, 2, 3, 1)
                mask = mask[..., None, None] & weight_mask

                # Check whether all cameras miss for each point
                all_miss = mask.sum(dim=[2, 3, 4], keepdim=True) == 0
                all_miss = all_miss.expand(-1, -1, self.num_pts, self.num_cams, self.num_levels, -1)

                # Invalidate weights for missing projections
                weights[~mask] = - torch.inf
                weights[all_miss] = 0.

                # Normalize weights across spatial/camera dimensions
                weights = weights.flatten(2, 4).softmax(dim=-2).reshape(
                    bs,
                    num_anchor * self.num_pts,
                    self.num_cams,
                    self.num_levels,
                    self.num_groups)
                # weights_clone = weights.detach().clone()
                # weights_clone[~all_miss.flatten(1, 2)] = 0.
                # weights = weights - weights_clone

                # Reweight features by valid attention
                weights = weights * (1 - all_miss.flatten(1, 2).float())

                # Sample multi-view features at 2D projected locations using learned weights
                temp_features_next = DAF.apply(
                    *temp_feature_maps, points_2d, weights
                ).reshape(bs, num_anchor, self.num_pts, self.embed_dims)
            else:
                temp_features_next = self.feature_sampling(
                    temp_feature_maps,
                    temp_key_points,
                    temp_metas["projection_mat"],
                    temp_metas.get("image_wh"),
                )
                temp_features_next = self.multi_view_level_fusion(
                    temp_features_next, weights
                )

            features = temp_features_next
            # print(f"[{__file__}:{inspect.currentframe().f_lineno}]", features.shape)

        features = features.sum(dim=2)  # fuse multi-point features
        output = self.proj_drop(self.output_proj(features))
        if self.residual_mode == "add":
            output = output + instance_feature
        elif self.residual_mode == "cat":
            output = torch.cat([output, instance_feature], dim=-1)
        return output

    def _get_weights(self, instance_feature, anchor_embed, metas=None):
        bs, num_anchor = instance_feature.shape[:2]
        feature = instance_feature + anchor_embed
        if self.camera_encoder is not None:
            camera_embed = self.camera_encoder(
                metas["projection_mat"][:, :, :3].reshape(
                    bs, self.num_cams, -1
                )
            )
            feature = feature[:, :, None] + camera_embed[:, None]
        weights = (
            self.weights_fc(feature)
            .reshape(bs, num_anchor, -1, self.num_groups)
            # .softmax(dim=-2)
            .reshape(
                bs,
                num_anchor,
                self.num_cams,
                self.num_levels,
                self.num_pts,
                self.num_groups,
            )
        )
        if self.training and self.attn_drop > 0:
            # mask = torch.rand(
            #     bs, num_anchor, self.num_cams, 1, self.num_pts, 1
            # )
            # mask = mask.to(device=weights.device, dtype=weights.dtype)
            # weights = ((mask > self.attn_drop) * weights) / (
            #     1 - self.attn_drop
            # )
            mask = torch.rand_like(weights)
            mask = mask > self.attn_drop
        else:
            mask = torch.ones_like(weights) > 0
        return weights, mask

    @staticmethod
    def project_points(key_points, projection_mat, image_wh=None):
        bs, num_anchor, num_pts = key_points.shape[:3]

        pts_extend = torch.cat(
            [key_points, torch.ones_like(key_points[..., :1])], dim=-1
        )
        points_2d = torch.matmul(
            projection_mat[:, :, None, None], pts_extend[:, None, ..., None]
        ).squeeze(-1)
        depth = points_2d[..., 2]
        points_2d = points_2d[..., :2] / torch.clamp(
            points_2d[..., 2:3], min=1e-5
        )
        if image_wh is not None:
            points_2d = points_2d / image_wh[:, :, None, None]
        # print(f"[{__file__}:{inspect.currentframe().f_lineno}]", key_points, points_2d)
        mask = (depth > 1e-5) & (points_2d[..., 0] > 0) & (points_2d[..., 0] < 1) & \
               (points_2d[..., 1] > 0) & (points_2d[..., 1] < 1)
        # print(f"[{__file__}:{inspect.currentframe().f_lineno}]", sum(mask))
        return points_2d, mask

    @staticmethod
    def feature_sampling(
            feature_maps: List[torch.Tensor],
            key_points: torch.Tensor,
            projection_mat: torch.Tensor,
            image_wh: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        num_levels = len(feature_maps)
        num_cams = feature_maps[0].shape[1]
        bs, num_anchor, num_pts = key_points.shape[:3]

        points_2d, _ = DeformableFeatureAggregation.project_points(
            key_points, projection_mat, image_wh
        )
        points_2d = points_2d * 2 - 1
        points_2d = points_2d.flatten(end_dim=1)

        # print(f"[{__file__}:{inspect.currentframe().f_lineno}]", points_2d.shape)

        features = []
        for fm in feature_maps:
            features.append(
                torch.nn.functional.grid_sample(
                    fm.flatten(end_dim=1), points_2d
                )
            )
        features = torch.stack(features, dim=1)
        features = features.reshape(
            bs, num_cams, num_levels, -1, num_anchor, num_pts
        ).permute(
            0, 4, 1, 2, 5, 3
        )  # bs, num_anchor, num_cams, num_levels, num_pts, embed_dims

        return features

    def multi_view_level_fusion(
            self,
            features: torch.Tensor,
            weights: torch.Tensor,
    ):
        bs, num_anchor = weights.shape[:2]
        features = weights[..., None] * features.reshape(
            features.shape[:-1] + (self.num_groups, self.group_dims)
        )
        features = features.sum(dim=2).sum(dim=2)
        features = features.reshape(
            bs, num_anchor, self.num_pts, self.embed_dims
        )
        return features

class SparseGaussian3DRefinementModuleV2(nn.Module): 
    def __init__(
        self,
        embed_dims=128,
        pc_range=[-20.0, -20.0, -2.3, 20.0, 20.0, 0.9],
        scale_range=[0.01, 3.2],
        unit_xyz=[4.0, 4.0, 1.0],
        semantics=True,
        semantic_dim=13,
        include_opa=True,
        semantics_activation='identity',
    ):
        super().__init__()
        self.embed_dims = embed_dims

        if semantics:
            assert semantic_dim is not None
        else:
            semantic_dim = 0

        self.output_dim = 10 + int(include_opa) + semantic_dim
        self.semantic_start = 10 + int(include_opa)
        self.semantic_dim = semantic_dim
        self.include_opa = include_opa
        self.semantics_activation = semantics_activation

        self.pc_range = pc_range
        self.scale_range = scale_range
        self.register_buffer("unit_xyz", torch.tensor(unit_xyz, dtype=torch.float), False)
        self.get_xyz = partial(cartesian, pc_range=pc_range, use_sigmoid=True)
        self.reverse_xyz = partial(reverse_cartesian, pc_range=pc_range, use_sigmoid=True)

        self.layers = nn.Sequential(
            *linear_relu_ln(embed_dims, 2, 2),
            nn.Linear(self.embed_dims, self.output_dim),
            Scale([1.0] * self.output_dim)
        )

    def forward(
            self,
            instance_feature: torch.Tensor,
            anchor: torch.Tensor,
            anchor_embed: torch.Tensor,
    ):
        # position offset
        output = self.layers(instance_feature + anchor_embed)

        #### for xyz ：(-1, 1) * unit_xyz
        delta_xyz = (2 * safe_sigmoid(output[..., :3]) - 1.) * self.unit_xyz[None, None]
        original_xyz = self.get_xyz(anchor[..., :3])  # SIGMOID + map
        anchor_xyz = original_xyz + delta_xyz
        anchor_xyz = self.reverse_xyz(anchor_xyz)

        #### for scale
        anchor_scale = output[..., 3:6]
        old_scale_phy = self.scale_range[0] + (self.scale_range[1] - self.scale_range[0]) * safe_sigmoid(anchor[..., 3:6])
        new_scale_phy = self.scale_range[0] + (self.scale_range[1] - self.scale_range[0]) * safe_sigmoid(anchor_scale)
        delta_scale = new_scale_phy - old_scale_phy

        #### for rotation
        anchor_rotation = output[..., 6:10]
        anchor_rotation = torch.nn.functional.normalize(anchor_rotation, 2, -1) # Impoetant：normalize this vector to ensure it represents a valid rotation quaternion
        assert torch.isfinite(anchor_rotation).all(), "anchor_rotation has NaN or Inf values after normalization."
        q_prev = anchor[..., 6:10]
        q_curr = anchor_rotation
        q_prev_conj = q_prev.clone()
        q_prev_conj[..., 1:] = -q_prev_conj[..., 1:]
        # w1, x1, y1, z1 = q_curr; w2, x2, y2, z2 = q_prev_conj
        w1, x1, y1, z1 = q_curr.unbind(dim=-1)
        w2, x2, y2, z2 = q_prev_conj.unbind(dim=-1)
        delta_w = w1*w2 - x1*x2 - y1*y2 - z1*z2
        delta_rotation = 1.0 - torch.abs(delta_w)

        #### for opacity
        anchor_opa = output[..., 10:(10 + int(self.include_opa))]
        old_opa_phy = safe_sigmoid(anchor[..., 10:(10 + int(self.include_opa))])
        new_opa_phy = safe_sigmoid(anchor_opa)
        delta_opacity = new_opa_phy - old_opa_phy

        #### for semantic
        anchor_sem = output[..., self.semantic_start:(self.semantic_start + self.semantic_dim)]
        old_sem_logits = anchor[..., self.semantic_start:(self.semantic_start + self.semantic_dim)]
        old_prob = old_sem_logits.softmax(dim=-1)
        new_prob = anchor_sem.softmax(dim=-1)
        # KL_div(new || old) = sum(new * log(new / old))
        delta_semantic = torch.sum(new_prob * (torch.log(new_prob + 1e-6) - torch.log(old_prob + 1e-6)), dim=-1)

        output = torch.cat([
            anchor_xyz, anchor_scale, anchor_rotation, anchor_opa, anchor_sem], dim=-1)

        # To physical
        xyz = self.get_xyz(anchor_xyz)
        scale = safe_sigmoid(anchor_scale)
        scale = self.scale_range[0] + (self.scale_range[1] - self.scale_range[0]) * scale

        if self.semantics_activation == 'softmax':
            semantics = anchor_sem.softmax(dim=-1)
        elif self.semantics_activation == 'softplus':
            semantics = F.softplus(anchor_sem)
        else:
            semantics = anchor_sem

        gaussian = GaussianPrediction(
            means=xyz,  # world
            scales=scale,
            rotations=anchor_rotation,
            opacities=safe_sigmoid(anchor_opa),
            semantics=semantics,
            original_means=original_xyz,
            delta_means=delta_xyz
        )
        delta_dict = {
            "delta_xyz": delta_xyz,          # [B, num_anchor, 3]
            "delta_scale": delta_scale,      # [B, num_anchor, 3]
            "delta_rotation": delta_rotation,# [B, num_anchor]
            "delta_opacity": delta_opacity,  # [B, num_anchor, 1]
            "delta_semantic": delta_semantic # [B, num_anchor]
        }
        
        return output, gaussian, delta_dict  # , semantics

if __name__ == '__main__':
    GaussianEncoder()
