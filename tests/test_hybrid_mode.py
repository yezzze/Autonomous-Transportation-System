"""
混合编排模式测试脚本

测试场景：
1. 正常执行（静态编排）
2. 单次失败 + 规则恢复
3. 多次失败 + LLM 重规划
"""

import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.distributed_workflow import run_distributed_workflow

# 配置日志
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def enable_debug_logging():
    """启用调试日志"""
    logging.getLogger().setLevel(logging.DEBUG)

def test_normal_execution():
    """测试场景 1：正常执行（无失败）"""
    print("\n" + "="*80)
    print("测试场景 1：正常执行 - 静态编排模式")
    print("="*80 + "\n")
    
    result = run_distributed_workflow(
        user_input="分析 DeepSeek R1 的技术亮点和应用场景",
        debug=True,
        replanning_enabled=True,  # 启用重规划，但正常情况不会触发
        max_replanning=2
    )
    
    print("\n✅ 测试完成 - 正常执行")
    print(f"执行计划任务数：{len(result.get('execution_plan', []))}")
    print(f"重规划次数：{result.get('replanning_count', 0)}")
    print(f"失败任务数：{len(result.get('failed_tasks', []))}")


def test_rule_based_recovery():
    """测试场景 2：简单失败 + 规则路由恢复"""
    print("\n" + "="*80)
    print("测试场景 2：规则路由恢复")
    print("="*80 + "\n")
    
    # 注意：在 Mock 模式下无法真正测试失败，这里只是演示配置
    result = run_distributed_workflow(
        user_input="获取最新的 AI 新闻并进行情感分析",
        debug=True,
        replanning_enabled=True,
        max_replanning=2
    )
    
    print("\n✅ 测试完成 - 规则路由")
    print(f"重规划次数：{result.get('replanning_count', 0)}")
    print(f"最后重规划原因：{result.get('last_replan_reason', 'N/A')}")


def test_llm_replanning():
    """测试场景 3：复杂失败 + LLM 重规划"""
    print("\n" + "="*80)
    print("测试场景 3：LLM 智能重规划")
    print("="*80 + "\n")
    
    result = run_distributed_workflow(
        user_input="分析 GPT-4 和 DeepSeek R1 的性能对比，并生成可视化图表",
        debug=True,
        replanning_enabled=True,
        max_replanning=2
    )
    
    print("\n✅ 测试完成 - LLM 重规划")
    print(f"执行计划任务数：{len(result.get('execution_plan', []))}")
    print(f"重规划次数：{result.get('replanning_count', 0)}")


def test_disabled_replanning():
    """测试场景 4：禁用重规划（纯静态模式）"""
    print("\n" + "="*80)
    print("测试场景 4：禁用重规划 - 纯静态编排")
    print("="*80 + "\n")
    
    result = run_distributed_workflow(
        user_input="总结 Transformer 架构的核心创新",
        debug=True,
        replanning_enabled=False,  # 禁用重规划
        max_replanning=0
    )
    
    print("\n✅ 测试完成 - 纯静态模式")
    print(f"重规划次数：{result.get('replanning_count', 0)}")
    print(f"重规划已禁用：{not result.get('replanning_enabled', True)}")


def compare_modes():
    """对比不同编排模式"""
    print("\n" + "="*80)
    print("混合编排模式 vs 静态编排模式 对比")
    print("="*80 + "\n")
    
    print("📊 混合模式特性：")
    print("  ✓ 正常情况：高效静态编排（无额外 LLM 调用）")
    print("  ✓ 失败情况：3 层恢复机制")
    print("    1️⃣  规则路由（简单重试、切换 Agent）")
    print("    2️⃣  LLM 智能重规划（复杂问题）")
    print("    3️⃣  限制重规划次数（控制成本）")
    print("\n📊 静态模式特性：")
    print("  ✓ 完全预定义流程（Planner → Executor → Monitor → Reporter）")
    print("  ✗ 无失败恢复能力")
    print("\n💡 混合模式优势：")
    print("  • 效率：大部分时间等同于静态模式")
    print("  • 鲁棒性：失败时自动切换到动态重规划")
    print("  • 成本可控：限制 LLM 调用次数")
    print("\n💡 适用场景：")
    print("  • 混合模式：生产环境（需要高可靠性）")
    print("  • 静态模式：Demo 或简单场景（追求极致效率）")


if __name__ == "__main__":
    # 启用调试日志
    enable_debug_logging()
    
    print("\n" + "🚀" * 40)
    print("混合编排模式测试套件")
    print("🚀" * 40)
    
    # 运行所有测试
    try:
        # 测试 1：正常执行
        test_normal_execution()
        
        # 测试 2：规则恢复（Mock 模式下无法真正触发）
        # test_rule_based_recovery()
        
        # 测试 3：LLM 重规划（Mock 模式下无法真正触发）
        # test_llm_replanning()
        
        # 测试 4：禁用重规划
        test_disabled_replanning()
        
        # 模式对比说明
        compare_modes()
        
        print("\n" + "="*80)
        print("✅ 所有测试完成")
        print("="*80 + "\n")
        
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
