import os
import sys
import torch
import numpy as np
import time
from torch.utils.data import DataLoader, Subset
from tqdm import tqdm

current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import opencood.hypes_yaml.yaml_utils as yaml_utils
from opencood.tools import train_utils
from opencood.data_utils.datasets import build_dataset
from opencood.utils.common_utils import update_dict

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

def tensor_to_numpy(obj):
    if isinstance(obj, torch.Tensor):
        return obj.cpu().numpy()
    elif isinstance(obj, dict):
        return {k: tensor_to_numpy(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [tensor_to_numpy(v) for v in obj]
    elif hasattr(obj, '_asdict'):
        return {k: tensor_to_numpy(v) for k, v in obj._asdict().items()}
    else:
        return obj

class CollaboratorRuntime:
    def __init__(self):
        self.model = None
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.opt = Opts()
        self.hypes = None
        self.full_dataset = None
        self.data_loader = None
        self.subset_dataset = None

    def _ensure_cuda_context(self):
        if self.device.type != "cuda":
            return
        torch.cuda.set_device(self.device)
        # Force lazy CUDA context creation in the serving process before custom ops run.
        _ = torch.empty(1, device=self.device)
        torch.cuda.synchronize()

    def load_model(self, model_dir):
        if self.model is not None:
            return

        # Always refresh device based on CURRENT CUDA availability.
        # __init__ sets self.device at import time (module-level instantiation);
        # in uvicorn subprocesses CUDA may not yet be initialized / visible at
        # import time, but it IS available by the time lifespan triggers
        # load_model. Without this refresh, self.device could remain "cpu"
        # while model.cuda() places params on GPU, so to_device() moves batch
        # data to CPU, and custom CUDA ops crash with:
        #   RuntimeError: t == DeviceType::CUDA INTERNAL ASSERT FAILED
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu"
        )

        # Apply NUM_FRAMES env override early so that the initial dataloader
        # (built below) already uses the requested frame count, instead of
        # the 100-frame default (which is then rebuilt inside
        # update_dataloader_frames anyway). This shortens startup time and
        # makes the env var fully honoured at every entry point.
        nf_env = os.getenv("NUM_FRAMES")
        if nf_env is not None:
            self.opt.num_frames = int(nf_env)

        self._ensure_cuda_context()
        
        # 绝对路径
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
        env_num_frames = os.getenv("NUM_FRAMES")
        if env_num_frames is not None:
            self.opt.num_frames = int(env_num_frames)
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

    async def run_benchmark(self, send_func):
        # Re-check CUDA availability right before execution (same rationale as
        # in load_model): env / runtime state may have shifted between any
        # two calls. update_dataloader_frames below relies on opt.num_frames,
        # which NUM_FRAMES env handling inside that function already applies.
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu"
        )
        self._ensure_cuda_context()
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
        
        for i, batch_data in pbar:
            if batch_data is None:
                continue
            
            with torch.no_grad():
                batch_data = train_utils.to_device(batch_data, self.device)
                batch_data["ego"]["benchmarking"] = True
                
                is_warmup = (i < self.opt.warmup_frames)
                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    t_start = time.perf_counter()
                
                # inference logic for collaborator
                infer_result = self.model(batch_data["ego"])

                # Mirror run_benchmark_sync: convert CUDA Tensors → numpy BEFORE
                # handing off to the NATS wrapper.  Otherwise the NATS layer's
                # _encode_control_payload calls json.dumps(payload) on raw
                # Tensors and raises: "Object of type Tensor is not JSON
                # serializable".
                payload = {
                    "frame_id": i,
                    "collaborator_gaussian": tensor_to_numpy(infer_result['collaborator_gaussian']),
                    "collaborator_GsSCE": tensor_to_numpy(infer_result.get('collaborator_GsSCE', None)),
                    "record_len": tensor_to_numpy(batch_data['ego']['record_len']),
                    "comm_stats": infer_result.get("comm_stats"),
                }

                # Send to ego agent asynchronously
                await send_func(payload)
                
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
            
        return {
            "status": "success",
            "mean_latency_ms": mean_latency,
            "message": "Collaborator finished sending all frames."
        }

    def run_benchmark_sync(self, send_func):
        # Mirror the same device refresh used in load_model / run_benchmark.
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu"
        )
        self._ensure_cuda_context()
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

        for i, batch_data in pbar:
            if batch_data is None:
                continue

            with torch.no_grad():
                batch_data = train_utils.to_device(batch_data, self.device)
                batch_data["ego"]["benchmarking"] = True

                is_warmup = (i < self.opt.warmup_frames)
                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    t_start = time.perf_counter()

                infer_result = self.model(batch_data["ego"])

                # 把模型前向产出的 comm_stats 连同 payload 一起传给 send_func，
                # 这样发送端（例如 local_test.py 的 mock_send）可以在真正"发送"的位置
                # 基于真实 payload 本身来做通信量统计，口径最准确、无歧义。
                payload = {
                    "frame_id": i,
                    "collaborator_gaussian": tensor_to_numpy(infer_result['collaborator_gaussian']),
                    "collaborator_GsSCE": tensor_to_numpy(infer_result.get('collaborator_GsSCE', None)),
                    "record_len": tensor_to_numpy(batch_data['ego']['record_len']),
                    "comm_stats": infer_result.get("comm_stats"),  # 发送端直接读此 dict 做过滤后计数
                }

                send_func(payload)

                if not is_warmup:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    frame_times.append(time.perf_counter() - t_start)

        # 注意：通信量的汇总统计在 send_func 调用点（mock_send 闭包内）完成并返回，
        # model_runtime 这里只保留发送帧数与状态，避免两处重复统计造成口径不一致。
        return {"status": "success", "sent_frames": len(frame_times)}

model_runtime = CollaboratorRuntime()
