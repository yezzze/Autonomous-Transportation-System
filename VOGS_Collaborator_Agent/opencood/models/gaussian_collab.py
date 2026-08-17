
import torch
import cv2
import torch.nn as nn
import numpy as np
from collections import Counter

import inspect
import importlib

from opencood.models.gaussian_modules.backbone import GaussianBackbone
from opencood.models.gaussian_modules.lifter import GaussianLifter
from opencood.models.gaussian_modules.lifterv2 import GaussianLifterV2
from opencood.models.gaussian_modules.encoder import GaussianEncoder
from opencood.models.gaussian_modules.gaussian_fuse import (
    GaussianCollabRefiner,
    transform_neighbor_gaussians,
)

class GaussianCollab(nn.Module):
    def __init__(self, args):
        super(GaussianCollab, self).__init__()
        self.args = args
        modality_name_list = list(args.keys())
        modality_name_list = [x for x in modality_name_list if x.startswith("m") and x[1:].isdigit()]
        self.modality_name_list = modality_name_list

        modality_name = self.modality_name_list[0]
        model_setting = args[modality_name]

        self.num_gaussian = model_setting["lifter_args"]["num_anchor"]
        print("num_gaussian =", self.num_gaussian)

        """
        Backbone building
        """
        setattr(self, f"backbone_{modality_name}", GaussianBackbone(**model_setting['backbone_args']))

        """
        Lifter building
        """
        lifter_args = model_setting['lifter_args']
        lifter_type = lifter_args.get('type', 'v1')
        
        if lifter_type == 'v1':
            setattr(self, f"lifter_{modality_name}", GaussianLifter(**lifter_args))
        elif lifter_type == 'GaussianLifterV2':
            setattr(self, f"lifter_{modality_name}", GaussianLifterV2(**lifter_args))
        else:
            raise ValueError(f"Unknown lifter type: {lifter_type}")

        """
        Encoder building
        """
        setattr(self, f"encoder_{modality_name}", GaussianEncoder(**model_setting['encoder_args']))

        """
        Collaboration refiner building
        """
        self.learned_refiner = model_setting.get('learned_refiner', False)
        if self.learned_refiner:
            setattr(self, f"refiner_{modality_name}", GaussianCollabRefiner(**model_setting['refiner_args']))

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

        outs = eval(f"self.lifter_{modality_name}")(**results)

        # For LifterV2 in Collab mode, we only supervise the Ego vehicle (index 0).
        # Discard neighbor outputs for pixel_logits and pixel_gt to avoid training on garbage/duplicate GT.
        if 'pixel_logits' in outs:
            outs['pixel_logits'] = outs['pixel_logits'][0:1]
        if 'pixel_gt' in outs:
            outs['pixel_gt'] = outs['pixel_gt'][0:1]

        results.update(outs)

        outs = eval(f"self.encoder_{modality_name}")(**results)
        results.update(outs)

        num_of_gaussian_list = []

        #fuse
        if record_len[0] > 1:
            # Fuse with shared neighbors
            fused_gaussian, num_of_gaussian_list = transform_neighbor_gaussians(
                gaussian_pred=results['representation'][-1]['gaussian'],
                record_len=record_len,
                pairwise_t_matrix=data_dict['pairwise_t_matrix'],
                roi_bounds=(-20, -20, -2.3, 20, 20, 0.9),
                # opacity_thresh=0.05
            )
            """
             Fuse ego Gaussians with shared Gaussians after transforming them to ego frame and filtering by ROI.
             Skip fusion if no valid shared neighbors.

                Inputs:
                 - gaussian_pred: GaussianPrediction with batch=1
                 - pairwise_t_matrix: transforms from other agents to ego frame (batch=1)
                 - record_len: number of CAVs in batch=1
                 - roi_bounds: (x_min, y_min, z_min, x_max, y_max, z_max)
                    """

            results['representation'][-1]['gaussian'] = fused_gaussian

            # Optional learned refinement
            if fused_gaussian.means.shape[1] > self.num_gaussian:
                if self.learned_refiner:
                    refiner = getattr(self, f"refiner_{modality_name}")
                    refined_gaussian = refiner(fused_gaussian)
                    results['representation'][-1]['gaussian'] = refined_gaussian

        # Fused metas
        if self.head_method == "occ_head":
            results['metas'].update({
                'occ_xyz': data_dict['label_dict']['occ_xyz'],
                'occ_label': data_dict['label_dict']['occ_label'],
                'occ_cam_mask': data_dict['label_dict']['occ_cam_mask'],
            })

        output_dict.update({'gaussian': results['representation'][-1]['gaussian']})
        output_dict.update({'gaussians': [r['gaussian'] for r in results['representation']]})
        output_dict.update({'anchor_init': results['anchor_init']})
        
        output_dict.update({'neighbor_gaussians': num_of_gaussian_list})
        
        output_dict.update(eval(f"self.occ_head_{modality_name}")(**results))

        # Merge pixel_logits and pixel_gt from lifter (needed for auxiliary losses)
        if 'pixel_logits' in results:
            output_dict['pixel_logits'] = results['pixel_logits']
        if 'pixel_gt' in results:
            output_dict['pixel_gt'] = results['pixel_gt']

        return output_dict
