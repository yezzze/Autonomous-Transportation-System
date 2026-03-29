"""
分布式 Agent 调度器的 Graph 构建
"""
from langgraph.graph import StateGraph, START

from .distributed_types import DistributedState
from .distributed_nodes import (
    distributed_planner_node,
    distributed_executor_node,
    distributed_monitor_node,
    distributed_reporter_node,
)


def build_distributed_graph():
    """
    构建分布式 Agent 调度器的 Graph
    
    工作流程：
    START -> Planner -> Executor -> Monitor -> (循环执行或生成报告) -> END
    
    复用了原 LangManus 的：
    - StateGraph 结构
    - 节点添加和边连接方式
    
    改变的部分：
    - 移除了本地工具节点（researcher, coder, browser）
    - 简化为 Planner -> Executor -> Monitor 的线性流程
    - Monitor 负责路由决策（类似原 Supervisor 的角色）
    """
    builder = StateGraph(DistributedState)
    
    # 添加起始边
    builder.add_edge(START, "planner")
    
    # 添加节点
    builder.add_node("planner", distributed_planner_node)
    builder.add_node("executor", distributed_executor_node)
    builder.add_node("monitor", distributed_monitor_node)
    builder.add_node("reporter", distributed_reporter_node)
    
    # 注意：边的连接由各节点的 Command.goto 动态控制
    # 这里不需要手动添加条件边，因为：
    # - planner 返回 goto="executor" 或 "__end__"
    # - executor 返回 goto="monitor"
    # - monitor 返回 goto="executor" 或 "reporter"
    # - reporter 返回 goto="__end__"
    
    return builder.compile()


# 为了兼容原有的导入方式，提供一个别名
build_graph = build_distributed_graph
