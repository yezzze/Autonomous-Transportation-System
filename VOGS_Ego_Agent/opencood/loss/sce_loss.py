import torch
import torch.nn as nn
import torch.nn.functional as F

from .base_loss import BaseLoss
from . import OPENOCC_LOSS

@OPENOCC_LOSS.register_module()
class SCELoss(BaseLoss):
    def __init__(self, weight=1.0, lambda1=1.0, lambda2=1.0, input_dict=None, **kwargs):
        super().__init__(weight)
        if input_dict is None:
            self.input_dict = {
                'H_geom_pred': 'complexity_map_geom',
                'GT_geom': 'GT_geom',
                'H_sem_pred': 'complexity_map_sem',
                'GT_sem': 'GT_sem'
            }
        else:
            self.input_dict = input_dict

        self.lambda1 = lambda1
        self.lambda2 = lambda2
        self.loss_func = self.compute_loss

    def compute_loss(self, H_geom_pred, GT_geom, H_sem_pred, GT_sem):
        # Using SmoothL1 loss as specified in whitepaper
        # H_geom_pred: [B, N, 1, H, W]
        # GT_geom: [B, N, 1, H, W]
        if H_geom_pred is None or GT_geom is None or H_sem_pred is None or GT_sem is None:
            self.loss_dict = {'loss_sce_geom': 0.0, 'loss_sce_sem': 0.0, 'SCELoss': 0.0}
            return torch.tensor(0.0, device='cuda')
            
        loss_geom = F.smooth_l1_loss(H_geom_pred, GT_geom)
        loss_sem = F.smooth_l1_loss(H_sem_pred, GT_sem)

        tot_loss = self.lambda1 * loss_geom + self.lambda2 * loss_sem

        self.loss_dict = {
            'loss_sce_geom': loss_geom.item(),
            'loss_sce_sem': loss_sem.item(),
            'SCELoss': tot_loss.item()
        }

        return tot_loss

    def logging(self, epoch, batch_id, batch_len, writer, pbar=None):
        msg = "[epoch %d][%d/%d]" % (epoch, batch_id + 1, batch_len)
        for k, v in self.loss_dict.items():
            msg += " || %s: %.4f" % (k, v)
            if writer is not None:
                writer.add_scalar('loss/' + k, v, epoch * batch_len + batch_id)
        if pbar is not None:
            pbar.set_description(msg)
        else:
            print(msg)
