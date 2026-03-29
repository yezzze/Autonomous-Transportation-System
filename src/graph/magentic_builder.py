"""
Magentic-One Graph Builder

构建完整的 Magentic-One 工作流图：
- Outer Loop: Planner
- Inner Loop: Orchestrator → Executor → (循环)
- Termination: Reporter
"""

from langgraph.graph import StateGraph, START
from .distributed_types import DistributedState
from .magentic_nodes import (
    magentic_planner_node,
    magentic_orchestrator_node,
    magentic_executor_node,
    magentic_reporter_node
)


def build_magentic_graph() -> StateGraph:
    """
    构建 Magentic-One 工作流图
    
    图结构：
    START → Planner → Orchestrator ⇄ Executor → Reporter → END
                          ↓
                      (inner loop)
    
    Returns:
        编译后的 StateGraph
    """
    # 创建状态图
    graph = StateGraph(DistributedState)
    
    # 添加节点（使用完整名称以匹配 Command 中的 goto）
    graph.add_node("magentic_planner", magentic_planner_node)
    graph.add_node("magentic_orchestrator", magentic_orchestrator_node)
    graph.add_node("magentic_executor", magentic_executor_node)
    graph.add_node("magentic_reporter", magentic_reporter_node)
    
    # 添加边
    # START → Planner
    graph.add_edge(START, "magentic_planner")
    
    # Planner → Orchestrator（进入 inner loop）
    # Orchestrator 会根据 Command 的 goto 决定下一步
    # Executor → Orchestrator（inner loop 循环）
    
    # 注意：Magentic-One 的路由由 Command 控制，不需要显式条件边
    
    # 编译图
    compiled_graph = graph.compile()
    
    return compiled_graph
