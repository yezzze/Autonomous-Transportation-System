import torch
import numpy as np
import numba as nb
import os

from opencood.models.gaussian_modules.gaussian_utils import get_meshgrid
from opencood.data_utils.post_processor.base_postprocessor import BasePostprocessor

class OccPostprocessor(BasePostprocessor):
    def __init__(self,
        anchor_params,
        train,
    ):
        self.params = anchor_params
        self.train = train
        self.cav_lidar_range = self.params['gt_range']
        self.min_bound = np.array(self.cav_lidar_range[:3])
        self.max_bound = np.array(self.cav_lidar_range[3:])
        self.grid_size = np.array(self.params['grid_size'])
        self.intervals = (self.max_bound - self.min_bound) / self.grid_size

    def generate_anchor_box(self):
        return None

    def generate_label(self, **kwargs):

        # -----------------------------
        # Get 3D center coordinates of each voxel
        # -----------------------------
        occ_xyz = get_meshgrid(self.cav_lidar_range, self.grid_size, self.intervals)

        if kwargs['voxel_label_20'] is not None:
            if isinstance(kwargs['voxel_label_20'], str):
                if os.path.exists(kwargs['voxel_label_20']):
                    # Load from file (GsFormer logic)
                    label_file = kwargs['voxel_label_20']
                    label = np.load(label_file)
                    
                    # Initialize with empty label (from config or default 17 as in GsFormer)
                    # Config usually has empty_label in occ_args but not passed here directly.
                    # Using 17 as per GsFormer standard for now, or 13 if following config.
                    # Assuming 13 based on existing code comment.
                    empty_lbl = 13 # default in existing code
                    
                    voxel_label = np.ones(tuple(self.grid_size), dtype=np.int64) * empty_lbl
                    
                    # Filter labels to ensure they are within grid bounds
                    valid_mask = (label[:, 0] >= 0) & (label[:, 0] < self.grid_size[0]) & \
                                 (label[:, 1] >= 0) & (label[:, 1] < self.grid_size[1]) & \
                                 (label[:, 2] >= 0) & (label[:, 2] < self.grid_size[2])
                    label = label[valid_mask]
                    
                    voxel_label[label[:, 0].astype(int), label[:, 1].astype(int), label[:, 2].astype(int)] = label[:, 3].astype(int)
                    
                    return {
                        "occ_label": voxel_label,
                        "occ_xyz": occ_xyz,
                        "occ_cam_mask": voxel_label != 0
                    }
                else:
                    # construct from raw points
                    semantic_xyz = kwargs["semantic_xyz"]
                    semantic_class = kwargs["semantic_class"]

                    cav_label, cav_grid_ind = point_cut(
                        semantic_class, semantic_xyz, self.min_bound, self.max_bound, self.intervals,
                        self.grid_size
                    )

                    voxel_label = np.ones(self.grid_size, dtype=np.uint8) * 13 # empty

                    # Merge voxel indices and labels
                    voxel_pair = np.concatenate([cav_grid_ind, cav_label[:, None]], axis=1)
                    voxel_pair = voxel_pair[np.lexsort((cav_grid_ind[:, 0], cav_grid_ind[:, 1], cav_grid_ind[:, 2]))]

                    # Apply fast label assignment to voxel grid
                    voxel_label = nb_process_label(np.copy(voxel_label), voxel_pair)

                    return {
                        "occ_label": voxel_label,
                        "occ_xyz": occ_xyz,
                        "occ_cam_mask": voxel_label != 0
                    }
            else:
                assert False
                voxel_label = kwargs['voxel_label_20']
                return {
                    "occ_label": voxel_label,
                    "occ_xyz": occ_xyz,
                    "occ_cam_mask": voxel_label != 0
                }
        #co3
        elif kwargs['voxel_label_co3sop'] is not None: # logics for generating labels using CO3SOP's directly provided voxel labels
            if isinstance(kwargs['voxel_label_co3sop'], str):
                # load from pre-processed numpy file
                voxel_label = np.load(kwargs['voxel_label_co3sop'])['voxels'] # 256 x 256 x 24
                
                """
                downsampled_labels = multiscale_supervision_priority(
                    torch.from_numpy(voxel_label).unsqueeze(0), ratio=2, gt_shape=(1, 1, 128, 128, 12), priority_map=None
                ).squeeze(0).numpy() # downsample to 128 x 128 x 12
                
                cropped_labels = downsampled_labels[14:-14, 14:-14, 0:-4].astype(np.uint8) # crop to 100 x 100 x 8, from lidar range [-25.6, -25.6, -2.0, 25.6, 25.6, 2.8] to [-20, -20, -2.0, 20, 20, 1.2]
                
                mapped_labels = kwargs['label_mapping'][cropped_labels] # map from co3sop labels to labels used in VOGS
                """

                return {
                    "occ_label": voxel_label,
                    "occ_xyz": occ_xyz,
                    "occ_cam_mask": voxel_label != 0
                }

        return {}


    def post_process(self, data_dict, output_dict, **kwargs):
        res = {
            'final_occ': output_dict['ego']['final_occ'],
            'neighbor_gaussians': output_dict['ego'].get('neighbor_gaussians', []),
            'gaussian': output_dict['ego']['gaussian'],
            'gaussians': output_dict['ego']['gaussians'],
            'anchor_init': output_dict['ego']['anchor_init'],
            'GsSCE': output_dict['ego'].get('GsSCE', None),
        }
        if 'collab_dict' in output_dict['ego']:
            res['collab_dict'] = output_dict['ego']['collab_dict']
        return res, None


    def generate_gt(self, data_dict, **kwargs):
        return {
            'sampled_label': data_dict['ego']['label_dict']['occ_label'].flatten(1),
            'occ_mask': data_dict['ego']['label_dict']['occ_cam_mask'],
        }


    def collate_batch(self, label_batch_list):
        """
        Customized collate function for target label generation.

        Parameters
        ----------
        label_batch_list : list
            List of dictionaries containing occupancy-related labels for each frame.

        Returns
        -------
        target_batch : dict
            Reformatted labels as torch tensors.
        """
        occ_xyz = []
        occ_label = []
        occ_cam_mask = []

        for label_dict in label_batch_list:
            occ_xyz.append(torch.tensor(label_dict["occ_xyz"]))  # (X, Y, Z, 3)
            occ_label.append(torch.tensor(label_dict["occ_label"]))  # (X, Y, Z)
            occ_cam_mask.append(torch.tensor(label_dict["occ_cam_mask"]))  # (X, Y, Z)

        if occ_xyz:
            occ_xyz = torch.stack(occ_xyz, dim=0)  # (B, X, Y, Z, 3)
        else:
            occ_xyz = torch.empty(0)

        if occ_label:
            occ_label = torch.stack(occ_label, dim=0)  # (B, X, Y, Z)
        else:
            occ_label = torch.empty(0)

        if occ_cam_mask:
            occ_cam_mask = torch.stack(occ_cam_mask, dim=0)  # (B, X, Y, Z)
        else:
            occ_cam_mask = torch.empty(0)

        return {
            "occ_label": occ_label,  # torch.Tensor, (B, X, Y, Z)
            "occ_xyz": occ_xyz,  # torch.Tensor, (B, X, Y, Z, 3)
            "occ_cam_mask": occ_cam_mask  # torch.Tensor, (B, X, Y, Z)
        }


