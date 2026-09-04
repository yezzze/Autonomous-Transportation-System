import torch
import cv2
import torch.nn as nn
import numpy as np
from collections import Counter

import inspect
import importlib

from opencood.models.gaussian_modules.backbone import GaussianBackbone
from opencood.models.gaussian_modules.lifter import GaussianLifter
from opencood.models.gaussian_modules.lifter_sce import GaussianLifterSce
from opencood.models.gaussian_modules.lifterv2_sce import GaussianLifterV2Sce
from opencood.models.gaussian_modules.encoder import GaussianEncoder
from opencood.models.gaussian_modules.encoder_AHR import GaussianEncoderAHR
from opencood.models.gaussian_modules.gaussian_fuse_sce import (
    GaussianCollabRefiner,
    transform_neighbor_gaussians,
)

class GaussianCollabSce(nn.Module):
    def __init__(self, args):
        super(GaussianCollabSce, self).__init__()
        self.args = args
        modality_name_list = list(args.keys())
        modality_name_list = [x for x in modality_name_list if x.startswith("m") and x[1:].isdigit()]
        self.modality_name_list = modality_name_list

        modality_name = self.modality_name_list[0]
        model_setting = args[modality_name]
        
        self.pc_range = model_setting['refiner_args']['pc_range']
        self.num_gaussian = model_setting["lifter_args"]["num_anchor"]
        print("num_gaussian =", self.num_gaussian)

        """
        Backbone building
        """
        setattr(self, f"backbone_{modality_name}", GaussianBackbone(**model_setting['backbone_args']))

        """
        SCE building
        """
        sce_args = model_setting.get('sce_args', None)
        if sce_args is not None and sce_args.get('enable', False):
            from opencood.models.sce_models.sce import SCE
            setattr(self, f"sce_{modality_name}", SCE(**sce_args))
            self.enable_sce = True
        else:
            self.enable_sce = False

        """
        Lifter building
        """
        lifter_args = model_setting['lifter_args']
        lifter_type = lifter_args.get('type', 'v1')
        
        if lifter_type == 'v1':
            setattr(self, f"lifter_{modality_name}", GaussianLifter(**lifter_args))
        elif lifter_type == 'GaussianLifterSce':
            setattr(self, f"lifter_{modality_name}", GaussianLifterSce(**lifter_args))
        elif lifter_type == 'GaussianLifterV2' or lifter_type == 'GaussianLifterV2Sce':
            setattr(self, f"lifter_{modality_name}", GaussianLifterV2Sce(**lifter_args))
        else:
            raise ValueError(f"Unknown lifter type: {lifter_type}")

        """
        Encoder building
        """
        encoder_args = model_setting.get('encoder_args', None)
        if encoder_args:
            encoder_type = encoder_args.get('type', 'GaussianEncoderAHR')
            encoder_kwargs = {k: v for k, v in encoder_args.items() if k != 'type'}
            if encoder_type == 'GaussianEncoder':
                setattr(self, f"encoder_{modality_name}", GaussianEncoder(**encoder_kwargs))
            elif encoder_type == 'GaussianEncoderAHR':
                setattr(self, f"encoder_{modality_name}", GaussianEncoderAHR(**encoder_kwargs))

        """
        Collaboration refiner building
        """
        self.learned_refiner = model_setting.get('learned_refiner', False)
        if self.learned_refiner:
            setattr(self, f"refiner_{modality_name}", GaussianCollabRefiner(**model_setting['refiner_args']))

        """
        Fusion args building
        """
        self.fusion_args = model_setting.get('fusion_args', {})

        """
        Shared Heads
        """
        self.head_method = model_setting.get('head_method', None)

        if self.head_method == "occ_head":
            from opencood.models.gaussian_modules.occ_head import GaussianOccHead
            setattr(self, f"occ_head_{modality_name}", GaussianOccHead(**model_setting['occ_args']))

        elif self.head_method == "det_head":
            from opencood.models.gaussian_modules.det_head import GaussianDetHead
            setattr(self, f"det_head_{modality_name}", GaussianDetHead(**model_setting['det_args']))

        elif self.head_method == "seg_head":
            from opencood.models.gaussian_modules.seg_head import GaussianSegHead
            setattr(self, f"seg_head_{modality_name}", GaussianSegHead(**model_setting['seg_args']))

        else:
            assert False, "unknown head_method"

    def init_weights(self, verbose=True):
        """Universal init for GaussianCollab model."""
        for name, module in self.named_children():
            if hasattr(module, 'init_weights') and callable(module.init_weights):
                if verbose:
                    print(f"[Init] Initializing module: {name}")
                module.init_weights()

    def forward(self, data_dict, show_bev=False):
        output_dict = {'pyramid': 'collab'}
        record_len = data_dict['record_len'] # [2, 2, 4, 5]
        assert len(record_len) == 1, "only support one record_len"

        modality_name = self.modality_name_list[0]

        results = eval(f"self.backbone_{modality_name}")(data_dict, modality_name)

        # Inject occ_label and flatten metas for LifterV2 which expects them as kwargs
        if 'metas' in results:
            results.update(results['metas'])
        
        # Inject occ_label into results['metas'] specifically because LifterV2.forward(metas, **kwargs)
        # accesses metas['occ_label']. Since we updated results with metas, **results passes 
        # results['metas'] as the 'metas' argument to forward.
        if 'occ_label' in data_dict:
            results['metas']['occ_label'] = data_dict['occ_label']
            results['metas']['occ_cam_mask'] = data_dict.get('occ_cam_mask', None)
            # Also keep in top level just in case
            results['occ_label'] = data_dict['occ_label']
            results['occ_cam_mask'] = data_dict.get('occ_cam_mask', None)
        elif 'label_dict' in data_dict:
             results['metas']['occ_label'] = data_dict['label_dict'].get('occ_label', None)
             results['metas']['occ_cam_mask'] = data_dict['label_dict'].get('occ_cam_mask', None)
             # Also keep in top level just in case
             results['occ_label'] = data_dict['label_dict'].get('occ_label', None)
             results['occ_cam_mask'] = data_dict['label_dict'].get('occ_cam_mask', None)

        # Expand occ_label and occ_cam_mask to match batch size if needed (for LifterV2)
        # Assuming batch_size=1 (single scene), but multiple CAVs (so results['imgs'].shape[0] > 1)
        # We only have GT for Ego (index 0), but LifterV2 expects GT for all batch items.
        # We duplicate Ego's GT for others to satisfy shape requirements, but we will discard their outputs later.
        if 'imgs' in results and 'occ_label' in results['metas']:
            B_cav = results['imgs'].shape[0]
            occ_label = results['metas']['occ_label']
            if occ_label is not None and occ_label.shape[0] == 1 and B_cav > 1:
                results['metas']['occ_label'] = occ_label.expand(B_cav, -1, -1, -1)
                # Also update top level
                results['occ_label'] = results['metas']['occ_label']
            
            occ_cam_mask = results['metas'].get('occ_cam_mask')
            if occ_cam_mask is not None and occ_cam_mask.shape[0] == 1 and B_cav > 1:
                results['metas']['occ_cam_mask'] = occ_cam_mask.expand(B_cav, -1, -1, -1)
                # Also update top level
                results['occ_cam_mask'] = results['metas']['occ_cam_mask']

        if getattr(self, "enable_sce", False):
            sce_module = eval(f"self.sce_{modality_name}")
            
            if 'secondfpn_out' not in results:
                if sce_module.initialize_backbone is not None:
                    imgs = results.get('imgs', data_dict.get('imgs'))
                    if imgs is None and 'inputs_m4' in data_dict:
                        imgs = data_dict['inputs_m4'].get('imgs')
                    feature_sce, H_geom_pred, GT_geom, H_sem_pred, GT_sem = sce_module(
                        imgs=imgs, 
                        metas=results['metas'], 
                        benchmarking=data_dict.get('benchmarking', False)
                    )
                else:
                    raise ValueError("SCE needs feature_map if initialize_backbone is not provided.")
            else:
                feature_map = results['secondfpn_out']
                feature_sce, H_geom_pred, GT_geom, H_sem_pred, GT_sem = sce_module(
                    feature_map=feature_map, 
                    metas=results['metas'], 
                    benchmarking=data_dict.get('benchmarking', False)
                )

            results['feature_sce'] = feature_sce
            output_dict['complexity_map_geom'] = H_geom_pred
            output_dict['GT_geom'] = GT_geom
            output_dict['complexity_map_sem'] = H_sem_pred
            output_dict['GT_sem'] = GT_sem

        outs = eval(f"self.lifter_{modality_name}")(**results)

        # For LifterV2 in Collab mode, we only supervise the Ego vehicle (index 0).
        # Discard neighbor outputs for pixel_logits and pixel_gt to avoid training on garbage/duplicate GT.
        if 'pixel_logits' in outs:
            outs['pixel_logits'] = outs['pixel_logits'][0:1]
        if 'pixel_gt' in outs:
            outs['pixel_gt'] = outs['pixel_gt'][0:1]

        results.update(outs)

        outs = eval(f"self.encoder_{modality_name}")(**results)
        
        if 'GsSCE' in outs and outs['GsSCE'] is not None:
            results['GsSCE'] = outs['GsSCE']
            
        results.update(outs)

        num_of_gaussian_list = []
        comm_stats = {"baseline_num": 0, "actual_num": 0}

        # 在 Collaborator 端，我们不需要融合（生成最终检测占用的那一步）。
        # 只需要做两件事：
        #   A) 提取 Collaborator (index 1) 的高斯特征，通过 payload 发出去；
        #   B) 用与 Ego 融合时完全相同的 transform_neighbor_gaussians 流程，计算原生 comm_stats，
        #      确保 baseline_num = ROI+opacity 过滤后个数；actual_num = 再经过 zbuffer /
        #      bandwidth-aware (Gumbel-TopK, target_ratio=0.7) 过滤后的最终实际传输个数。
        #   这样 Baseline Gaussians 与 Actual Transmitted 的比值，天然与 ours 配置下
        #   enable_bandwidth_aware + target_bandwidth_ratio 保持一致，不会产生统计口径漂移。
        has_real_collab = record_len[0] > 1
        collaborator_idx = 1 if has_real_collab else 0
        collaborator_gaussian = results['representation'][-1]['gaussian']
        # gaussian_pred is a NamedTuple. We need to extract the index.
        from opencood.models.gaussian_modules.gaussian_utils import GaussianPrediction
        collab_gs = GaussianPrediction(
            means=collaborator_gaussian.means[collaborator_idx].unsqueeze(0),
            scales=collaborator_gaussian.scales[collaborator_idx].unsqueeze(0),
            rotations=collaborator_gaussian.rotations[collaborator_idx].unsqueeze(0),
            opacities=collaborator_gaussian.opacities[collaborator_idx].unsqueeze(0),
            semantics=collaborator_gaussian.semantics[collaborator_idx].unsqueeze(0),
        )

        collab_GsSCE = results.get('GsSCE', None)
        if collab_GsSCE is not None:
            collab_GsSCE = collab_GsSCE[collaborator_idx].unsqueeze(0)

        # ========== 关键：调用 transform_neighbor_gaussians 获取原生的两步过滤后的 comm_stats ==========
        # 对于 Collaborator 端，我们只关心它产出的 comm_stats（第 3 个返回值）；
        # fused_gaussian / fused_GsSCE / collab_dict / num_of_gaussian_list 在这里不需要使用。
        # 过滤链路（与 Ego 完全一致）：
        #   1) ROI (pc_range) + opacity >= 0.01   → baseline_num
        #   2) use_zbuffer_blind (可选)            → 继续过滤
        #   3) enable_bandwidth_aware (Gumbel TopK, target_ratio=0.7，来自 config.yaml fusion_args)
        #                                         → actual_num
        if has_real_collab:
            fusion_args = getattr(self, "fusion_args", {})
            gaussian_pred = results['representation'][-1]['gaussian']
            try:
                _, _, _comm, _, _ = transform_neighbor_gaussians(
                    gaussian_pred=gaussian_pred,
                    record_len=record_len,
                    pairwise_t_matrix=data_dict['pairwise_t_matrix'],
                    roi_bounds=self.pc_range,
                    GsSCE=results.get('GsSCE', None),
                    fusion_args=fusion_args,
                    metas=results.get('metas', None)
                )
                comm_stats["baseline_num"] = int(_comm.get("baseline_num", 0))
                comm_stats["actual_num"] = int(_comm.get("actual_num", 0))
            except Exception as e:
                # 任何异常情况下都回退到 baseline=actual，不影响程序正常跑完
                print(f"[Collaborator Warning] transform_neighbor_gaussians failed: {e}; "
                      f"fallback to baseline == actual (no bandwidth-aware filter in stats).")
                from opencood.models.gaussian_modules.gaussian_utils import GaussianPrediction as _GP
                pairwise_t_matrix = data_dict['pairwise_t_matrix']
                dev = gaussian_pred.means.device
                j = 1
                mean = gaussian_pred.means[j]
                opa = gaussian_pred.opacities[j]
                t = pairwise_t_matrix[0, j, 0].to(torch.float32).to(dev)
                ones = torch.ones(mean.shape[0], 1, device=dev)
                mean_ego = (t @ torch.cat([mean, ones], dim=-1).T).T[:, :3]
                x_min, y_min, z_min, x_max, y_max, z_max = self.pc_range
                inside_roi = (
                    (mean_ego[:, 0] > x_min) & (mean_ego[:, 0] < x_max - 1e-4) &
                    (mean_ego[:, 1] > y_min) & (mean_ego[:, 1] < y_max - 1e-4) &
                    (mean_ego[:, 2] > z_min) & (mean_ego[:, 2] < z_max - 1e-4)
                )
                opa_flat = opa if opa.ndim == 2 else opa.unsqueeze(-1)
                mask_opa = opa_flat[:, 0] >= 0.01
                _n = int((inside_roi & mask_opa).sum().item())
                comm_stats["baseline_num"] = _n
                comm_stats["actual_num"] = _n
        else:
            # 没有真正的协同车（record_len == 1），把发送个数明确记为 0
            comm_stats["baseline_num"] = 0
            comm_stats["actual_num"] = 0

        output_dict['collaborator_gaussian'] = collab_gs
        output_dict['collaborator_GsSCE'] = collab_GsSCE
        output_dict['comm_stats'] = comm_stats

        return output_dict
