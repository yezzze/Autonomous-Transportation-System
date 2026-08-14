import torch
import numpy as np
from sklearn.cluster import KMeans

def _kmeans_1d_pytorch(x: torch.Tensor, k: int) -> torch.Tensor:
    """
    Pure PyTorch 1D K-Means for inference. Runs entirely on GPU,
    avoiding CPU-GPU transfers and sklearn overhead.
    Uses quantile-based init + Lloyd iterations.
    """
    B, N, _ = x.shape
    device = x.device
    x_flat = x.squeeze(-1)  # [B, N]

    x_sorted, sort_idx = torch.sort(x_flat, dim=-1)

    # Quantile-based initialization (works well for 1D with small k)
    quantiles = torch.linspace(0, 1, k + 2, device=device)[1:-1]  # [k]
    init_indices = (quantiles * (N - 1)).long().unsqueeze(0).expand(B, -1)
    centroids = x_sorted.gather(1, init_indices).clone()  # [B, k]

    # Lloyd iterations - 1D converges in 2-5 iters
    for _ in range(5):
        dists = (x_sorted.unsqueeze(-1) - centroids.unsqueeze(1)) ** 2  # [B, N, k]
        labels = dists.argmin(dim=-1)  # [B, N]

        new_centroids = centroids.clone()
        for j in range(k):
            mask = (labels == j)
            counts = mask.sum(dim=-1).clamp(min=1).float()
            sums = torch.where(mask, x_sorted, torch.zeros_like(x_sorted)).sum(dim=-1)
            new_centroids[:, j] = sums / counts

        if torch.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids

    # Map back to original order
    labels_unsorted = torch.zeros_like(labels)
    labels_unsorted.scatter_(1, sort_idx, labels)

    # Ensure label 0 = smallest centroid
    order = centroids.argsort(dim=-1)  # [B, k]
    mapping = torch.zeros(B, k, dtype=torch.long, device=device)
    for j in range(k):
        mapping.scatter_(1, order[:, j:j + 1], torch.full((B, 1), j, device=device, dtype=torch.long))

    return mapping.gather(1, labels_unsorted)

def kmeans_1d_original(x: torch.Tensor, k: int) -> torch.Tensor:
    B, N, _ = x.shape
    device = x.device
    
    mapped_labels = torch.zeros((B, N), dtype=torch.long, device=device)
    
    x_np = x.detach().cpu().numpy()
    
    for b in range(B):
        batch_data = x_np[b]
        
        kmeans = KMeans(n_clusters=k, n_init="auto", random_state=42)
        
        labels_np = kmeans.fit_predict(batch_data)
        centers_np = kmeans.cluster_centers_.squeeze(-1) # [k]
        
        sort_indices = np.argsort(centers_np)
        
        mapping = np.zeros(k, dtype=np.int64)
        for new_label, old_label in enumerate(sort_indices):
            mapping[old_label] = new_label
            
        mapped_labels_np = mapping[labels_np]
        mapped_labels[b] = torch.from_numpy(mapped_labels_np).to(device)
        
    return mapped_labels

class KMeans1DSTE(torch.autograd.Function):
    @staticmethod
    def forward(ctx, GsSCE, k, use_sklearn=True):
        if use_sklearn:
            labels = kmeans_1d_original(GsSCE, k)
        else:
            labels = _kmeans_1d_pytorch(GsSCE, k)
        return labels.float()

    @staticmethod
    def backward(ctx, grad_output):
        # STE: pass gradient through unchanged
        grad_GsSCE = grad_output.unsqueeze(-1).clone()
        return grad_GsSCE, None, None

def kmeans_1d(x: torch.Tensor, k: int) -> torch.Tensor:
    """
    1D K-Means clustering with automatic train/inference routing.
    - Training (requires_grad): uses sklearn via STE for stable gradient flow
    - Inference (no_grad): uses pure PyTorch for zero CPU-GPU transfer overhead
    """
    if torch.is_grad_enabled() and x.requires_grad:
        return KMeans1DSTE.apply(x, k, True)
    else:
        return _kmeans_1d_pytorch(x, k)