def point_cut(lidar_label, lidar_xyz, min_bound, max_bound, intervals, cur_grid_size):
    """
    Filter points that lie within the specified 3D bounding box and compute their voxel indices.

    Args:
        lidar_label (np.ndarray): Labels for each point.
        lidar_xyz (np.ndarray): Coordinates of each point.
        min_bound (np.ndarray): Lower bound of 3D grid (x, y, z).
        max_bound (np.ndarray): Upper bound of 3D grid (x, y, z).
        intervals (np.ndarray): Size of each voxel cell.
        cur_grid_size (np.ndarray): Total number of voxels in each dimension.

    Returns:
        tuple: Filtered labels and voxel grid indices.
    """
    mask = np.all((lidar_xyz >= min_bound) & (lidar_xyz <= max_bound), axis=1)
    lidar_xyz = lidar_xyz[mask]
    lidar_label = lidar_label[mask]
    grid_ind = np.floor((lidar_xyz - min_bound) / intervals).astype(int)
    grid_ind = np.clip(grid_ind, 0, cur_grid_size - 1)
    return lidar_label, grid_ind


@nb.jit('u1[:,:,:](u1[:,:,:],i8[:,:])', nopython=True, cache=True)
def nb_process_label(processed_label, sorted_label_voxel_pair):
    """
    Assigns voxel labels by majority vote using sorted (voxel index, class) pairs.

    Args:
        processed_label (np.ndarray): (H, W, D) initialized with unknown label.
        sorted_label_voxel_pair (np.ndarray): (N, 4) array: (x, y, z, class_id).

    Returns:
        np.ndarray: Updated voxel label grid.
    """
    label_size = 256
    counter = np.zeros((label_size,), dtype=np.uint16)
    # Initialize first voxel
    counter[sorted_label_voxel_pair[0, 3]] = 1
    cur_voxel = sorted_label_voxel_pair[0, :3]

    for i in range(1, sorted_label_voxel_pair.shape[0]):
        cur_ind = sorted_label_voxel_pair[i, :3]
        class_id = sorted_label_voxel_pair[i, 3]
        if not np.all(cur_ind == cur_voxel):
            # Assign label by majority vote
            processed_label[cur_voxel[0], cur_voxel[1], cur_voxel[2]] = np.argmax(counter)
            counter.fill(0)
            cur_voxel = cur_ind

        counter[class_id] += 1

    processed_label[cur_voxel[0], cur_voxel[1], cur_voxel[2]] = np.argmax(counter)
    return processed_label
