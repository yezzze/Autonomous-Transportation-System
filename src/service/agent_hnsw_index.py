"""
HNSW 智能体向量索引服务

用于在大规模智能体场景下（上万个）快速检索最相关的候选智能体
使用 HNSW (Hierarchical Navigable Small World) 算法进行高效的向量相似度搜索

工作流程：
1. 将智能体的能力描述编码为向量
2. 建立 HNSW 索引
3. 给定任务描述，快速检索 Top-K 候选智能体
4. 再通过 LLM 进行精细筛选
"""
import logging
import numpy as np
import hnswlib
from typing import List, Dict, Any, Tuple
from pathlib import Path
import json
import hashlib
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class AgentHNSWIndex:
    """智能体 HNSW 向量索引"""
    
    def __init__(
        self,
        index_path: str = "data/agent_hnsw_index",
        embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2",
        dim: int = 384,  # MiniLM 模型的维度
        max_elements: int = 100000,  # 支持最多10万个智能体
        ef_construction: int = 200,
        M: int = 16
    ):
        """
        初始化 HNSW 索引
        
        Args:
            index_path: 索引文件保存路径
            embedding_model: 句子编码模型
            dim: 向量维度
            max_elements: 最大元素数量
            ef_construction: 构建参数（越大越精确但越慢）
            M: 连接数（影响召回率和内存）
        """
        self.index_path = Path(index_path)
        self.index_path.mkdir(parents=True, exist_ok=True)
        
        self.dim = dim
        self.max_elements = max_elements
        
        # 初始化 HNSW 索引
        self.index = hnswlib.Index(space='cosine', dim=dim)
        self.index.init_index(
            max_elements=max_elements,
            ef_construction=ef_construction,
            M=M
        )
        self.index.set_ef(50)  # 查询时的 ef 参数
        
        # 加载 Sentence Transformer 模型
        logger.info(f"🔧 加载嵌入模型: {embedding_model}")
        self.encoder = SentenceTransformer(embedding_model)
        
        # 智能体元数据存储
        self.agent_metadata: Dict[int, Dict[str, Any]] = {}
        self.id_to_idx: Dict[str, int] = {}  # agent_id -> index
        self.idx_to_id: Dict[int, str] = {}  # index -> agent_id
        self.next_idx = 0
        
        # 尝试加载已有索引
        self._load_index()
    
    def _get_agent_text(self, agent: Dict[str, Any]) -> str:
        """
        构建智能体的文本表示（用于编码）
        
        Args:
            agent: 智能体信息
        
        Returns:
            文本表示
        """
        parts = []
        
        # 能力
        if capability := agent.get("capability"):
            parts.append(f"能力: {capability}")
        
        # 描述
        if description := agent.get("description"):
            parts.append(f"描述: {description}")
        
        # 标签
        if tags := agent.get("tags"):
            tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
            parts.append(f"标签: {tags_str}")
        
        # 节点名称
        if node_name := agent.get("node_name"):
            parts.append(f"节点: {node_name}")
        
        return " | ".join(parts)
    
    def _encode_text(self, text: str) -> np.ndarray:
        """
        将文本编码为向量
        
        Args:
            text: 输入文本
        
        Returns:
            向量表示
        """
        embedding = self.encoder.encode(text, convert_to_numpy=True)
        # 归一化（cosine 距离需要）
        embedding = embedding / np.linalg.norm(embedding)
        return embedding.astype('float32')
    
    def add_agents(self, agents: List[Dict[str, Any]], rebuild: bool = False):
        """
        添加智能体到索引
        
        Args:
            agents: 智能体列表
            rebuild: 是否重建索引
        """
        if rebuild:
            logger.info("🔄 重建 HNSW 索引...")
            self.index = hnswlib.Index(space='cosine', dim=self.dim)
            self.index.init_index(
                max_elements=self.max_elements,
                ef_construction=200,
                M=16
            )
            self.agent_metadata.clear()
            self.id_to_idx.clear()
            self.idx_to_id.clear()
            self.next_idx = 0
        
        logger.info(f"📥 添加 {len(agents)} 个智能体到 HNSW 索引...")
        
        vectors = []
        indices = []
        
        for agent in agents:
            agent_id = agent.get("id")
            if not agent_id:
                logger.warning(f"⚠️  智能体缺少 ID，跳过: {agent}")
                continue
            
            # 检查是否已存在
            if agent_id in self.id_to_idx:
                logger.debug(f"智能体 {agent_id} 已存在，跳过")
                continue
            
            # 构建文本表示
            agent_text = self._get_agent_text(agent)
            
            # 编码为向量
            vector = self._encode_text(agent_text)
            
            # 分配索引
            idx = self.next_idx
            self.next_idx += 1
            
            # 保存映射关系
            self.id_to_idx[agent_id] = idx
            self.idx_to_id[idx] = agent_id
            self.agent_metadata[idx] = agent
            
            vectors.append(vector)
            indices.append(idx)
        
        # 批量添加到 HNSW 索引
        if vectors:
            vectors = np.array(vectors)
            indices = np.array(indices)
            self.index.add_items(vectors, indices)
            logger.info(f"✅ 成功添加 {len(vectors)} 个智能体到索引")
        
        # 保存索引
        self._save_index()
    
    def search(
        self, 
        query: str, 
        top_k: int = 10,
        filter_capability: str = None,
        filter_enabled: bool = True
    ) -> List[Tuple[Dict[str, Any], float]]:
        """
        搜索最相关的智能体
        
        Args:
            query: 查询文本（任务描述）
            top_k: 返回前 K 个结果
            filter_capability: 过滤特定能力
            filter_enabled: 是否只返回启用的智能体
        
        Returns:
            (智能体信息, 相似度得分) 的列表，按相似度降序排列
        """
        if self.next_idx == 0:
            logger.warning("⚠️  索引为空，无法搜索")
            return []
        
        # 编码查询
        query_vector = self._encode_text(query)
        
        # 搜索（返回更多候选，用于后续过滤）
        search_k = min(top_k * 3, self.next_idx)
        labels, distances = self.index.knn_query(query_vector, k=search_k)
        
        # 转换为智能体信息
        results = []
        for idx, distance in zip(labels[0], distances[0]):
            agent = self.agent_metadata.get(idx)
            if not agent:
                continue
            
            # 过滤条件
            if filter_enabled and not agent.get("enabled", True):
                continue
            
            if filter_capability and agent.get("capability") != filter_capability:
                continue
            
            # Cosine 距离转换为相似度（0-1，越大越相似）
            similarity = 1 - distance
            results.append((agent, float(similarity)))
            
            if len(results) >= top_k:
                break
        
        logger.info(
            f"🔍 HNSW 检索: 查询='{query[:50]}...', "
            f"返回 {len(results)} 个候选智能体"
        )
        
        return results
    
    def get_agent_by_id(self, agent_id: str) -> Dict[str, Any]:
        """
        根据 ID 获取智能体信息
        
        Args:
            agent_id: 智能体 ID
        
        Returns:
            智能体信息，不存在则返回 None
        """
        idx = self.id_to_idx.get(agent_id)
        if idx is None:
            return None
        return self.agent_metadata.get(idx)
    
    def _save_index(self):
        """保存索引到磁盘"""
        try:
            # 保存 HNSW 索引
            index_file = self.index_path / "hnsw_index.bin"
            self.index.save_index(str(index_file))
            
            # 保存元数据
            metadata_file = self.index_path / "metadata.json"
            metadata = {
                "agent_metadata": {
                    str(k): v for k, v in self.agent_metadata.items()
                },
                "id_to_idx": self.id_to_idx,
                "idx_to_id": {str(k): v for k, v in self.idx_to_id.items()},
                "next_idx": self.next_idx
            }
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            logger.info(f"💾 索引已保存: {self.index_path}")
        except Exception as e:
            logger.error(f"❌ 保存索引失败: {e}")
    
    def _load_index(self):
        """从磁盘加载索引"""
        index_file = self.index_path / "hnsw_index.bin"
        metadata_file = self.index_path / "metadata.json"
        
        if not index_file.exists() or not metadata_file.exists():
            logger.info("📂 未找到已有索引，将创建新索引")
            return
        
        try:
            # 加载 HNSW 索引
            self.index.load_index(str(index_file), max_elements=self.max_elements)
            
            # 加载元数据
            with open(metadata_file, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            self.agent_metadata = {
                int(k): v for k, v in metadata["agent_metadata"].items()
            }
            self.id_to_idx = metadata["id_to_idx"]
            self.idx_to_id = {
                int(k): v for k, v in metadata["idx_to_id"].items()
            }
            self.next_idx = metadata["next_idx"]
            
            logger.info(
                f"✅ 成功加载索引: {len(self.agent_metadata)} 个智能体"
            )
        except Exception as e:
            logger.error(f"❌ 加载索引失败: {e}")
            logger.info("将创建新索引")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取索引统计信息
        
        Returns:
            统计信息字典
        """
        return {
            "total_agents": len(self.agent_metadata),
            "index_size": self.next_idx,
            "max_elements": self.max_elements,
            "dimension": self.dim,
            "capabilities": list(set(
                a.get("capability") for a in self.agent_metadata.values()
                if a.get("capability")
            ))
        }


# 全局单例
_hnsw_index_instance = None


def get_hnsw_index() -> AgentHNSWIndex:
    """
    获取 HNSW 索引的全局单例
    
    Returns:
        AgentHNSWIndex 实例
    """
    global _hnsw_index_instance
    if _hnsw_index_instance is None:
        _hnsw_index_instance = AgentHNSWIndex()
    return _hnsw_index_instance
