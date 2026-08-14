
import torch.nn as nn
from . import OPENOCC_LOSS
from opencood.misc.tb_wrapper import WrappedTBWriter

if 'selfocc' in WrappedTBWriter._instance_dict:
    writer = WrappedTBWriter.get_instance('selfocc')
else:
    writer = None

@OPENOCC_LOSS.register_module()
class MultiLoss(nn.Module):

    def __init__(self, loss_cfgs):
        super().__init__()
        
        # Handle dict input (if passed as args from config)
        if isinstance(loss_cfgs, dict):
            if 'loss_cfgs' in loss_cfgs:
                loss_cfgs = loss_cfgs['loss_cfgs']
            else:
                # If it's a dict but not wrapping a list, maybe it's a single config? 
                # But MultiLoss expects a list of configs.
                # Or maybe it is the kwargs?
                pass

        assert isinstance(loss_cfgs, list)
        self.num_losses = len(loss_cfgs)
        
        losses = []
        for loss_cfg in loss_cfgs:
            losses.append(OPENOCC_LOSS.build(loss_cfg))
        self.losses = nn.ModuleList(losses)
        self.iter_counter = 0

    def forward(self, inputs, target_dict=None, suffix=""):
        # Adapt inputs if target_dict is provided (legacy call style)
        if target_dict is not None:
            # In this case, 'inputs' is actually 'output_dict'
            # We merge target_dict into it to form the complete input expected by sub-losses
            inputs.update(target_dict)
        
        self.loss_dict = {}
        tot_loss = 0.
        for loss_func in self.losses:
            loss = loss_func(inputs)
            tot_loss += loss
            
            if hasattr(loss_func, 'loss_dict'):
                 self.loss_dict.update(loss_func.loss_dict)

            self.loss_dict.update({
                loss_func.__class__.__name__: \
                loss.detach().item()
            })
            
        self.loss_dict['total_loss'] = tot_loss.detach().item()
        self.iter_counter += 1
        
        # Return only tot_loss if target_dict was passed (legacy behavior), 
        # or adapt to what train.py expects.
        # train.py expects a single return value (loss) if it's using the old interface.
        # However, train.py might also try to access logging.
        # To be safe, if we are in legacy mode (target_dict is not None), we return tot_loss.
        if target_dict is not None:
            return tot_loss

        return tot_loss, self.loss_dict

    def logging(self, epoch, batch_id, batch_len, writer=None, suffix=""):
        """
        Logging method to support train.py interface.
        Prints loss metrics per data point (batch) and writes to TensorBoard.
        """
        # Construct message
        msg = "[epoch %d][%d/%d]%s" % (epoch, batch_id + 1, batch_len, suffix)
        
        # Specific keys requested by user + total_loss
        # Expected: pixel_distribution_loss, loss_voxel_ce, loss_voxel_lovasz, OccupancyLoss, total_loss
        # We try to print these if they exist, otherwise print what we have.
        
        # Priority keys
        priority_keys = ['loss_voxel_ce', 'loss_voxel_lovasz', 'OccupancyLoss', 'pixel_distribution_loss', 'loss_sce_geom', 'loss_sce_sem', 'SCELoss', 'total_loss']
        printed_keys = set()
        
        for key in priority_keys:
            if key in self.loss_dict:
                msg += " || %s: %.4f" % (key, self.loss_dict[key])
                printed_keys.add(key)
                
        # Optional: Print other keys if necessary (omitted for cleaner output as per request)
        
        print(msg)
        
        # Write to TensorBoard
        if writer is not None:
             global_step = epoch * batch_len + batch_id
             for key, val in self.loss_dict.items():
                 writer.add_scalar(key + suffix, val, global_step)