#co3 ?????
def multiscale_supervision_priority(gt_occ, ratio, gt_shape, priority_map=None):
    '''
    Self-defined priority based downsampling method for voxel labels from Co3SOP
    
    Uses the Painter's Algorithm to assign labels to downsampled voxels, ensuring that critical classes (e.g., roadlines, pedestrians) 
    are preserved even if they occupy a small portion of the voxel, while less critical classes (e.g., vegetation, roads) can be overwritten if they are present in the same voxel. 
    This approach helps mitigate issues of lane line offset and small object label disappearance during downsampling.
    '''
    
    # 如果没有传入外部定义的 map，则使用默认的 Co3SOP 配置
    if priority_map is None:
        # 你的类别列表 (Index 即 ID)
        # 0: empty, 1: buildings, 2: fences, 3: other, 4: pedestrians
        # 5: poles, 6: roadlines, 7: roads, 8: sidewalks, 9: vegetation
        # 10: vehicles, 11: walls, 12: trafficsigns, 13: sky, 14: ground
        # 15: bridge, 16: railtrack, 17: guardrail, 18: trafficlight
        # 19: static, 20: dydamic, 21: water, 22: terrain, 23: unlabeled
        
        # 初始化 (假设最大ID不超过255)
        priority_map = torch.zeros(256, device=gt_occ.device, dtype=torch.long)
        
        # --- Level 0: 忽略或极低优先级 (Priority: 0-5) ---
        priority_map[0]  = 0  # empty
        priority_map[23] = 0  # unlabeled
        priority_map[13] = 1  # sky
        
        # --- Level 1: 大面积自然背景 (Priority: 10) ---
        # 这些物体体积大，即使被吃掉一点边缘也无所谓
        priority_map[9]  = 10 # vegetation (树木经常遮挡杆子，放低一点)
        priority_map[21] = 10 # water
        priority_map[22] = 10 # terrain
        priority_map[14] = 10 # ground
        priority_map[3]  = 10 # other
        
        # --- Level 2: 大面积人造背景 (Priority: 20) ---
        priority_map[7]  = 20 # roads (路面)
        priority_map[8]  = 20 # sidewalks
        priority_map[1]  = 20 # buildings
        priority_map[11] = 20 # walls
        priority_map[15] = 20 # bridge
        priority_map[16] = 20 # railtrack
        
        # --- Level 3: 细小/条状静态结构 (Priority: 50-60) ---
        # 关键！Roadlines (6) 必须比 Roads (7) 优先级高，有些roadlines会被标为static所以static与Roadlines优先级一样
        # 否则会被路面覆盖导致偏移
        priority_map[6]  = 65 # roadlines (车道线) -> 设为 60，稳压 Road(20)
        priority_map[5]  = 55 # poles (杆子)
        priority_map[12] = 55 # trafficsigns (标志牌)
        priority_map[18] = 55 # trafficlight (红绿灯)
        priority_map[2]  = 55 # fences (栅栏)
        priority_map[17] = 55 # guardrail (护栏)
        priority_map[19] = 55 # static (一般静态杂物)
        priority_map[20] = 65  # dynamic (其他动态物体) 关注细小动态物体的保留
        priority_map[10] = 50 # vehicles (车) 优先级放低，因为车体积较大，吃掉一点边缘无所谓
        
        # --- Level 4: 动态/核心感知目标 (Priority: 100) ---
        # 绝对不能被覆盖的物体
        priority_map[4]  = 100 # pedestrians (人)

    bs = gt_occ.shape[0]
    gt_pts = []
    
    for i in range(bs):
        # 1. 获取非零点
        non_zeros = torch.nonzero(gt_occ[i]) 
        values = gt_occ[i][non_zeros[:,0], non_zeros[:,1], non_zeros[:,2]]
        
        # 2. 根据 map 获取优先级
        current_priorities = priority_map[values.long()] 
        
        # 3. 排序 (argsort 从小到大，意味着高优先级的在 tensor 末尾)
        sort_indices = torch.argsort(current_priorities)
        
        sorted_non_zeros = non_zeros[sort_indices]
        sorted_values = values[sort_indices]
        
        pts = torch.cat([
            sorted_non_zeros,
            sorted_values.unsqueeze(1)
        ], dim=1)
        gt_pts.append(pts.float())

    gt = torch.zeros([gt_shape[0], gt_shape[2], gt_shape[3], gt_shape[4]]).to(gt_occ.device).type(torch.float) 
    
    for i in range(gt.shape[0]):
        #coords = gt_pts[i][:, :3].type(torch.long) // ratio
        coords = torch.div(gt_pts[i][:, :3].type(torch.long), ratio, rounding_mode='trunc').type(torch.long)
        # 高优先级的点在后，执行覆盖操作
        gt[i, coords[:, 0], coords[:, 1], coords[:, 2]] =  gt_pts[i][:, 3]
    
    return gt 