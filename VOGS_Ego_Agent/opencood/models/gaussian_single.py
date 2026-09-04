
import torch
import cv2
import torch.nn as nn
import numpy as np

import inspect
import importlib

from opencood.models.gaussian_modules.backbone import GaussianBackbone
from opencood.models.gaussian_modules.lifter import GaussianLifter
from opencood.models.gaussian_modules.lifterv2 import GaussianLifterV2
from opencood.models.gaussian_modules.encoder import GaussianEncoder
from opencood.models.gaussian_modules.occ_head import GaussianOccHead

class GaussianSingle(nn.Module):
    def __init__(self, args):
        super(GaussianSingle, self).__init__()
        self.args = args
        modality_name_list = list(args.keys())
        modality_name_list = [x for x in modality_name_list if x.startswith("m") and x[1:].isdigit()]
        self.modality_name_list = modality_name_list

        modality_name = self.modality_name_list[0]
        model_setting = args[modality_name]

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
            # Create a copy to avoid modifying the original dict if needed, 
            # though here we just pass kwargs.
            # GaussianLifterV2 captures **kwargs, so 'type' argument will be ignored/stored in kwargs
            # which is fine.
            setattr(self, f"lifter_{modality_name}", GaussianLifterV2(**lifter_args))
        else:
            raise ValueError(f"Unknown lifter type: {lifter_type}")

        """
        Encoder building
        """
        setattr(self, f"encoder_{modality_name}", GaussianEncoder(**model_setting['encoder_args']))

        """
        Shared Heads
        """
        setattr(self, f"occ_head_{modality_name}", GaussianOccHead(**model_setting['occ_args']))

    def init_weights(self, verbose=True):
        """Universal init for GaussianCollab model."""
        for name, module in self.named_children():
            if hasattr(module, 'init_weights') and callable(module.init_weights):
                if verbose:
                    print(f"[Init] Initializing module: {name}")
                module.init_weights()

    def forward(self, data_dict, show_bev=False):
        output_dict = {'pyramid': 'single'}

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

        outs = eval(f"self.lifter_{modality_name}")(**results)
        results.update(outs)

        outs = eval(f"self.encoder_{modality_name}")(**results)
        results.update(outs)

        results['metas'].update({
            'occ_xyz': data_dict['label_dict']['occ_xyz'],
            'occ_label': data_dict['label_dict']['occ_label'],
            'occ_cam_mask': data_dict['label_dict']['occ_cam_mask'],
        })

        output_dict.update({'gaussian': results['representation'][-1]['gaussian']})
        output_dict.update({'gaussians': [r['gaussian'] for r in results['representation']]})
        output_dict.update({'anchor_init': results['anchor_init']})

        output_dict.update(eval(f"self.occ_head_{modality_name}")(**results))

        # Merge pixel_logits and pixel_gt from lifter (needed for auxiliary losses)
        if 'pixel_logits' in results:
            output_dict['pixel_logits'] = results['pixel_logits']
        if 'pixel_gt' in results:
            output_dict['pixel_gt'] = results['pixel_gt']

        return output_dict
