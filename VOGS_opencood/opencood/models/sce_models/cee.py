import torch
import torch.nn as nn
import torch.nn.functional as F

class CEE(nn.Module):
    def __init__(self, w1=1.0, w2=1.0, w3=1.0, w4=1.0, w5=1.0, update_mode="ema", ema_alpha=0.5):
        super(CEE, self).__init__()
        self.w1 = w1
        self.w2 = w2  
        self.w3 = w3  
        self.w4 = w4  
        self.w5 = w5  
        self.update_mode = update_mode
        self.ema_alpha = ema_alpha
        
        if self.update_mode == "gating":
            self.gating_net_sce = nn.Sequential(
                nn.Linear(6, 32),
                nn.LayerNorm(32),
                nn.GELU(),
                nn.Linear(32, 16),
                nn.LayerNorm(16),
                nn.GELU(),
                nn.Linear(16, 1),
                nn.Tanh()
            )

    def forward(self, GsSCE, delta_dict):
        """
        GsSCE: [B, num_anchor, 1]
        delta_dict: dict containing:
            - delta_xyz: [B, num_anchor, 3]
            - delta_scale: [B, num_anchor, 3]
            - delta_rotation: [B, num_anchor]
            - delta_opacity: [B, num_anchor, 1]
            - delta_semantic: [B, num_anchor]
        """
        
        norm_xyz = torch.norm(delta_dict["delta_xyz"].detach(), p=2, dim=-1, keepdim=True) # [B, num_anchor, 1]
        norm_scale = delta_dict["delta_scale"].detach().abs().mean(dim=-1, keepdim=True) # [B, num_anchor, 1]
        
        val_rot = delta_dict["delta_rotation"].detach()
        val_rot = val_rot.unsqueeze(-1) if val_rot.dim() == 2 else val_rot
        
        val_sem = delta_dict["delta_semantic"].detach()
        val_sem = val_sem.unsqueeze(-1) if val_sem.dim() == 2 else val_sem
        
        norm_opa = torch.norm(delta_dict["delta_opacity"].detach(), p=1, dim=-1, keepdim=True)

        M_l = (self.w1 * norm_xyz.squeeze(-1) + 
                   self.w2 * norm_scale.squeeze(-1) + 
                   self.w3 * val_rot.squeeze(-1) + 
                   self.w4 * val_sem.squeeze(-1) + 
                   self.w5 * norm_opa.squeeze(-1)) # [B, num_anchor]
        M_l = M_l.unsqueeze(-1) # [B, num_anchor, 1]
        
        if self.update_mode == "ema":
            
            M_min = M_l.min(dim=1, keepdim=True)[0]
            M_max = M_l.max(dim=1, keepdim=True)[0]
            M_norm = (M_l - M_min) / (M_max - M_min + 1e-6) 

            GsSCE_new = (1.0 - self.ema_alpha) * GsSCE + self.ema_alpha * M_norm
            
        elif self.update_mode == "gating":
            M_log = torch.log1p(M_l)
            x = torch.cat([GsSCE, norm_xyz, norm_scale, val_rot, val_sem, norm_opa], dim=-1) # [B, num_anchor, 6]
            
            x_log = torch.log1p(x.abs()) * torch.sign(x)
            
            gain = self.gating_net_sce(x_log) * 0.5 # [B, num_anchor, 1]
            
            GsSCE_new = F.relu(GsSCE + gain + (0.1 * M_log))
            
        else:
            raise ValueError(f"Unknown CEE update mode: {self.update_mode}")
            
        return GsSCE_new
