import torch
import torch.nn.functional as F
from torch_scatter import scatter_min

def generate_ego_depth_map(ego_xyz, projection_mat, image_wh, kernel_size):
    """
    Generate a 2D depth map (Z-Buffer) from the ego vehicle's perspective using scatter_reduce_.
    
    Args:
        ego_xyz (torch.Tensor): [num_ego, 3] Ego Gaussians' coordinates in lidar frame.
        projection_mat (torch.Tensor): [N, 4, 4] Lidar-to-image projection matrices (for N cameras).
        image_wh (torch.Tensor): [N, 2] Image width and height for each camera.
        
    Returns:
        depth_map (torch.Tensor): [N, H_max, W_max] Depth map populated with min Z values (inf if empty).
    """
    N = projection_mat.shape[0]
    num_ego = ego_xyz.shape[0]
    
    # Get maximum dimensions for the depth map tensor
    max_w = int(image_wh[:, 0].max().item())
    max_h = int(image_wh[:, 1].max().item())
    
    # Initialize depth map with infinity
    device = ego_xyz.device
    depth_map = torch.full((N, max_h, max_w), float('inf'), dtype=torch.float32, device=device)
    
    if num_ego == 0:
        return depth_map
        
    # Convert ego_xyz to homogeneous coordinates: [num_ego, 4]
    xyz_homo = torch.cat([ego_xyz, torch.ones_like(ego_xyz[..., :1])], dim=-1)
    
    # Expand to [N, num_ego, 4]
    xyz_homo = xyz_homo.unsqueeze(0).expand(N, num_ego, 4)
    
    # Project points: proj_pts = projection_mat @ xyz_homo
    # projection_mat is [N, 4, 4], xyz_homo is [N, num_ego, 4]
    proj_pts = torch.bmm(projection_mat, xyz_homo.transpose(1, 2)).transpose(1, 2)  # [N, num_ego, 4]
    
    d = proj_pts[..., 2]  # Depth: [N, num_ego]
    
    # Add a small epsilon to avoid division by zero
    u = proj_pts[..., 0] / (d + 1e-6)  # Pixel u: [N, num_ego]
    v = proj_pts[..., 1] / (d + 1e-6)  # Pixel v: [N, num_ego]
    
    img_w = image_wh[:, 0].unsqueeze(1)  # [N, 1]
    img_h = image_wh[:, 1].unsqueeze(1)  # [N, 1]
    
    # Valid mask: depth > 0 and within image boundaries
    valid_mask = (d > 0) & (u >= 0) & (u < img_w) & (v >= 0) & (v < img_h)  # [N, num_ego]
    
    # Discard invalid points by setting their depth to infinity
    d_valid = torch.where(valid_mask, d, torch.tensor(float('inf'), device=device))
    
    # Convert coordinates to long for indexing
    u_idx = u.long()
    v_idx = v.long()
    
    # Clamp indices to avoid out-of-bounds due to numerical issues
    u_idx = u_idx.clamp(0, max_w - 1)
    v_idx = v_idx.clamp(0, max_h - 1)
    
    # Flatten spatial dimensions for scatter
    # We want to scatter into [N, max_h * max_w]
    flat_indices = v_idx * max_w + u_idx  # [N, num_ego]
    
    # Use scatter_min to find the minimum depth for each pixel
    # We scatter along dim 1 of a [N, max_h * max_w] tensor
    # Initialize output tensor with infinity
    flat_depth_map = torch.full((N, max_h * max_w), float('inf'), dtype=torch.float32, device=device)
    
    # scatter_min updates flat_depth_map in-place and returns (out, argmin)
    out, _ = scatter_min(d_valid, flat_indices, dim=1, out=flat_depth_map)
    
    depth_map = out.view(N, max_h, max_w)
    
    if kernel_size > 1:
        # Min-pooling: Apply max_pool2d on negative depths to find minimum valid depths
        depth_map_neg = -depth_map.unsqueeze(1)
        pad = kernel_size // 2
        
        # Max-pool on negative depth map to perform morphological dilation
        dilated_neg = F.max_pool2d(depth_map_neg, kernel_size=kernel_size, stride=1, padding=pad)
        
        depth_map = -dilated_neg.squeeze(1)
    
    return depth_map

