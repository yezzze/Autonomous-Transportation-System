import torch

def gumbel_topk_bandwidth_filter(agent_sce: torch.Tensor, valid_mask: torch.Tensor, target_ratio: float) -> torch.Tensor:
    valid_indices = torch.nonzero(valid_mask).squeeze(1)
    num_valid = len(valid_indices)
    
    if num_valid > 0:
        valid_sce = agent_sce[valid_indices]
        
        sce_min = valid_sce.min()
        sce_max = valid_sce.max()
        if sce_max > sce_min:
            sce_norm = (valid_sce - sce_min) / (sce_max - sce_min)
        else:
            sce_norm = torch.ones_like(valid_sce)
            
        sce_log = torch.log(sce_norm + 1e-8)
        
        U = torch.rand_like(sce_log)
        noise = -torch.log(-torch.log(U + 1e-8) + 1e-8)
        
        p_sce = sce_log + noise
        
        k = int(num_valid * target_ratio)
        
        if k > 0:
            if k < num_valid:
                _, topk_idx = torch.topk(p_sce, k)
                selected_valid_indices = valid_indices[topk_idx]
                
                keep_mask = torch.zeros_like(agent_sce, dtype=torch.bool)
                keep_mask[selected_valid_indices] = True
                
                return valid_mask & keep_mask
            else:
                return valid_mask
        else:
            new_valid_mask = valid_mask.clone()
            new_valid_mask[:] = False
            return new_valid_mask
            
    return valid_mask
