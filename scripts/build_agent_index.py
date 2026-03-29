"""
批量添加智能体到 HNSW 索引

用于在部署时预构建索引
"""
import sys
import json
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.service.agent_hnsw_index import get_hnsw_index

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_agents_from_config(config_file: str = "config/agent_registry.json"):
    """从配置文件加载智能体"""
    config_path = Path(config_file)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_file}")
        return []
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    agents = config.get('agents', [])
    logger.info(f"从配置文件加载了 {len(agents)} 个智能体")
    return agents


def build_hnsw_index(config_file: str = "config/agent_registry.json", rebuild: bool = True):
    """构建 HNSW 索引"""
    print("=" * 60)
    print("🔧 构建 HNSW 智能体索引")
    print("=" * 60)
    
    # 加载智能体
    print(f"\n📥 从配置文件加载智能体: {config_file}")
    agents = load_agents_from_config(config_file)
    
    if not agents:
        print("❌ 未找到智能体，退出")
        return
    
    print(f"✅ 加载了 {len(agents)} 个智能体")
    
    # 获取 HNSW 索引
    print("\n🔧 初始化 HNSW 索引...")
    hnsw_index = get_hnsw_index()
    
    # 添加智能体
    print(f"\n📦 添加智能体到索引 (rebuild={rebuild})...")
    hnsw_index.add_agents(agents, rebuild=rebuild)
    
    # 显示统计
    stats = hnsw_index.get_stats()
    print("\n📊 索引统计:")
    print(f"  - 智能体总数: {stats['total_agents']}")
    print(f"  - 索引大小: {stats['index_size']}")
    print(f"  - 最大容量: {stats['max_elements']}")
    print(f"  - 向量维度: {stats['dimension']}")
    print(f"  - 能力类型: {', '.join(stats['capabilities'])}")
    
    print("\n✅ 索引构建完成！")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="构建 HNSW 智能体索引")
    parser.add_argument(
        "--config",
        type=str,
        default="config/agent_registry.json",
        help="智能体配置文件路径"
    )
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="不重建索引，仅添加新智能体"
    )
    
    args = parser.parse_args()
    
    try:
        build_hnsw_index(
            config_file=args.config,
            rebuild=not args.no_rebuild
        )
    except Exception as e:
        logger.error(f"构建索引失败: {e}", exc_info=True)
        sys.exit(1)
