# -*- coding: utf-8 -*-
# Author: Yifan Lu <yifan_lu@sjtu.edu.cn>
# License: TDG-Attribution-NonCommercial-NoDistrib

# Modifications by Xiangbo Gao <xiangbogaobarry@gmail.com>
# New License for modifications: MIT License

import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import random
import torch
import numpy as np

# seeding
random.seed(3)
torch.manual_seed(3)
torch.cuda.manual_seed(3)
torch.cuda.manual_seed_all(3)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

# from fvcore.nn import FlopCountAnalysis
# from fvcore.nn import flop_count_table

import argparse
import os
import statistics
import sys
from datetime import datetime

from torch.utils.data import DataLoader, Subset
from tensorboardX import SummaryWriter

# Add project root to sys.path to ensure local opencood is used
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import opencood.hypes_yaml.yaml_utils as yaml_utils
from opencood.tools import train_utils
from opencood.data_utils.datasets import build_dataset
from opencood.misc.checkpoint_util import refine_load_from_sd

from icecream import ic

def train_parser():
    parser = argparse.ArgumentParser(description="synthetic data generation")
    
    parser.add_argument("--hypes_yaml", "-y", default= None, type=str, required=False, help="data generation yaml file needed ")
    parser.add_argument("--model_dir", default="EXP_7CAVS_FULL/co3sop/full_12800/collab_0.4_STE", help="Continued training path")
    
    parser.add_argument("--fusion_method", "-f", default="intermediate", help="passed to inference.")
    parser.add_argument("--flop_count", action="store_true")
    opt = parser.parse_args()
    return opt

# --------------------------------------------------------------------------------
# --------------------------------------------------------------------------------
class Tee(object):
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def isatty(self):
        return self.terminal.isatty()

