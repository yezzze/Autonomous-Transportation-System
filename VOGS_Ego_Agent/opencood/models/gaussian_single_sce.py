import torch
import cv2
import torch.nn as nn
import numpy as np

import inspect
import importlib

from opencood.models.gaussian_modules.backbone import GaussianBackbone
from opencood.models.gaussian_modules.lifter import GaussianLifter
from opencood.models.gaussian_modules.lifter_sce import GaussianLifterSce
from opencood.models.gaussian_modules.encoder import GaussianEncoder
from opencood.models.gaussian_modules.encoder_AHR import GaussianEncoderAHR
from opencood.models.gaussian_modules.occ_head import GaussianOccHead

class GaussianSingleSce(nn.Module):
    def __init__(self, args):
        super(GaussianSingleSce, self).__init__()
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
            encoder_type = encoder_args.pop('type')
            if encoder_type == 'GaussianEncoder':
                setattr(self, f"encoder_{modality_name}", GaussianEncoder(**encoder_args))
            elif encoder_type == 'GaussianEncoderAHR':
                setattr(self, f"encoder_{modality_name}", GaussianEncoderAHR(**encoder_args))

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
        results.update(outs)

        outs = eval(f"self.encoder_{modality_name}")(**results)
        
        if 'GsSCE' in outs and outs['GsSCE'] is not None:
            results['GsSCE'] = outs['GsSCE']
            
        results.update(outs)

        results['metas'].update({
            'occ_xyz': data_dict['label_dict']['occ_xyz'],
            'occ_label': data_dict['label_dict']['occ_label'],
            'occ_cam_mask': data_dict['label_dict']['occ_cam_mask'],
        })

        output_dict.update({'gaussian': results['representation'][-1]['gaussian']})
        output_dict.update({'gaussians': [r['gaussian'] for r in results['representation']]})
        output_dict.update({'anchor_init': results['anchor_init']})
        if 'GsSCE' in results and results['GsSCE'] is not None:
            output_dict.update({'GsSCE': results['GsSCE']})

        output_dict.update(eval(f"self.occ_head_{modality_name}")(**results))

        # Merge pixel_logits and pixel_gt from lifter (needed for auxiliary losses)
        if 'pixel_logits' in results:
            output_dict['pixel_logits'] = results['pixel_logits']
        if 'pixel_gt' in results:
            output_dict['pixel_gt'] = results['pixel_gt']

        return output_dict