def zbuffer_culling(sender_xyz, ego_depth_map, projection_mat, image_wh, epsilon=0.5):
    """
    Perform Z-Buffer culling on Sender's Gaussians.
    
    Args:
        sender_xyz (torch.Tensor): [num_sender, 3] Sender Gaussians' coordinates in ego lidar frame.
        ego_depth_map (torch.Tensor): [N, H_max, W_max] Ego's depth map.
        projection_mat (torch.Tensor): [N, 4, 4] Lidar-to-image projection matrices.
        image_wh (torch.Tensor): [N, 2] Image width and height for each camera.
        epsilon (float): Depth tolerance.
        
    Returns:
        blind_zone_mask (torch.Tensor): [num_sender] Boolean mask. True if occluded (in blind zone) or out of FOV.
    """
    N, max_h, max_w = ego_depth_map.shape
    num_sender = sender_xyz.shape[0]
    device = sender_xyz.device
    
    # Default to True (keep for transmission)
    blind_zone_mask = torch.ones(num_sender, dtype=torch.bool, device=device)
    
    if num_sender == 0:
        return blind_zone_mask
        
    # Convert sender_xyz to homogeneous coordinates: [num_sender, 4]
    xyz_homo = torch.cat([sender_xyz, torch.ones_like(sender_xyz[..., :1])], dim=-1)
    
    # Expand to [N, num_sender, 4]
    xyz_homo = xyz_homo.unsqueeze(0).expand(N, num_sender, 4)
    
    # Project points: [N, num_sender, 4]
    proj_pts = torch.bmm(projection_mat, xyz_homo.transpose(1, 2)).transpose(1, 2)
    
    d_true = proj_pts[..., 2]  # [N, num_sender]
    u = proj_pts[..., 0] / (d_true + 1e-6)  # [N, num_sender]
    v = proj_pts[..., 1] / (d_true + 1e-6)  # [N, num_sender]
    
    img_w = image_wh[:, 0].unsqueeze(1)  # [N, 1]
    img_h = image_wh[:, 1].unsqueeze(1)  # [N, 1]
    
    # Valid mask: depth > 0 and within image boundaries
    valid_mask = (d_true > 0) & (u >= 0) & (u < img_w) & (v >= 0) & (v < img_h)  # [N, num_sender]
    
    u_idx = u.long().clamp(0, max_w - 1)
    v_idx = v.long().clamp(0, max_h - 1)
    
    # For each camera, check if the sender's Gaussian is visible
    for i in range(N):
        cam_valid = valid_mask[i]  # [num_sender]
        
        # Get the corresponding D_true and Ego Depth for the valid points in this camera
        cam_d_true = d_true[i, cam_valid]
        cam_u = u_idx[i, cam_valid]
        cam_v = v_idx[i, cam_valid]
        
        cam_d_ego = ego_depth_map[i, cam_v, cam_u]
        
        # Condition 1: If D_true <= D_ego + epsilon, it is NOT occluded -> Ego can see it -> redundant
        # Therefore, we should discard it (Mask = False)
        # We also need to ignore inf depth values in the ego depth map (where ego has no observation)
        visible_mask = (cam_d_true <= (cam_d_ego + epsilon)) & ~torch.isinf(cam_d_ego)
        """
        Consideration:
        epsilon higher -----> more points filtered out.
        """
        
        # Update the global mask for these valid points
        # If a point is visible in ANY camera, it's considered redundant and should be filtered out
        # So we use bitwise AND with the logical NOT of visibility
        # (meaning if it's visible here, we set its overall mask to False)
        blind_zone_mask[cam_valid] = blind_zone_mask[cam_valid] & (~visible_mask)
        
    return blind_zone_mask
