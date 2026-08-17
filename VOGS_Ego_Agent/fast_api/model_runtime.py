import os
import sys
import torch
import numpy as np
import time
from torch.utils.data import DataLoader, Subset
from tqdm import tqdm
import statistics
import json

current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import opencood.hypes_yaml.yaml_utils as yaml_utils
from opencood.tools import train_utils, inference_utils
from opencood.data_utils.datasets import build_dataset
from opencood.utils.common_utils import update_dict
from opencood.utils.seg_iou import mean_IU
from opencood.misc.metric_util import MeanIoU
from Latency_Test.benchmark_latency import compute_bev_from_voxels, compute_iou, eval_occseg_result

class Opts:
    def __init__(self):
        self.model_dir = "Latency_Test/ours/collab"
        self.fusion_method = "intermediate"
        self.score_threshold = 0.2
        self.noise = 0.0
        self.num_frames = 100
        self.warmup_frames = 1
        self.range = "20,20"
        self.note = ""
        self.aggregation = ""
        self.task = "occupancy"
        self.all = False
        self.show_bev = False
        self.protocol_result = False
        self.data_only = False
        self.left_hand = False

class EgoRuntime:
    def __init__(self):
        self.model = None
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.opt = Opts()
        self.hypes = None
        self.full_dataset = None
        self.data_loader = None
        self.subset_dataset = None

    def load_model(self, model_dir):
        if self.model is not None:
            return
        
        model_dir = os.path.join(current_dir, model_dir)
        self.opt.model_dir = model_dir
        config_path = os.path.join(model_dir, "config.yaml")
        self.hypes = yaml_utils.load_yaml(config_path, self.opt)
        
        self.hypes = update_dict(self.hypes, {"score_threshold": self.opt.score_threshold})
        self.hypes["validate_dir"] = self.hypes["test_dir"]
        
        print("Creating Model")
        self.model = train_utils.create_model(self.hypes)
        
        print("Loading Model from checkpoint")
        resume_epoch, self.model = train_utils.load_saved_model(model_dir, self.model)
        
        if torch.cuda.is_available():
            self.model.cuda()
        self.model.eval()
        
        print("Dataset Building")
        self.full_dataset = build_dataset(self.hypes, visualize=True, train=False)
        total_dataset_len = len(self.full_dataset)
        num_frames = self.opt.num_frames if self.opt.num_frames > 0 else total_dataset_len
        num_frames = min(num_frames, total_dataset_len)
        print(f"Total dataset: {total_dataset_len}, frames to run: {num_frames}, warmup: {self.opt.warmup_frames}")
        
        collate_fn = self.full_dataset.collate_batch_test
        if num_frames < total_dataset_len:
            self.subset_dataset = Subset(self.full_dataset, list(range(num_frames)))
        else:
            self.subset_dataset = self.full_dataset
            
        self.data_loader = DataLoader(
            self.subset_dataset,
            batch_size=1,
            num_workers=0,
            collate_fn=collate_fn,
            shuffle=False,
            pin_memory=False,
            drop_last=False,
        )

    def update_dataloader_frames(self):
        total_dataset_len = len(self.full_dataset)
        num_frames = self.opt.num_frames if self.opt.num_frames > 0 else total_dataset_len
        num_frames = min(num_frames, total_dataset_len)
        print(f"Total dataset: {total_dataset_len}, frames to run: {num_frames}, warmup: {self.opt.warmup_frames}")
        
        collate_fn = self.full_dataset.collate_batch_test
        if num_frames < total_dataset_len:
            self.subset_dataset = Subset(self.full_dataset, list(range(num_frames)))
        else:
            self.subset_dataset = self.full_dataset
            
        self.data_loader = DataLoader(
            self.subset_dataset,
            batch_size=1,
            num_workers=0,
            collate_fn=collate_fn,
            shuffle=False,
            pin_memory=False,
            drop_last=False,
        )

    async def run_benchmark(self, receive_func):
        self.update_dataloader_frames()
        print("Priming DataLoader and CUDA (1 batch)...")
        _prime_iter = iter(self.data_loader)
        _prime_batch = None
        try:
            _prime_batch = next(_prime_iter)
        except StopIteration:
            pass
        if _prime_batch is not None:
            _prime_batch = train_utils.to_device(_prime_batch, self.device)
            _prime_batch["ego"]["benchmarking"] = True
            with torch.no_grad():
                _ = self.model(_prime_batch["ego"])
                if torch.cuda.is_available():
                    torch.cuda.synchronize()
        
        collate_fn = self.full_dataset.collate_batch_test
        self.data_loader = DataLoader(
            self.subset_dataset,
            batch_size=1,
            num_workers=0,
            collate_fn=collate_fn,
            shuffle=False,
            pin_memory=False,
            drop_last=False,
        )
        print("Priming done.\n")
        
        frame_times = []
        pbar = tqdm(enumerate(self.data_loader))
        
        miou_metric = MeanIoU(
            list(range(1, 13)), 13,
            ['Building', 'Fence', 'Terrain', 'Pole', 'Road', 'SideWalk',
             'Vegetation', 'Vehicles', 'Wall', 'GuardRail', 'TrafficSign', 'Bridge'],
            True, 13, filter_minmax=False
        )
        miou_metric.reset()
        ave_ious = {
            'road_ave_iou': [],
            'vehicle_ave_iou': [],
            'other_ave_iou': []
        }
        
        total_baseline_num = 0
        total_actual_num = 0
        valid_comm_frames = 0
        
        for i, batch_data in pbar:
            if batch_data is None:
                continue
                
            # Wait for Collaborator payload asynchronously
            collab_payload = await receive_func()
            
            with torch.no_grad():
                batch_data = train_utils.to_device(batch_data, self.device)
                batch_data["ego"]["benchmarking"] = True
                
                is_warmup = (i < self.opt.warmup_frames)
                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    t_start = time.perf_counter()
                
                # inference_intermediate_fusion logic
                infer_result = self.model(batch_data["ego"], collab_payload=collab_payload)
                
                # post process logic
                post_processor = self.full_dataset.post_processor
                
                output_dict = {"ego": infer_result}
                pred_box_tensor, pred_score = post_processor.post_process(batch_data, output_dict)
                
                # Calculate metrics for occupancy
                if pred_box_tensor is not None and 'final_occ' in pred_box_tensor:
                    gt_box_tensor = post_processor.generate_gt(batch_data)
                    pred_occ = pred_box_tensor['final_occ'][0]
                    gt_occ = gt_box_tensor['sampled_label'][0]
                    occ_mask = gt_box_tensor['occ_mask'][0].flatten()
                    
                    miou_metric._after_step(pred_occ, gt_occ, occ_mask)
                    occshape = (100, 100, 8)
                    iou_road, iou_vehicle, iou_other = eval_occseg_result(pred_occ, gt_occ, occshape)
                    ave_ious["road_ave_iou"].append(iou_road)
                    ave_ious["vehicle_ave_iou"].append(iou_vehicle)
                    ave_ious["other_ave_iou"].append(iou_other)
                    
                    if "comm_stats" in infer_result:
                        total_baseline_num += infer_result["comm_stats"].get("baseline_num", 0)
                        total_actual_num += infer_result["comm_stats"].get("actual_num", 0)
                        valid_comm_frames += 1
                
                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    frame_times.append(time.perf_counter() - t_start)
                    
        # Summarize
        if len(frame_times) > 0:
            frame_arr = np.array(frame_times)
            mean_latency = frame_arr.mean() * 1000
        else:
            mean_latency = 0.0
            
        vehicle_ave_iou = statistics.mean(ave_ious["vehicle_ave_iou"]) if ave_ious["vehicle_ave_iou"] else 0.0
        road_ave_iou = statistics.mean(ave_ious["road_ave_iou"]) if ave_ious["road_ave_iou"] else 0.0
        other_ave_iou = statistics.mean(ave_ious["other_ave_iou"]) if ave_ious["other_ave_iou"] else 0.0
        
        miou, iou2, per_class_iou = miou_metric._after_epoch()
        
        output_dict = {
            "status": "success",
            "mean_latency_ms": mean_latency,
            "mIoU": float(miou),
            "vehicle_ave_iou": vehicle_ave_iou,
            "road_ave_iou": road_ave_iou,
            "other_ave_iou": other_ave_iou
        }
        
        if valid_comm_frames > 0:
            avg_baseline = total_baseline_num / valid_comm_frames
            avg_actual = total_actual_num / valid_comm_frames
            ratio = (avg_actual / avg_baseline * 100) if avg_baseline > 0 else 0.0
            output_dict["Baseline Gaussians"] = float(f"{avg_baseline:.1f}")
            output_dict["Actual Transmitted"] = float(f"{avg_actual:.1f}")
            output_dict["Ratio"] = f"{ratio:.2f}%"
            
        return output_dict

    def run_benchmark_sync(self, receive_func):
        self.update_dataloader_frames()
        print("Priming DataLoader and CUDA (1 batch)...")
        _prime_iter = iter(self.data_loader)
        _prime_batch = None
        try:
            _prime_batch = next(_prime_iter)
        except StopIteration:
            pass
        if _prime_batch is not None:
            _prime_batch = train_utils.to_device(_prime_batch, self.device)
            _prime_batch["ego"]["benchmarking"] = True
            with torch.no_grad():
                _ = self.model(_prime_batch["ego"])
                if torch.cuda.is_available():
                    torch.cuda.synchronize()
        
        collate_fn = self.full_dataset.collate_batch_test
        self.data_loader = DataLoader(
            self.subset_dataset,
            batch_size=1,
            num_workers=0,
            collate_fn=collate_fn,
            shuffle=False,
            pin_memory=False,
            drop_last=False,
        )
        print("Priming done.\n")
        
        frame_times = []
        pbar = tqdm(enumerate(self.data_loader))
        
        miou_metric = MeanIoU(
            list(range(1, 13)), 13,
            ['Building', 'Fence', 'Terrain', 'Pole', 'Road', 'SideWalk',
             'Vegetation', 'Vehicles', 'Wall', 'GuardRail', 'TrafficSign', 'Bridge'],
            True, 13, filter_minmax=False
        )
        miou_metric.reset()
        ave_ious = {
            'road_ave_iou': [],
            'vehicle_ave_iou': [],
            'other_ave_iou': []
        }
        
        total_baseline_num = 0
        total_actual_num = 0
        valid_comm_frames = 0
        
        for i, batch_data in pbar:
            if batch_data is None:
                continue
                
            # Wait for Collaborator payload
            collab_payload = receive_func()
            
            with torch.no_grad():
                batch_data = train_utils.to_device(batch_data, self.device)
                batch_data["ego"]["benchmarking"] = True
                
                is_warmup = (i < self.opt.warmup_frames)
                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    t_start = time.perf_counter()
                
                # inference_intermediate_fusion logic
                infer_result = self.model(batch_data["ego"], collab_payload=collab_payload)
                
                # post process logic
                post_processor = self.full_dataset.post_processor
                
                output_dict = {"ego": infer_result}
                pred_box_tensor, pred_score = post_processor.post_process(batch_data, output_dict)
                
                # Calculate metrics for occupancy
                if pred_box_tensor is not None and 'final_occ' in pred_box_tensor:
                    gt_box_tensor = post_processor.generate_gt(batch_data)
                    pred_occ = pred_box_tensor['final_occ'][0]
                    gt_occ = gt_box_tensor['sampled_label'][0]
                    occ_mask = gt_box_tensor['occ_mask'][0].flatten()
                    
                    miou_metric._after_step(pred_occ, gt_occ, occ_mask)
                    occshape = (100, 100, 8)
                    iou_road, iou_vehicle, iou_other = eval_occseg_result(pred_occ, gt_occ, occshape)
                    ave_ious["road_ave_iou"].append(iou_road)
                    ave_ious["vehicle_ave_iou"].append(iou_vehicle)
                    ave_ious["other_ave_iou"].append(iou_other)
                    
                    if "comm_stats" in infer_result:
                        total_baseline_num += infer_result["comm_stats"].get("baseline_num", 0)
                        total_actual_num += infer_result["comm_stats"].get("actual_num", 0)
                        valid_comm_frames += 1
                
                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    frame_times.append(time.perf_counter() - t_start)
                    
        # Summarize
        if len(frame_times) > 0:
            frame_arr = np.array(frame_times)
            mean_latency = frame_arr.mean() * 1000
        else:
            mean_latency = 0.0
            
        vehicle_ave_iou = statistics.mean(ave_ious["vehicle_ave_iou"]) if ave_ious["vehicle_ave_iou"] else 0.0
        road_ave_iou = statistics.mean(ave_ious["road_ave_iou"]) if ave_ious["road_ave_iou"] else 0.0
        other_ave_iou = statistics.mean(ave_ious["other_ave_iou"]) if ave_ious["other_ave_iou"] else 0.0
        
        miou, iou2, per_class_iou = miou_metric._after_epoch()
        
        output_dict = {
            "status": "success",
            "mean_latency_ms": mean_latency,
            "mIoU": float(miou),
            "vehicle_ave_iou": vehicle_ave_iou,
            "road_ave_iou": road_ave_iou,
            "other_ave_iou": other_ave_iou
        }
        
        if valid_comm_frames > 0:
            avg_baseline = total_baseline_num / valid_comm_frames
            avg_actual = total_actual_num / valid_comm_frames
            ratio = (avg_actual / avg_baseline * 100) if avg_baseline > 0 else 0.0
            output_dict["Baseline Gaussians"] = float(f"{avg_baseline:.1f}")
            output_dict["Actual Transmitted"] = float(f"{avg_actual:.1f}")
            output_dict["Ratio"] = f"{ratio:.2f}%"
            
        return output_dict

model_runtime = EgoRuntime()
