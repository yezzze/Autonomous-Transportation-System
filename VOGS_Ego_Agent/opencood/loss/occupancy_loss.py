
import torch, torch.nn as nn
import torch.nn.functional as F
from torch.amp import autocast
import numpy as np

from .lovasz_softmax import lovasz_softmax
from .base_loss import BaseLoss
from . import OPENOCC_LOSS

# Surround train
nusc_class_frequencies = np.array([
    8921462,
    4077636,
    3336744,
    27356228,
    657075,
    89139632,
    22676500,
    9267481,
    13632227,
    4691101,
    3470079,
    57787,
    73495,
    # 1503762553
])
# [0.9622, 1.0117, 1.0253, 0.8993, 1.1496, 0.8413, 0.9092, 0.9600, 0.9374, 1.0025, 1.0226, 1.4045, 1.3744, 0.5]

@OPENOCC_LOSS.register_module()
class OccupancyLoss(BaseLoss):
    def __init__(self, 
                 weight=1.0,
                 empty_label=13,
                 num_classes=14,
                 use_sem_geo_scal_loss=False,
                 use_lovasz_loss=True,
                 lovasz_ignore=13,
                 ignore_empty=False,
                 lovasz_use_softmax=False,
                 balance_cls_weight=False,
                 manual_class_weight=None,
                 multi_loss_weights=None,
                 use_focal_loss=False,
                 use_dice_loss=False,
                 input_dict=None,
                 **kwargs):
        
        super().__init__(weight)
        
        if input_dict is None:
            self.input_dict = {
                'pred_occ': 'pred_occ',
                'sampled_xyz': 'sampled_xyz',
                'sampled_label': 'sampled_label',
                'occ_mask': 'occ_mask'
            }
        else:
            self.input_dict = input_dict

        self.loss_func = self.loss_voxel

        self.empty_label = empty_label
        self.num_classes = num_classes
        self.classes = list(range(self.num_classes))
        self.use_sem_geo_scal_loss = use_sem_geo_scal_loss
        self.use_lovasz_loss = use_lovasz_loss
        self.lovasz_ignore = lovasz_ignore
        self.ignore_empty = ignore_empty
        self.lovasz_use_softmax = lovasz_use_softmax

        if multi_loss_weights is None:
            multi_loss_weights = {}
        
        self.loss_voxel_ce_weight = multi_loss_weights.get('loss_voxel_ce_weight', 1.0)
        self.loss_voxel_sem_scal_weight = multi_loss_weights.get('loss_voxel_sem_scal_weight', 1.0)
        self.loss_voxel_geo_scal_weight = multi_loss_weights.get('loss_voxel_geo_scal_weight', 1.0)
        self.loss_voxel_lovasz_weight = multi_loss_weights.get('loss_voxel_lovasz_weight', 1.0)
        
        self.loss_dict = {}

        if balance_cls_weight:
            if manual_class_weight is not None:
                self.class_weights = torch.tensor(manual_class_weight)
            else:
                class_freqs = nusc_class_frequencies
                self.class_weights = torch.from_numpy(1 / np.log(class_freqs[:self.num_classes] + 0.001))
                raise ValueError("manual_class_weight must be provided when balance_cls_weight=True")
            self.class_weights = self.num_classes * F.normalize(self.class_weights, 1, -1)
        else:
            self.class_weights = torch.ones(self.num_classes)
            # assert False, "Need balance_cls_weight=True"
        
        # self.loss_dict_global = {}
        self.use_focal_loss = use_focal_loss
        if self.use_focal_loss:
            # Note: focal_loss_args is not defined in original code snippet context, assuming passed via kwargs or defaults
            # For now commenting out to avoid error if not provided
            # self.focal_loss = CustomFocalLoss(**kwargs.get('focal_loss_args', {}))
            pass

        self.use_dice_loss = use_dice_loss
        if self.use_dice_loss:
            # Assuming DiceLoss is available or imported if used
            # self.dice_loss = DiceLoss(
            #     class_weight=self.class_weights,
            #     loss_weight=2.0
            # )
            pass

    def loss_voxel(self, pred_occ, sampled_xyz, sampled_label, occ_mask=None):
        tot_loss = 0.

        aggregated_loss_dict = {}

        if self.ignore_empty:#0
            empty_mask = sampled_label != self.empty_label
            occ_mask = empty_mask if occ_mask is None else empty_mask & occ_mask.flatten(1)

        if occ_mask is not None: #1
            occ_mask = occ_mask.flatten(1)
            sampled_label = sampled_label[occ_mask][None]

        for semantics in pred_occ:
            if occ_mask is not None:#1
                semantics = semantics.transpose(1, 2)[occ_mask][None].transpose(1, 2)

            loss_dict = {}

            if self.use_focal_loss:
                # loss_dict['loss_voxel_ce'] = self.loss_voxel_ce_weight * self.focal_loss(
                #     semantics, sampled_label, sampled_xyz,
                #     self.class_weights.type_as(semantics),
                #     ignore_index=255
                # )
                pass
            else:   #1
                if self.lovasz_use_softmax: #1
                    loss_dict['loss_voxel_ce'] = self.loss_voxel_ce_weight * CE_ssc_loss(
                        semantics, sampled_label,
                        self.class_weights.type_as(semantics),
                        ignore_index=255
                    )
                else: #0
                    loss_dict['loss_voxel_ce'] = self.loss_voxel_ce_weight * CE_wo_softmax(
                        semantics, sampled_label,
                        self.class_weights.type_as(semantics),
                        ignore_index=255
                    )

            if self.use_sem_geo_scal_loss:#0
                scal_input = torch.softmax(semantics, dim=1) if self.lovasz_use_softmax else semantics
                loss_dict['loss_voxel_sem_scal'] = self.loss_voxel_sem_scal_weight * sem_scal_loss(
                    scal_input.clone(), sampled_label, ignore_index=255
                )
                loss_dict['loss_voxel_geo_scal'] = self.loss_voxel_geo_scal_weight * geo_scal_loss(
                    scal_input.clone(), sampled_label, ignore_index=255, non_empty_idx=self.empty_label
                )

            if self.use_lovasz_loss:#1
                lovasz_input = torch.softmax(semantics, dim=1) if self.lovasz_use_softmax else semantics
                loss_dict['loss_voxel_lovasz'] = self.loss_voxel_lovasz_weight * lovasz_softmax(
                    lovasz_input.transpose(1, 2).flatten(0, 1), sampled_label.flatten(), ignore=self.lovasz_ignore
                )

            if self.use_dice_loss:
                # loss_dict['loss_voxel_dice'] = self.dice_loss(semantics, sampled_label)
                pass

            loss = sum(loss_dict.values())
            tot_loss += loss

            # Accumulate component-wise loss
            for k, v in loss_dict.items():
                if k not in aggregated_loss_dict:
                    aggregated_loss_dict[k] = v.clone()
                else:
                    aggregated_loss_dict[k] += v

        # Average over number of predictions
        num_preds = len(pred_occ)
        avg_total_loss = tot_loss / num_preds
        avg_loss_dict = {k: v / num_preds for k, v in aggregated_loss_dict.items()}
        avg_loss_dict['total_loss'] = avg_total_loss
        
        # Update self.loss_dict for external access (e.g. MultiLoss)
        self.loss_dict = {k: v.item() for k, v in avg_loss_dict.items()}
        self.loss_dict['OccupancyLoss'] = avg_total_loss.item()

        return avg_total_loss

    # def forward is inherited from BaseLoss

    def logging(self, epoch, batch_id, batch_len, writer, pbar=None):
        """
        Print and log all keys in self.loss_dict dynamically.

        Parameters
        ----------
        epoch : int
            Current training epoch.
        batch_id : int
            Index of the current batch.
        batch_len : int
            Number of total batches in this epoch.
        writer : SummaryWriter
            TensorBoard writer.
        pbar : tqdm.tqdm, optional
            Progress bar for CLI logging.
        """
        # Prepare formatted loss string
        log_items = [f"[epoch {epoch}][{batch_id + 1}/{batch_len}]"]
        for k, v in self.loss_dict.items():
            if isinstance(v, torch.Tensor):
                v = v.detach().item()
            log_items.append(f"{k}: {v:.4f}")

        log_str = " || ".join(log_items)

        if pbar is not None:
            pbar.set_description(log_str)
        else:
            print(log_str)


        # Write to TensorBoard
        if writer is not None:
            global_step = epoch * batch_len + batch_id
            for k, v in self.loss_dict.items():
                if isinstance(v, torch.Tensor):
                    v = v.item()
                writer.add_scalar(k, v, global_step)

def CE_ssc_loss(pred, target, class_weights=None, ignore_index=255):
    """
    :param: prediction: the predicted tensor, must be [BS, C, ...]
    """
    criterion = nn.CrossEntropyLoss(
        weight=class_weights, ignore_index=ignore_index, reduction="mean"
    )
    with autocast('cuda', enabled=False):
        loss = criterion(pred, target.long())

    return loss


def CE_wo_softmax(pred, target, class_weights=None, ignore_index=255):
    """
    :param: prediction: the predicted tensor, must be [BS, C, ...]
    """
    pred = torch.clamp(pred, 1e-6, 1. - 1e-6)
    with autocast('cuda', enabled=False):
        loss = F.nll_loss(torch.log(pred), target.long(), class_weights, ignore_index=ignore_index)
    return loss

def sem_scal_loss(pred, target, ignore_index=255):
    # Implementation placeholder if needed, or remove call if not used
    # Assuming it exists in original codebase or imported
    return 0.0

def geo_scal_loss(pred, target, ignore_index=255, non_empty_idx=17):
    # Implementation placeholder
    return 0.0
