"""
HNSW 智能体检索系统测试

演示如何使用 HNSW 进行大规模智能体快速检索
"""
import sys
import logging
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from src.service.agent_hnsw_index import get_hnsw_index
from src.service.agent_registry import get_registry_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_hnsw_retrieval():
    """测试 HNSW 智能体检索"""
    
    print("=" * 60)
    print("🧪 HNSW 智能体检索系统测试")
    print("=" * 60)
    
    # 1. 获取注册表客户端（自动判断是否使用 HNSW）
    print("\n📦 初始化注册表...")
    registry = get_registry_client()
    
    # 打印统计信息
    summary = registry.get_registry_summary()
    print(f"\n📊 注册表统计:")
    print(f"  - 总智能体数: {summary['total_agents']}")
    print(f"  - 在线智能体: {summary['online_agents']}")
    print(f"  - 检索模式: {summary['retrieval_mode']}")
    if 'hnsw_stats' in summary:
        hnsw_stats = summary['hnsw_stats']
        print(f"  - HNSW 索引大小: {hnsw_stats['total_agents']}")
        print(f"  - 向量维度: {hnsw_stats['dimension']}")
    
    # 2. 测试不同的查询
    test_queries = [
        {
            "task": "搜索今天的天气情况",
            "expected_capability": "search"
        },
        {
            "task": "计算一组数据的统计分析，包括均值、方差和标准差",
            "expected_capability": "compute"
        },
        {
            "task": "分析这张图片中的物体并生成描述",
            "expected_capability": "vision"
        },
        {
            "task": "将这段英文翻译成中文，并进行情感分析",
            "expected_capability": "nlp"
        },
        {
            "task": "执行这段 Python 代码并返回结果",
            "expected_capability": "code_execution"
        },
        {
            "task": "打开网页并提取指定元素的内容",
            "expected_capability": "web_interaction"
        }
    ]
    
    print("\n" + "=" * 60)
    print("🔍 开始检索测试")
    print("=" * 60)
    
    for i, query in enumerate(test_queries, 1):
        task = query["task"]
        expected = query["expected_capability"]
        
        print(f"\n【测试 {i}】")
        print(f"任务: {task}")
        print(f"期望能力: {expected}")
        print("-" * 60)
        
        # 使用 HNSW 检索（如果启用）
        agents = registry.query_agents(
            task_description=task,
            top_k=3
        )
        
        if agents:
            print(f"✅ 检索到 {len(agents)} 个候选智能体:")
            for j, agent in enumerate(agents, 1):
                similarity = agent.get("_similarity_score", 0)
                print(f"  {j}. {agent['id']}")
                print(f"     能力: {agent['capability']}")
                print(f"     描述: {agent['description']}")
                if similarity > 0:
                    print(f"     相似度: {similarity:.3f}")
                print()
            
            # 检查是否匹配预期
            top_agent = agents[0]
            if top_agent['capability'] == expected:
                print(f"✅ 匹配成功！Top-1 智能体能力正确")
            else:
                print(f"⚠️  Top-1 智能体能力 ({top_agent['capability']}) "
                      f"与预期 ({expected}) 不符")
        else:
            print("❌ 未找到匹配的智能体")
    
    print("\n" + "=" * 60)
    print("✅ 测试完成")
    print("=" * 60)


def test_capability_filter():
    """测试能力过滤"""
    print("\n" + "=" * 60)
    print("🧪 测试能力过滤")
    print("=" * 60)
    
    registry = get_registry_client()
    
    # 测试：只搜索 search 能力的智能体
    task = "我需要搜索一些信息"
    print(f"\n任务: {task}")
    print(f"过滤: capability=search")
    
    agents = registry.query_agents(
        task_description=task,
        capability="search",
        top_k=5
    )
    
    print(f"\n✅ 找到 {len(agents)} 个 search 能力的智能体:")
    for agent in agents:
        print(f"  - {agent['id']}: {agent['capability']}")


def benchmark_retrieval_speed():
    """性能基准测试"""
    import time
    
    print("\n" + "=" * 60)
    print("⚡ 检索性能测试")
    print("=" * 60)
    
    registry = get_registry_client()
    
    test_query = "分析这份财务报表并生成摘要"
    num_runs = 100
    
    print(f"\n查询: {test_query}")
    print(f"运行次数: {num_runs}")
    
    # 预热
    registry.query_agents(task_description=test_query, top_k=10)
    
    # 计时
    start = time.time()
    for _ in range(num_runs):
        registry.query_agents(task_description=test_query, top_k=10)
    elapsed = time.time() - start
    
    avg_time = elapsed / num_runs * 1000  # 毫秒
    
    print(f"\n性能结果:")
    print(f"  - 总耗时: {elapsed:.3f} 秒")
    print(f"  - 平均耗时: {avg_time:.2f} 毫秒/次")
    print(f"  - QPS: {num_runs/elapsed:.1f}")


if __name__ == "__main__":
    print("\n🚀 LangManus HNSW 智能体检索系统\n")
    
    try:
        # 基本检索测试
        test_hnsw_retrieval()
        
        # 能力过滤测试
        test_capability_filter()
        
        # 性能测试
        benchmark_retrieval_speed()
        
    except Exception as e:
        logger.error(f"测试失败: {e}", exc_info=True)
        sys.exit(1)
