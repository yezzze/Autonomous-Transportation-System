"""
云端语义通信服务器
功能：接收车端发送的token，使用大模型生成事故摘要和处置建议
"""

import os
from pathlib import Path
# 禁用 Hugging Face 的部分远程特性，优先使用本地缓存/本地模型
os.environ["HF_HUB_DISABLE_XET"] = "1"
# ✅ 关键：关闭网络访问，只使用本地缓存和本地模型文件
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

QWEN_LOCAL_PATH = str(
    Path(__file__).resolve().parents[1]
    / "weights"
    / "qwen"
    / "Qwen2.5-VL-3B-Instruct"
)

import json
import base64
import pickle
import gzip
import traceback
import numpy as np
import torch
from flask import Flask, request, jsonify
from transformers import AutoProcessor, AutoModelForVision2Seq
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings
from typing import List, Dict, Any, Optional


class SemanticCommServer:
    """语义通信服务器 - 云端"""
    
    def __init__(self, 
                 model_path: str = QWEN_LOCAL_PATH,
                 kb_path_1: str = "kb_accident_law",
                 kb_path_2: str = "kb_accident_law_1",
                 embedder_path: str = "shibing624/text2vec-base-chinese",
                 port: int = 5001):
        """
        初始化服务器
        
        Args:
            model_path: 大模型路径
            kb_path_1: 知识库1路径
            kb_path_2: 知识库2路径
            embedder_path: 文本嵌入模型路径
            port: 服务器端口
        """
        self.port = port
        self.device = self._detect_device()
        
        # 加载大模型
        print("🔄 正在加载大模型...")
        self.processor = AutoProcessor.from_pretrained(
            model_path,
            trust_remote_code=True,
            local_files_only=True
        )
        
        if self.device == "cuda":
            self.model = AutoModelForVision2Seq.from_pretrained(
                model_path,
                device_map="auto",
                torch_dtype=torch.float16,
                trust_remote_code=True,
                local_files_only=True
            )
        else:
            self.model = AutoModelForVision2Seq.from_pretrained(
                model_path,
                device_map="cpu",
                torch_dtype=torch.float16 if self.device == "mps" else torch.float32,
                trust_remote_code=True,
                local_files_only=True
            )
            if self.device == "mps":
                self.model = self.model.to("mps")
        
        print(f"✅ 大模型加载完成，设备: {self.device}")
        
        # 加载知识库
        print("🔄 正在加载知识库...")
        # 使用本地缓存 / 本地模型，不再访问 HuggingFace 远程仓库
        # embedder_path 默认是模型名称（如 shibing624/text2vec-base-chinese），
        # 在 HF_HUB_OFFLINE=1 下会直接从 ~/.cache/huggingface 中加载已缓存的权重。
        self.embedder = HuggingFaceEmbeddings(
            model_name=embedder_path,
            model_kwargs={
                "device": "cpu",            # 在 CPU 上运行向量模型
                "local_files_only": True    # 只使用本地文件，禁止联网下载
            }
        )
        
        self.vectorstore1 = FAISS.load_local(
            kb_path_1, 
            embeddings=self.embedder, 
            allow_dangerous_deserialization=True
        )
        self.vectorstore2 = FAISS.load_local(
            kb_path_2, 
            embeddings=self.embedder, 
            allow_dangerous_deserialization=True
        )
        print("✅ 知识库加载完成")
        
        # 创建Flask应用
        self.app = Flask(__name__)
        self._setup_routes()
    
    def _detect_device(self):
        """检测可用设备"""
        if torch.backends.mps.is_available():
            return "mps"
        elif torch.cuda.is_available():
            return "cuda"
        else:
            return "cpu"
    
    def _setup_routes(self):
        """设置路由"""
        @self.app.route('/api/health', methods=['GET'])
        def health():
            """健康检查接口"""
            return jsonify({
                "status": "ok",
                "device": self.device
            })
        
        @self.app.route('/api/process_tokens', methods=['POST'])
        def process_tokens():
            """处理token并生成建议"""
            import time
            start_time = time.time()
            try:
                print("\n" + "="*60)
                print("📥 收到token处理请求")
                print("="*60)
                
                data = request.json
                if not data or "tokens" not in data:
                    return jsonify({"error": "缺少tokens字段"}), 400
                
                # 解析tokens（可能是JSON字符串）
                print("🔄 步骤1/4: 解析tokens...")
                tokens_str = data["tokens"]
                if isinstance(tokens_str, str):
                    tokens = json.loads(tokens_str)
                else:
                    tokens = tokens_str
                print(f"✅ Token解析完成，包含字段: {list(tokens.keys())}")
                
                # 生成事故摘要
                print("🔄 步骤2/4: 生成事故摘要（这可能需要较长时间）...")
                summary_start = time.time()
                summary, _, _, _ = self.generate_summary_from_tokens(tokens)
                summary_time = time.time() - summary_start
                print(f"✅ 摘要生成完成，耗时: {summary_time:.2f}秒")
                print(f"📋 摘要内容: {summary[:100]}...")
                
                # 检索相关法规
                print("🔄 步骤3/4: 检索相关法规...")
                regulations = self.retrieve_regulations(summary)
                print(f"✅ 检索完成，找到 {len(regulations)} 条法规")
                
                # 生成处置建议
                print("🔄 步骤4/4: 生成处置建议...")
                advice_start = time.time()
                advice = self.generate_advice(summary, regulations)
                advice_time = time.time() - advice_start
                print(f"✅ 建议生成完成，耗时: {advice_time:.2f}秒")
                
                total_time = time.time() - start_time
                print(f"\n✅ 处理完成，总耗时: {total_time:.2f}秒")
                print("="*60 + "\n")
                
                return jsonify({
                    "summary": summary,
                    "advice": advice,
                    "regulations_count": len(regulations)
                })
                
            except Exception as e:
                error_msg = f"处理失败: {str(e)}\n{traceback.format_exc()}"
                print(f"\n❌ 错误详情:\n{error_msg}")
                print("="*60 + "\n")
                return jsonify({"error": error_msg}), 500
    
    def _decode_input_ids(self, tokens: Dict[str, Any]):
        """解码input_ids和attention_mask"""
        if "input_ids_b64" not in tokens:
            return None, None
        
        # 解码input_ids
        input_bytes = base64.b64decode(tokens["input_ids_b64"])
        input_np = pickle.loads(input_bytes)
        shape = tokens.get("input_ids_shape")
        if shape:
            input_np = input_np.reshape(shape)
        input_ids = torch.tensor(input_np, dtype=torch.long).to(self.device)
        
        # 解码attention_mask
        attention_mask = None
        if tokens.get("attention_mask_b64"):
            mask_bytes = base64.b64decode(tokens["attention_mask_b64"])
            mask_np = pickle.loads(mask_bytes)
            mask_shape = tokens.get("attention_mask_shape")
            if mask_shape:
                mask_np = mask_np.reshape(mask_shape)
            attention_mask = torch.tensor(mask_np, dtype=torch.long).to(self.device)
        
        return input_ids, attention_mask
    
    def _decode_vision_features(self, tokens: Dict[str, Any]):
        """
        解码vision_features（Vision Encoder输出的特征）
        支持语义压缩后的特征恢复
        """
        if tokens.get("compressed", False) or "vision_features_b64" in tokens:
            if "vision_features_b64" not in tokens:
                raise ValueError("Token标记为压缩格式，但缺少vision_features_b64字段")
            
            # Base64解码
            compressed = base64.b64decode(tokens["vision_features_b64"])
            # gzip解压
            vision_bytes = gzip.decompress(compressed)
            # pickle反序列化
            vision_features = pickle.loads(vision_bytes)
        elif "vision_features" in tokens:
            vision_features = np.array(tokens["vision_features"])
            shape = tokens.get("vision_features_shape")
            if shape:
                vision_features = vision_features.reshape(shape)
        else:
            raise ValueError("Token中既没有vision_features_b64也没有vision_features字段")
        
        # 转换为PyTorch张量
        dtype_str = tokens.get("vision_features_dtype", "float32")
        dtype_map = {"float32": torch.float32, "float16": torch.float16}
        dtype = dtype_map.get(dtype_str, torch.float32)
        
        vision_tensor = torch.tensor(vision_features, dtype=dtype).to(self.device)
        
        # 检查是否使用了语义压缩
        use_semantic_compression = tokens.get("use_semantic_compression", False)
        compression_info = tokens.get("compression_info")
        
        if use_semantic_compression and compression_info is not None:
            # 需要恢复压缩后的特征
            print("  └─ 检测到语义压缩，正在恢复特征...")
            
            # 检查是否按图片压缩
            per_image = compression_info.get('per_image', False)
            
            if per_image:
                # 按图片恢复
                from .semantic_compression_module import decompress_per_image
                
                # 将numpy数组转换为torch tensor
                compressed_features_list = [
                    torch.tensor(cf, dtype=vision_tensor.dtype).to(self.device)
                    for cf in compression_info['compressed_features_list']
                ]
                keep_indices_list = [
                    torch.tensor(ki, dtype=torch.long).to(self.device)
                    for ki in compression_info['keep_indices_list']
                ]
                original_shapes = compression_info['original_shapes']
                compression_ratio = compression_info.get('compression_ratio', 1.0)
                num_images = compression_info.get('num_images', len(compressed_features_list))
                
                print(f"    [COMPRESSION] 按图片压缩: {num_images} 张图片")
                print(f"    [COMPRESSION] 平均压缩率: {compression_ratio:.2%}")
                
                # 获取 image_grid_thw
                image_grid_thw = self._decode_image_grid(tokens, vision_tensor)
                
                # 恢复特征
                vision_tensor = decompress_per_image(
                    compressed_features_list=compressed_features_list,
                    keep_indices_list=keep_indices_list,
                    original_shapes=original_shapes,
                    image_grid_thw=image_grid_thw,
                    device=self.device
                )
                
                print(f"    [COMPRESSION] 恢复后形状: {vision_tensor.shape}")
            else:
                # 整体恢复（旧方式，兼容性）
                from .semantic_compression_module import decompress_features
                
                keep_indices = torch.tensor(
                    compression_info['keep_indices'],
                    dtype=torch.long
                ).to(self.device)
                original_shape = tuple(compression_info['original_shape'])
                compression_ratio = compression_info.get('compression_ratio', 1.0)
                
                print(f"    [COMPRESSION] 原始形状: {original_shape}")
                print(f"    [COMPRESSION] 压缩后形状: {vision_tensor.shape}")
                print(f"    [COMPRESSION] 压缩率: {compression_ratio:.2%}")
                print(f"    [COMPRESSION] 保留索引形状: {keep_indices.shape}")
                
                # 恢复特征
                vision_tensor = decompress_features(
                    compressed_features=vision_tensor,
                    keep_indices=keep_indices,
                    original_shape=original_shape,
                    device=self.device
                )
                
                print(f"    [COMPRESSION] 恢复后形状: {vision_tensor.shape}")
            
            print("  ✅ 特征恢复完成")
        else:
            print("  └─ 未使用语义压缩，使用原始特征")
        
        # 调试信息：打印接收到的视觉特征基本情况
        print(f"  [VISION DEBUG] 解码后的vision_features形状: {vision_tensor.shape}, dtype: {vision_tensor.dtype}, 设备: {vision_tensor.device}")
        return vision_tensor
    
    def _decode_pixel_values(self, tokens: Dict[str, Any]):
        """解码pixel_values（兼容旧格式）"""
        if tokens.get("compressed", False) or "pixel_values_b64" in tokens:
            if "pixel_values_b64" not in tokens:
                raise ValueError("Token标记为压缩格式，但缺少pixel_values_b64字段")
            
            # Base64解码
            compressed = base64.b64decode(tokens["pixel_values_b64"])
            # gzip解压
            pixel_bytes = gzip.decompress(compressed)
            # pickle反序列化
            pixel_values = pickle.loads(pixel_bytes)
        elif "pixel_values" in tokens:
            pixel_values = np.array(tokens["pixel_values"])
            shape = tokens.get("pixel_values_shape")
            if shape:
                pixel_values = pixel_values.reshape(shape)
        else:
            raise ValueError("Token中既没有pixel_values_b64也没有pixel_values字段")
        
        # 转换为PyTorch张量
        dtype_str = tokens.get("pixel_values_dtype", "float32")
        dtype_map = {"float32": torch.float32, "float16": torch.float16}
        dtype = dtype_map.get(dtype_str, torch.float32)
        
        return torch.tensor(pixel_values, dtype=dtype).to(self.device)
    
    def _decode_image_grid(self, tokens: Dict[str, Any], pixel_values: torch.Tensor):
        """解码image_grid_thw"""
        if "image_grid_thw" in tokens and tokens["image_grid_thw"] is not None:
            image_grid_thw = torch.tensor(tokens["image_grid_thw"], dtype=torch.int64).to(self.device)
        else:
            # 如果没有提供，根据batch_size生成默认值
            batch_size = pixel_values.shape[0]
            image_grid_thw = torch.tensor([[1, 1, 1]] * batch_size, dtype=torch.int64).to(self.device)
        
        return image_grid_thw
    
    def generate_summary_from_tokens(self, tokens: Dict[str, Any]):
        """
        基于token生成事故摘要
        
        Args:
            tokens: 包含vision_features或pixel_values, input_ids等的token字典
            
        Returns:
            summary: 事故摘要文本
            full_input_ids: 完整的input_ids（包含生成的token）
            full_attention_mask: 完整的attention_mask
            image_grid_thw: 图像网格信息
        """
        import time
        
        # 解码token
        print("  └─ 解码input_ids...")
        decode_start = time.time()
        input_ids, attention_mask = self._decode_input_ids(tokens)
        if input_ids is None:
            raise ValueError("缺少input_ids信息，无法生成摘要")
        print(f"  └─ input_ids形状: {input_ids.shape}")
        
        # 检查是否接收到视觉特征（新格式）还是pixel_values（旧格式）
        is_vision_features = tokens.get("is_vision_features", False) or "vision_features_b64" in tokens
        
        if is_vision_features:
            print("  └─ 检测到视觉特征格式（Vision Encoder输出）...")
            vision_features = self._decode_vision_features(tokens)
            print(f"  └─ 视觉特征形状(解码后): {vision_features.shape}, 设备: {vision_features.device}")
            
            # 解码image_grid_thw
            print("  └─ 解码image_grid_thw...")
            if "image_grid_thw" in tokens and tokens["image_grid_thw"] is not None:
                image_grid_thw = torch.tensor(tokens["image_grid_thw"], dtype=torch.int64).to(self.device)
            else:
                num_images = vision_features.shape[0]
                image_grid_thw = torch.tensor([[1, 1, 1]] * num_images, dtype=torch.int64).to(self.device)
            print(f"  └─ image_grid_thw张量: {image_grid_thw}, 形状: {image_grid_thw.shape}")
            
            decode_time = time.time() - decode_start
            print(f"  ✅ Token解码完成，耗时: {decode_time:.2f}秒")
            
            # 使用视觉特征直接生成（需要特殊处理）
            print("  └─ 准备生成参数（使用预计算的视觉特征）...")
            
            # 方法：创建一个dummy pixel_values，然后通过hook注入视觉特征
            # 或者直接修改模型的forward方法
            # 这里我们使用一个更直接的方法：通过模型的内部方法注入视觉特征
            
            # 准备生成参数
            gen_kwargs = {
                "input_ids": input_ids.clone(),
                "max_new_tokens": 256,
                "do_sample": False
            }
            if attention_mask is not None:
                gen_kwargs["attention_mask"] = attention_mask.clone()
            
            # 创建一个hook来注入视觉特征
            vision_features_list = [vision_features]  # 使用列表以便在hook中修改
            
            def vision_hook(module, input, output):
                # 在模型forward时注入视觉特征
                # 这个方法需要根据Qwen2.5-VL的实际实现来调整
                pass
            
            # 注册hook（如果模型支持）
            # 由于Qwen2.5-VL的模型结构可能不支持直接注入，我们使用另一种方法：
            # 创建一个包装器，在generate之前预处理输入
            
            # 临时方案：仍然需要pixel_values，但我们可以创建一个dummy的
            # 实际上，更好的方法是修改模型的forward方法
            # 但为了兼容性，我们暂时使用一个workaround：
            # 如果模型支持vision_features参数，直接传递；否则需要重新编码
            
            # 尝试直接传递vision_features（如果模型支持）
            try:
                gen_kwargs["vision_features"] = vision_features
                gen_kwargs["image_grid_thw"] = image_grid_thw
            except:
                # 如果不支持，需要fallback到pixel_values
                print("  ⚠️  模型不支持直接输入vision_features，需要重新编码...")
                # 这里我们需要pixel_values，但车端没有发送
                # 所以我们需要告诉用户这种情况
                raise ValueError("当前模型不支持直接使用vision_features，需要pixel_values")
            
        else:
            # 旧格式：使用pixel_values
            print("  └─ 解码pixel_values（旧格式）...")
            pixel_values = self._decode_pixel_values(tokens)
            print(f"  └─ pixel_values形状: {pixel_values.shape}, 设备: {pixel_values.device}")
            
            print("  └─ 解码image_grid_thw...")
            image_grid_thw = self._decode_image_grid(tokens, pixel_values)
            print(f"  └─ image_grid_thw: {image_grid_thw}")
            
            decode_time = time.time() - decode_start
            print(f"  ✅ Token解码完成，耗时: {decode_time:.2f}秒")
            
            # 准备生成参数
            print("  └─ 准备生成参数...")
            gen_kwargs = {
                "input_ids": input_ids.clone(),
                "pixel_values": pixel_values,
                "image_grid_thw": image_grid_thw,
                "max_new_tokens": 256,
                "do_sample": False
            }
            if attention_mask is not None:
                gen_kwargs["attention_mask"] = attention_mask.clone()
        
        # 生成摘要
        print("  └─ 开始模型推理（这可能需要较长时间，请耐心等待）...")
        gen_start = time.time()
        with torch.no_grad():
            # 如果使用vision_features，需要特殊处理
            if is_vision_features and "vision_features" in gen_kwargs:
                # 需要修改模型的forward方法来接受vision_features
                # 这里我们使用一个workaround：通过修改模型的内部状态
                # 实际上，Qwen2.5-VL可能不支持这个，所以我们需要创建一个包装器
                
                # 临时解决方案：创建一个自定义的generate方法
                # 但为了简化，我们先尝试直接调用，如果失败则fallback
                try:
                    output_ids = self._generate_with_vision_features(
                        input_ids=input_ids,
                        vision_features=vision_features,
                        image_grid_thw=image_grid_thw,
                        attention_mask=attention_mask,
                        max_new_tokens=256
                    )
                except Exception as e:
                    print(f"  ⚠️  使用vision_features生成失败: {e}")
                    print("  └─ 尝试fallback方法...")
                    # Fallback: 需要重新编码，但车端没有发送pixel_values
                    raise ValueError("无法使用vision_features生成，需要pixel_values")
            else:
                output_ids = self.model.generate(**gen_kwargs)
        
        gen_time = time.time() - gen_start
        print(f"  ✅ 模型推理完成，耗时: {gen_time:.2f}秒")
        
        # 提取生成的token（去掉prompt部分）
        prompt_len = input_ids.shape[1]
        generated = output_ids[:, prompt_len:]
        summary = self.processor.tokenizer.decode(generated[0], skip_special_tokens=True).strip()
        
        # 返回完整信息（用于后续处理）
        full_input_ids = output_ids.detach()
        if attention_mask is not None:
            generated_mask = torch.ones_like(generated, dtype=attention_mask.dtype).to(self.device)
            full_attention = torch.cat([attention_mask, generated_mask], dim=1)
        else:
            full_attention = torch.ones_like(full_input_ids)
        
        return summary, full_input_ids, full_attention, image_grid_thw
    
    def _generate_with_vision_features(self, input_ids, vision_features, image_grid_thw, attention_mask=None, max_new_tokens=256):
        """
        使用预计算的视觉特征生成文本
        
        通过临时替换get_image_features方法来跳过Vision Encoder
        """
        # 保存原始的get_image_features方法
        original_get_image_features = None
        model_module = None
        
        # 找到get_image_features方法
        if hasattr(self.model, 'model') and hasattr(self.model.model, 'get_image_features'):
            model_module = self.model.model
            original_get_image_features = self.model.model.get_image_features
        elif hasattr(self.model, 'get_image_features'):
            model_module = self.model
            original_get_image_features = self.model.get_image_features
        
        if original_get_image_features is None:
            raise ValueError("无法找到get_image_features方法，无法注入视觉特征")
        
        # 创建一个新的get_image_features方法，直接返回预计算的视觉特征
        def new_get_image_features(pixel_values, image_grid_thw):
            """替换的get_image_features方法，直接返回预计算的视觉特征"""
            # Qwen2.5-VL的get_image_features返回一个列表，每个元素是一个图像的embedding
            # vision_features形状: [batch, total_patches, 2048] 或 [total_patches, 2048]
            # 需要根据image_grid_thw分割成多个图像的embedding列表
            
            # 确保vision_features在正确的设备上
            vision_features_device = vision_features.to(self.device)
            
            # 如果vision_features是2D，添加batch维度
            if len(vision_features_device.shape) == 2:
                vision_features_expanded = vision_features_device.unsqueeze(0)
            else:
                vision_features_expanded = vision_features_device
            
            # 移除batch维度，得到 [total_patches, 2048]
            vision_features_flat = vision_features_expanded.squeeze(0)
            
            # 处理image_grid_thw
            if isinstance(image_grid_thw, torch.Tensor):
                image_grid_thw_np = image_grid_thw.cpu().numpy()
            else:
                image_grid_thw_np = np.array(image_grid_thw)
            
            # 确保image_grid_thw是2D数组
            if len(image_grid_thw_np.shape) == 1:
                image_grid_thw_np = image_grid_thw_np.reshape(1, -1)
            
            # 根据image_grid_thw分割成多个图像
            image_embeds_list = []
            start_idx = 0
            
            # 打印调试信息
            print(f"    [DEBUG] image_grid_thw_np形状: {image_grid_thw_np.shape}, 类型: {type(image_grid_thw_np)}")
            print(f"    [DEBUG] vision_features_flat形状: {vision_features_flat.shape}")
            print(f"    [DEBUG] vision_features_flat前2行: {vision_features_flat[:2]}")
            
            try:
                for i in range(len(image_grid_thw_np)):
                    grid_thw = image_grid_thw_np[i]
                    # 确保grid_thw是一维数组
                    if isinstance(grid_thw, np.ndarray):
                        grid_thw_flat = grid_thw.flatten()
                    else:
                        grid_thw_flat = np.array(grid_thw).flatten()
                    
                    # 确保有3个元素
                    if len(grid_thw_flat) < 3:
                        print(f"    [WARN] grid_thw[{i}] 元素不足3个: {grid_thw_flat}")
                        # 使用默认值（极端兜底情况）
                        t, h, w = 1, 1, 1
                    else:
                        t, h, w = int(grid_thw_flat[0]), int(grid_thw_flat[1]), int(grid_thw_flat[2])
                    
                    # ✅ 修正点：考虑 Qwen2.5-VL 的 2x2 pooling 压缩
                    # 原始 patch 数: h * w
                    # Qwen2.5-VL 在视觉编码阶段对空间维度做了 2x2 merge，
                    # 因此有效 token 数应为 (h/2) * (w/2)
                    processed_h = max(h // 2, 1)
                    processed_w = max(w // 2, 1)
                    num_patches = t * processed_h * processed_w
                    end_idx = start_idx + num_patches
                    
                    # 调试输出：当前图像的patch切分信息
                    print(
                        f"    [DEBUG] 图像{i}: "
                        f"t={t}, h={h}, w={w}, "
                        f"processed_h={processed_h}, processed_w={processed_w}, "
                        f"num_patches={num_patches}, start={start_idx}, end={end_idx}"
                    )
                    
                    # 提取这个图像的embedding
                    if end_idx <= vision_features_flat.shape[0]:
                        image_embed = vision_features_flat[start_idx:end_idx]  # [num_patches, 2048]
                    else:
                        # 如果超出范围，使用剩余的所有patches
                        image_embed = vision_features_flat[start_idx:]
                        print(f"    [WARN] 图像{i}超出范围，使用剩余patches: {image_embed.shape}")
                    
                    image_embeds_list.append(image_embed)
                    start_idx = end_idx
                    
                    # 如果已经处理完所有patches，跳出循环
                    if start_idx >= vision_features_flat.shape[0]:
                        break
                
                # 如果还有剩余的patches，添加到最后一个图像
                if start_idx < vision_features_flat.shape[0] and len(image_embeds_list) > 0:
                    remaining = vision_features_flat[start_idx:]
                    image_embeds_list[-1] = torch.cat([image_embeds_list[-1], remaining], dim=0)
                    print(f"    [DEBUG] 将剩余{remaining.shape[0]}个patches添加到最后一个图像")
                
                print(f"    [DEBUG] 最终返回{len(image_embeds_list)}个图像的embedding")
                for idx, emb in enumerate(image_embeds_list):
                    print(f"    [DEBUG]   图像{idx}: {emb.shape}")
                
            except Exception as e:
                print(f"    [ERROR] 分割视觉特征时出错: {e}")
                import traceback
                traceback.print_exc()
                # 如果出错，返回整个vision_features作为一个图像
                image_embeds_list = [vision_features_flat]
            
            # 确保返回的是列表
            if not isinstance(image_embeds_list, list):
                image_embeds_list = [image_embeds_list]
            
            # 返回列表，格式符合Qwen2.5-VL的预期
            # 每个元素是一个tensor，形状为 [num_patches_i, 2048]
            return image_embeds_list
        
        try:
            # 临时替换get_image_features方法
            if hasattr(self.model, 'model'):
                self.model.model.get_image_features = new_get_image_features
            else:
                self.model.get_image_features = new_get_image_features
            
            print("  └─ 已替换get_image_features方法，跳过Vision Encoder")
            print(f"  └─ 视觉特征形状: {vision_features.shape}")
            print(f"  └─ image_grid_thw形状: {image_grid_thw.shape if isinstance(image_grid_thw, torch.Tensor) else len(image_grid_thw)}")
            
            # 创建一个dummy pixel_values（模型需要这个参数，但会被忽略）
            # 根据image_grid_thw确定batch_size
            if isinstance(image_grid_thw, torch.Tensor):
                batch_size = image_grid_thw.shape[0] if len(image_grid_thw.shape) > 1 else 1
            else:
                batch_size = len(image_grid_thw) if isinstance(image_grid_thw, (list, tuple)) else 1
            
            dummy_pixel_values = torch.zeros(
                (batch_size, 3, 224, 224),
                dtype=vision_features.dtype,
                device=vision_features.device
            )
            
            # 调用generate
            gen_kwargs = {
                "input_ids": input_ids,
                "pixel_values": dummy_pixel_values,
                "image_grid_thw": image_grid_thw,
                "max_new_tokens": max_new_tokens,
                "do_sample": False
            }
            if attention_mask is not None:
                gen_kwargs["attention_mask"] = attention_mask
            
            print("  └─ 开始生成（使用预计算的视觉特征）...")
            output_ids = self.model.generate(**gen_kwargs)
            
            return output_ids
            
        finally:
            # 恢复原始的get_image_features方法
            if original_get_image_features is not None:
                if hasattr(self.model, 'model'):
                    self.model.model.get_image_features = original_get_image_features
                else:
                    self.model.get_image_features = original_get_image_features
                print("  └─ 已恢复get_image_features方法")
    
    def retrieve_regulations(self, summary: str, k: int = 3) -> List[str]:
        """
        从知识库检索相关法规
        
        Args:
            summary: 事故摘要
            k: 每个知识库检索的数量
            
        Returns:
            相关法规文本列表
        """
        # 从两个知识库分别检索
        results1 = self.vectorstore1.similarity_search(summary, k=k)
        results2 = self.vectorstore2.similarity_search(summary, k=k)
        
        # 合并结果
        all_results = results1 + results2
        
        # 计算查询向量
        query_embedding = np.array(self.embedder.embed_query(summary))
        
        # 按相似度排序
        all_results.sort(
            key=lambda doc: np.dot(query_embedding, np.array(self.embedder.embed_query(doc.page_content))),
            reverse=True
        )
        
        # 取最相关的前k条
        top_results = all_results[:k]
        
        return [doc.page_content for doc in top_results]
    
    def generate_advice(self, summary: str, regulations: List[str]) -> str:
        """
        基于摘要和法规生成处置建议
        
        Args:
            summary: 事故摘要
            regulations: 检索到的法规列表
            
        Returns:
            处置建议文本
        """
        # 合并法规文本
        retrieved_knowledge = "\n\n\n=====!!!!!!======".join(regulations)
        
        # 构造prompt
        prompt_text = f"""你是一名交通事故处置专家。

以下是视频中的事故摘要：
"{summary}"

以下是相关的交通法规：
====================
{retrieved_knowledge}
====================

请基于上述摘要和法规，给出详细的处置建议，建议只根据提供的法规提取；若无法依据法规给出建议，回答：没有根据提供的交通法规检索到处置建议。
输出格式：
处置建议: ...
"""
        
        # 构造消息
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "你是一名交通事故处置专家。"}
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text}
                ]
            }
        ]
        
        # 生成文本模板
        text = self.processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            chat_format="chatml"
        )
        
        # 处理输入
        inputs = self.processor(text=[text], return_tensors="pt").to(self.device)
        
        # 生成建议
        with torch.no_grad():
            output_ids = self.model.generate(**inputs, max_new_tokens=512, do_sample=False)
        
        # 解码响应
        response = self.processor.batch_decode(output_ids, skip_special_tokens=True)[0]
        
        # 提取建议部分（去掉prompt）
        if "assistant" in response.lower():
            parts = response.split("assistant")
            advice = parts[-1].strip() if len(parts) > 1 else response.strip()
        else:
            advice = response.strip()
        
        return advice
    
    def run(self, host: str = "0.0.0.0", debug: bool = False):
        """启动服务器"""
        print(f"\n🚀 服务器启动在 http://{host}:{self.port}")
        print("📡 API接口:")
        print(f"   GET  /api/health - 健康检查")
        print(f"   POST /api/process_tokens - 处理token")
        print("\n⚠️  注意: 处理token可能需要较长时间（30-120秒），请确保客户端超时时间足够长")
        print("\n按 Ctrl+C 停止服务器\n")
        
        # 增加Flask的请求超时时间（通过threaded和processes参数）
        self.app.run(host=host, port=self.port, debug=debug, threaded=True)


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="语义通信服务器")
    parser.add_argument("--port", type=int, default=5001, help="服务器端口")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="服务器地址")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    
    args = parser.parse_args()
    
    # 创建并启动服务器
    server = SemanticCommServer(port=args.port)
    server.run(host=args.host, debug=args.debug)


if __name__ == "__main__":
    main()
