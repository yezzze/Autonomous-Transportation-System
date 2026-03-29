"""
测试 Magentic-One 工作流

测试场景：
1. 简单任务 → Sequential 编排
2. 中等任务 → Concurrent 编排  
3. 复杂任务 → Magentic-One 编排
"""
import asyncio
import os
import sys

# 添加项目根目录到 path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.graph.state_utils import create_magentic_state, get_magentic_status
from src.graph.adaptive_orchestrator import (
    evaluate_task_complexity,
    evaluate_complexity_by_rules,
    run_adaptive_workflow
)
from src.graph.progress_ledger import create_progress_ledger
from src.agents.llm import get_llm_by_type
from src.config.agents import LLMType


async def test_complexity_evaluation():
    """测试复杂度评估"""
    print("=" * 60)
    print("测试 1: 复杂度评估")
    print("=" * 60)
    
    test_cases = [
        "今天北京天气如何？",  # 简单
        "比较一下 Python 和 Go 的性能差异",  # 中等
        "分析 2024 年全球 AI 发展趋势，需要搜索最新论文、查看市场报告、生成可视化图表，最后写一份 10 页的分析报告"  # 复杂
    ]
    
    for i, query in enumerate(test_cases, 1):
        print(f"\n--- 案例 {i} ---")
        print(f"查询: {query}")
        
        # LLM 评估
        try:
            llm_result = await evaluate_task_complexity(query)
            print(f"LLM 评估: {llm_result}")
        except Exception as e:
            print(f"LLM 评估失败: {e}")
        
        # 规则评估
        rule_result = evaluate_complexity_by_rules(query)
        print(f"规则评估: {rule_result}")
        print()


async def test_progress_ledger():
    """测试 Progress Ledger 生成"""
    print("=" * 60)
    print("测试 2: Progress Ledger 生成")
    print("=" * 60)
    
    # 模拟状态
    state = create_magentic_state(
        user_query="分析特斯拉 2024 Q3 财报，生成投资建议",
        max_round=20,
        max_stall=3
    )
    
    # 添加一些历史消息（模拟执行过程）
    state["messages"].extend([
        {"role": "assistant", "content": "我将分 3 个步骤完成：1. 搜索财报 2. 分析数据 3. 生成建议"},
        {"role": "assistant", "content": "[WebSurfer] 已找到特斯拉 Q3 财报，营收 251 亿美元"},
        {"role": "assistant", "content": "[Coder] 正在分析财务指标..."},
    ])
    
    try:
        ledger = await create_progress_ledger(state)
        print("\n生成的 Progress Ledger:")
        print(f"任务完成: {ledger['is_request_satisfied']['answer']}")
        print(f"陷入循环: {ledger['is_in_loop']['answer']}")
        print(f"有进展: {ledger['is_progress_being_made']['answer']}")
        print(f"\n下一个发言者: {ledger['next_speaker']['answer']}")
        print(f"理由: {ledger['next_speaker']['reason']}")
        print(f"\n指令: {ledger['instruction_or_question']['answer']}")
        
    except Exception as e:
        print(f"生成失败: {e}")
        import traceback
        traceback.print_exc()


async def test_adaptive_workflow():
    """测试自适应工作流（模拟）"""
    print("\n" + "=" * 60)
    print("测试 3: 自适应工作流（仅测试路由逻辑）")
    print("=" * 60)
    
    test_cases = [
        ("今天北京天气如何？", "simple"),
        ("比较 Python 和 Go 的性能", "medium"),
        ("分析 AI 趋势并生成报告", "complex"),
    ]
    
    for query, expected in test_cases:
        print(f"\n查询: {query}")
        print(f"预期复杂度: {expected}")
        
        # 仅测试复杂度评估
        result = evaluate_complexity_by_rules(query)
        print(f"实际复杂度: {result}")
        print(f"匹配: {'✅' if result == expected else '❌'}")


async def test_magentic_state():
    """测试 Magentic State 初始化"""
    print("\n" + "=" * 60)
    print("测试 4: Magentic State 初始化")
    print("=" * 60)
    
    state = create_magentic_state(
        user_query="测试查询",
        max_round=15,
        max_stall=2
    )
    
    status = get_magentic_status(state)
    print("\n初始状态:")
    for key, value in status.items():
        print(f"  {key}: {value}")
    
    # 模拟执行一轮
    state["magentic_round"] = 1
    state["magentic_mode"] = "inner_loop"
    state["progress_ledger"] = {"test": "data"}
    
    status = get_magentic_status(state)
    print("\n执行一轮后:")
    for key, value in status.items():
        print(f"  {key}: {value}")


async def main():
    """主测试函数"""
    print("\n🚀 开始测试 Magentic-One 功能\n")
    
    try:
        # 测试 1: 复杂度评估
        await test_complexity_evaluation()
        
        # 测试 2: Progress Ledger
        await test_progress_ledger()
        
        # 测试 3: 自适应路由
        await test_adaptive_workflow()
        
        # 测试 4: State 初始化
        await test_magentic_state()
        
        print("\n" + "=" * 60)
        print("✅ 所有测试完成")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
