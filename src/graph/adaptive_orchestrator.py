"""
自适应编排器（Adaptive Orchestrator）

根据任务复杂度自动选择编排模式：
- Simple: Sequential（串行）
- Medium: Concurrent（并行）
- Complex: Magentic-One（反馈循环）
"""

from typing import Literal
from langchain_core.messages import HumanMessage
from src.agents.llm import get_llm_by_type
import logging

logger = logging.getLogger(__name__)


# ===== 复杂度评估 =====

COMPLEXITY_EVALUATION_PROMPT = """
分析以下任务的复杂度，选择最合适的等级。

## 任务
{task}

## 复杂度定义

### simple（简单）
- 单步骤任务，无需多轮交互
- 示例：查询天气、简单计算、信息检索
- 特征：目标明确、步骤单一、无需迭代

### medium（中等）
- 多步骤但相对独立的任务
- 示例：多角度分析、并行数据收集、多源对比
- 特征：可并行处理、步骤相对独立、少量依赖

### complex（复杂）
- 需要多轮迭代、动态调整的任务
- 示例：研究报告、复杂问题求解、需要反馈的任务
- 特征：高度不确定性、需要多次尝试、步骤间强依赖

## 评估标准

考虑以下因素：
1. **步骤数量**：1步=simple，2-5步=medium，>5步=complex
2. **不确定性**：确定=simple，部分确定=medium，高度不确定=complex
3. **反馈需求**：无需反馈=simple，少量反馈=medium，频繁反馈=complex
4. **失败风险**：低风险=simple，中风险=medium，高风险=complex

## 输出

只输出一个单词，不要有任何其他内容：simple / medium / complex
"""


async def evaluate_task_complexity(task: str) -> Literal["simple", "medium", "complex"]:
    """
    评估任务复杂度
    
    Args:
        task: 用户任务描述
        
    Returns:
        "simple" | "medium" | "complex"
    """
    llm = get_llm_by_type("basic")  # 使用基础模型评估即可
    
    prompt = COMPLEXITY_EVALUATION_PROMPT.format(task=task)
    
    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        complexity = response.content.strip().lower()
        
        # 验证输出
        if complexity not in ["simple", "medium", "complex"]:
            logger.warning(f"LLM 返回了无效的复杂度: {complexity}，默认使用 medium")
            return "medium"
        
        logger.info(f"📊 任务复杂度评估: {complexity.upper()}")
        return complexity
        
    except Exception as e:
        logger.error(f"复杂度评估失败: {e}，默认使用 medium")
        return "medium"


def evaluate_complexity_by_rules(task: str) -> Literal["simple", "medium", "complex"]:
    """
    基于规则的复杂度评估（快速，无 LLM 成本）
    
    Args:
        task: 用户任务描述
        
    Returns:
        "simple" | "medium" | "complex"
    """
    task_lower = task.lower()
    
    # 简单任务关键词
    simple_keywords = [
        "查询", "查找", "搜索", "获取", "查看",
        "what is", "show me", "get", "find", "search"
    ]
    
    # 复杂任务关键词
    complex_keywords = [
        "分析", "研究", "调查", "优化", "设计", "规划",
        "analyze", "research", "investigate", "optimize", "design", "plan",
        "多轮", "迭代", "反复", "尝试", "探索"
    ]
    
    # 中等任务关键词
    medium_keywords = [
        "对比", "比较", "总结", "整理", "汇总",
        "compare", "summarize", "organize", "collect"
    ]
    
    # 判断逻辑
    complex_count = sum(1 for kw in complex_keywords if kw in task_lower)
    medium_count = sum(1 for kw in medium_keywords if kw in task_lower)
    simple_count = sum(1 for kw in simple_keywords if kw in task_lower)
    
    # 任务长度也是一个指标
    task_length = len(task)
    
    if complex_count > 0 or task_length > 200:
        return "complex"
    elif simple_count > 0 and task_length < 50:
        return "simple"
    else:
        return "medium"


# ===== 自适应编排器 =====

async def adaptive_orchestrate(
    task: str,
    use_llm_evaluation: bool = True,
    debug: bool = False
) -> Literal["simple", "medium", "complex"]:
    """
    自适应编排入口
    
    Args:
        task: 用户任务
        use_llm_evaluation: 是否使用 LLM 评估（False 则使用规则）
        debug: 是否显示调试信息
        
    Returns:
        选择的编排模式
    """
    if use_llm_evaluation:
        complexity = await evaluate_task_complexity(task)
    else:
        complexity = evaluate_complexity_by_rules(task)
        logger.info(f"📊 任务复杂度评估（规则）: {complexity.upper()}")
    
    # 显示选择的模式
    mode_desc = {
        "simple": "Sequential（串行编排）- 高效、低成本",
        "medium": "Concurrent（并行编排）- 快速、多角度",
        "complex": "Magentic-One（反馈循环）- 鲁棒、智能"
    }
    
    logger.info(f"✅ 选择编排模式: {mode_desc[complexity]}")
    
    if debug:
        print(f"\n{'='*60}")
        print(f"🎯 自适应编排决策")
        print(f"{'='*60}")
        print(f"任务: {task[:100]}...")
        print(f"复杂度: {complexity.upper()}")
        print(f"模式: {mode_desc[complexity]}")
        print(f"{'='*60}\n")
    
    return complexity


# ===== 编排模式映射 =====

def get_workflow_builder_for_complexity(complexity: str):
    """
    根据复杂度返回对应的 Workflow Builder
    
    Args:
        complexity: "simple" | "medium" | "complex"
        
    Returns:
        对应的 builder 函数
    """
    from .distributed_builder import build_distributed_graph
    from .magentic_builder import build_magentic_graph
    
    if complexity == "simple":
        # Sequential: 使用简化的线性流程
        return build_distributed_graph  # 复用现有的（本质是 sequential）
    
    elif complexity == "medium":
        # Concurrent: 需要实现并行逻辑
        # 暂时使用 distributed，未来可扩展
        return build_distributed_graph
    
    else:  # complex
        # Magentic-One: 完整的反馈循环
        return build_magentic_graph


# ===== 使用示例 =====

async def run_adaptive_workflow(user_input: str, debug: bool = False, **kwargs):
    """
    运行自适应工作流
    
    自动根据任务复杂度选择最优编排模式
    """
    # 1. 评估复杂度
    complexity = await adaptive_orchestrate(user_input, debug=debug)
    
    # 2. 获取对应的 workflow builder
    builder = get_workflow_builder_for_complexity(complexity)
    
    # 3. 构建并运行 workflow
    workflow = builder()
    
    initial_state = {
        "messages": [{"role": "user", "content": user_input}],
        "complexity_level": complexity,  # 记录使用的模式
        **kwargs
    }
    
    result = await workflow.ainvoke(initial_state)
    
    return result
