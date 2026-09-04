# -*- coding: utf-8 -*-
"""
Benchmark gumbel_topk_bandwidth_filter latency.
"""
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import torch
import numpy as np
from opencood.models.sce_models.bw_gumbel import gumbel_topk_bandwidth_filter

N = 12800
TARGET_RATIO = 0.3
WARMUP = 50
ITERS = 500
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# --- Generate test data ---
agent_sce = torch.rand(N, device=DEVICE)
valid_mask = torch.rand(N, device=DEVICE) > 0.3  # ~70% valid

# --- Warmup ---
for _ in range(WARMUP):
    _ = gumbel_topk_bandwidth_filter(agent_sce, valid_mask, TARGET_RATIO)
if DEVICE.type == "cuda":
    torch.cuda.synchronize()

# --- Timed iterations ---
times = []
for _ in range(ITERS):
    if DEVICE.type == "cuda":
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    _ = gumbel_topk_bandwidth_filter(agent_sce, valid_mask, TARGET_RATIO)
    if DEVICE.type == "cuda":
        torch.cuda.synchronize()
    times.append(time.perf_counter() - t0)

arr = np.array(times)
print(f"Device:       {DEVICE}")
print(f"Input size:   {N}")
print(f"Target ratio: {TARGET_RATIO}")
print(f"Iterations:   {ITERS} (warmup {WARMUP} excluded)")
print()
print(f"Mean:    {arr.mean()*1000:.4f} ms")
print(f"Std:     {arr.std()*1000:.4f} ms")
print(f"Min:     {arr.min()*1000:.4f} ms")
print(f"Max:     {arr.max()*1000:.4f} ms")
print(f"Median:  {np.median(arr)*1000:.4f} ms")