def main():
    opt = train_parser()
    hypes = yaml_utils.load_yaml(opt.hypes_yaml, opt)

    print("Dataset Building")
    opencood_train_dataset = build_dataset(hypes, visualize=False, train=True)
    opencood_validate_dataset = build_dataset(hypes, visualize=False, train=False)

    train_loader = DataLoader(
        opencood_train_dataset,
        batch_size=hypes["train_params"]["batch_size"],
        # num_workers=0,
        num_workers=24,
        collate_fn=opencood_train_dataset.collate_batch_train,
        shuffle=True,
        pin_memory=True,
        drop_last=True,
        prefetch_factor=2,
        # prefetch_factor=None,
    )
    val_loader = DataLoader(
        opencood_validate_dataset,
        batch_size=hypes["train_params"]["batch_size"],
        # num_workers=0,
        num_workers=24,
        collate_fn=opencood_train_dataset.collate_batch_train,
        shuffle=True,
        pin_memory=True,
        drop_last=True,
        prefetch_factor=2,
        # prefetch_factor=None,
    )

    print("Creating Model")
    model = train_utils.create_model(hypes)

    train_cee = hypes["model"]["args"].get("m4", {}).get("encoder_args", {}).get("train_cee", False)
    if train_cee:
        print("Stage 3")
        for param in model.parameters():
            param.requires_grad = False
            
        from opencood.models.sce_models.cee import CEE
        cee_params_count = 0
        for name, module in model.named_modules():
            if isinstance(module, CEE):
                for param_name, param in module.named_parameters():
                    param.requires_grad = True
                    print(f"  - Keeping parameter active: {name}.{param_name}")
                    cee_params_count += 1
                    
        if cee_params_count == 0:
            raise ValueError("train_cee is True, but no trainable CEE parameters were found! Check if CEE update_mode is 'gating' and CEE is correctly instantiated.")

    comm_stats_accum = {"baseline_num": 0, "actual_num": 0, "count": 0}
    if hasattr(model, "init_weights") and callable(model.init_weights):
        model.init_weights()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    lowest_val_loss = 1e5
    lowest_val_epoch = -1

    criterion = train_utils.create_loss(hypes)
    optimizer = train_utils.setup_optimizer(hypes, model)

    if opt.model_dir:
        saved_path = opt.model_dir
        init_epoch, model = train_utils.load_saved_model(saved_path, model)
        lowest_val_epoch = init_epoch
        scheduler = train_utils.setup_lr_schedular(
            hypes, optimizer, init_epoch=init_epoch, n_iter_per_epoch=len(train_loader)
        )
        print(f"resume from {init_epoch} epoch.")
    else:
        init_epoch = 0
        saved_path = train_utils.setup_train(hypes)
        scheduler = train_utils.setup_lr_schedular(
            hypes, optimizer, n_iter_per_epoch=len(train_loader)
        )

    # --------------------------------------------------------------------------------
    # --------------------------------------------------------------------------------
    log_filename = os.path.join(saved_path, datetime.now().strftime("%Y%m%d-%H%M%S_train.log"))
    sys.stdout = Tee(log_filename)
    sys.stderr = Tee(log_filename)
    print(f"Log file created at: {log_filename}")
    # --------------------------------------------------------------------------------

    # Load pretrained model
    for m_key in ["m1", "m2", "m3", "m4"]:
        load_path = hypes["model"]["args"].get(m_key, {}).get("load_from", None)
        if load_path is not None:
            if not train_cee:
                print(f"Loading pretrained weights for {m_key} from {load_path}")
                ckpt = torch.load(load_path, map_location="cpu")
                state_dict = ckpt["state_dict"]

                backbone_attr = f"backbone_{m_key}"
            
                try:
                    load_result = getattr(model, backbone_attr).load_state_dict(state_dict, strict=False)
                except Exception as e:
                    print(f"Refining state_dict for {m_key} due to error: {e}")
                    refined_state_dict = refine_load_from_sd(state_dict)
                    load_result = getattr(model, backbone_attr).load_state_dict(refined_state_dict, strict=False)
                print(f"{m_key} load result:", load_result)
                print(f"Pretrained Backbone {m_key} Loaded.")
            else:
                print("SKIP BB")

    if torch.cuda.is_available():
        model.to(device)

    num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Total trainable parameters: {num_params}")

    writer = SummaryWriter(saved_path)

    print("Training start")
    epoches = hypes["train_params"]["epoches"]
    grad_accumulation = int(hypes["train_params"].get("grad_accumulation", 1))
    grad_accumulation = max(1, grad_accumulation)
    grad_max_norm = hypes["train_params"].get("grad_max_norm", float("inf"))
    global_iter = 0

    supervise_single_flag = (
        False if not hasattr(opencood_train_dataset, "supervise_single")
        else opencood_train_dataset.supervise_single
    )

    for epoch in [0] if opt.flop_count else range(init_epoch, max(epoches, init_epoch)):
        for param_group in optimizer.param_groups:
            print("learning rate %f" % param_group["lr"])

        model.train()
        try:
            model.model_train_init()
        except Exception:
            print("No model_train_init function")

        total_flops_sum = 0
        grad_flops_sum = 0

        optimizer.zero_grad(set_to_none=True)

        for i, batch_data in enumerate(train_loader):
            if batch_data is None:
                continue
            if "occ_label" not in batch_data["ego"]["label_dict"] and batch_data["ego"]["object_bbx_mask"].sum() == 0:
                continue

            batch_data = train_utils.to_device(batch_data, device)
            batch_data["ego"]["epoch"] = epoch

            if opt.flop_count:
                flops = FlopCountAnalysis(model, batch_data["ego"])
                flops_counter = flops.by_module()
                for key, value in flops_counter.items():
                    if getattr(model, key, None) is not None:
                        requires_grad = False
                        for param in getattr(model, key).parameters():
                            if param.requires_grad:
                                requires_grad = True
                                break
                        if requires_grad:
                            grad_flops_sum += value
                        total_flops_sum += value

                print(i, "/", len(train_loader), "Grad FLOPS: ", grad_flops_sum)
                print(i, "/", len(train_loader), "Total FLOPS: ", total_flops_sum)
                del flops
                torch.cuda.empty_cache()
                continue
            output_dict = model(batch_data["ego"])

            if "comm_stats" in output_dict:
                comm_stats_accum["baseline_num"] += output_dict["comm_stats"]["baseline_num"]
                comm_stats_accum["actual_num"] += output_dict["comm_stats"]["actual_num"]
                comm_stats_accum["count"] += 1
                
                if comm_stats_accum["count"] == 800:
                    avg_baseline = comm_stats_accum["baseline_num"] / 800
                    avg_actual = comm_stats_accum["actual_num"] / 800
                    ratio = (avg_actual / avg_baseline * 100) if avg_baseline > 0 else 0
                    
                    mb_per_gaussian = 96 / (1024 * 1024)
                    base_mb = avg_baseline * mb_per_gaussian
                    act_mb = avg_actual * mb_per_gaussian
                    
                    msg = (f"[Communication Stats (Train)] Epoch {epoch} Batch {i+1}: "
                           f"Baseline Gaussians: {avg_baseline:.1f} ({base_mb:.4f} MB) | "
                           f"Actual Transmitted: {avg_actual:.1f} ({act_mb:.4f} MB) | "
                           f"Ratio: {ratio:.2f}%")
                    print(msg)
                    if writer is not None:
                        writer.add_text("Communication_Stats/Train", msg, epoch * len(train_loader) + i)
                    comm_stats_accum = {"baseline_num": 0, "actual_num": 0, "count": 0}

            final_loss = criterion(output_dict, batch_data["ego"]["label_dict"], epoch)
            criterion.logging(epoch, i, len(train_loader), writer)

            if supervise_single_flag:
                final_loss = final_loss + criterion(
                    output_dict,
                    batch_data["ego"]["label_dict_single"],
                    suffix="_single",
                ) * hypes["train_params"].get("single_weight", 1)
                criterion.logging(epoch, i, len(train_loader), writer, suffix="_single")

            loss = final_loss / grad_accumulation
            loss.backward()

            if (global_iter + 1) % grad_accumulation == 0:
                grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), grad_max_norm)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)

                lr = max([p["lr"] for p in optimizer.param_groups])
                print(
                    f"[epoch {epoch}][{i + 1}/{len(train_loader)}] || "
                    f"lr: {lr:.7f} || grad_norm: {grad_norm:.4f}"
                )

            global_iter += 1

            if hasattr(scheduler, "step_update"):
                scheduler.step_update(global_iter)

        if not opt.flop_count and (global_iter % grad_accumulation != 0):
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), grad_max_norm)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)

            lr = max([p["lr"] for p in optimizer.param_groups])
            print(f"[epoch {epoch}][tail step] || lr: {lr:.7f} || grad_norm: {grad_norm:.4f}")

        if opt.flop_count:
            print("Total FLOPS: ", total_flops_sum)
            return

        if epoch % hypes["train_params"]["save_freq"] == 0:
            torch.save(model.state_dict(), os.path.join(saved_path, "net_epoch%d.pth" % (epoch + 1)))

        if epoch % hypes["train_params"]["eval_freq"] == 0:
            valid_ave_loss = []
            val_comm_stats_accum = {"baseline_num": 0, "actual_num": 0, "count": 0}

            model.eval()
            with torch.no_grad():
                for i, batch_data in enumerate(val_loader):
                    if batch_data is None:
                        continue

                    batch_data = train_utils.to_device(batch_data, device)
                    batch_data["ego"]["epoch"] = epoch
                    output_dict = model(batch_data["ego"])

                    if "comm_stats" in output_dict:
                        val_comm_stats_accum["baseline_num"] += output_dict["comm_stats"]["baseline_num"]
                        val_comm_stats_accum["actual_num"] += output_dict["comm_stats"]["actual_num"]
                        val_comm_stats_accum["count"] += 1

                    final_loss = criterion(output_dict, batch_data["ego"]["label_dict"])
                    criterion.logging(epoch, i, len(val_loader), writer)
                    valid_ave_loss.append(final_loss.item())

            valid_ave_loss = statistics.mean(valid_ave_loss)
            print("At epoch %d, the validation loss is %f" % (epoch, valid_ave_loss))
            writer.add_scalar("Validate_Loss", valid_ave_loss, epoch)

            if val_comm_stats_accum["count"] > 0:
                avg_baseline = val_comm_stats_accum["baseline_num"] / val_comm_stats_accum["count"]
                avg_actual = val_comm_stats_accum["actual_num"] / val_comm_stats_accum["count"]
                ratio = (avg_actual / avg_baseline * 100) if avg_baseline > 0 else 0
                mb_per_gaussian = 96 / (1024 * 1024)
                base_mb = avg_baseline * mb_per_gaussian
                act_mb = avg_actual * mb_per_gaussian
                
                msg = (f"[Communication Stats (Val)] Epoch {epoch}: "
                       f"Baseline Gaussians: {avg_baseline:.1f} ({base_mb:.4f} MB) | "
                       f"Actual Transmitted: {avg_actual:.1f} ({act_mb:.4f} MB) | "
                       f"Ratio: {ratio:.2f}%")
                print(msg)
                if writer is not None:
                    writer.add_text("Communication_Stats/Val", msg, epoch)

            if valid_ave_loss < lowest_val_loss:
                lowest_val_loss = valid_ave_loss
                torch.save(
                    model.state_dict(),
                    os.path.join(saved_path, "net_epoch_bestval_at%d.pth" % (epoch + 1)),
                )
                if lowest_val_epoch != -1 and os.path.exists(
                        os.path.join(saved_path, "net_epoch_bestval_at%d.pth" % (lowest_val_epoch))
                ):
                    os.remove(os.path.join(saved_path, "net_epoch_bestval_at%d.pth" % (lowest_val_epoch)))
                lowest_val_epoch = epoch + 1

        if not hasattr(scheduler, "step_update"):
            scheduler.step()

        opencood_train_dataset.reinitialize()

    print("Training Finished, checkpoints saved to %s" % saved_path)

    if opt.flop_count:
        print("Total FLOPS: ", total_flops_sum)
        return

if __name__ == "__main__":
    main()
