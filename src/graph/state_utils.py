"""
State 初始化工具函数
"""
from typing import Dict, Any, List
from datetime import datetime
from .distributed_types import DistributedState


def create_magentic_state(
    user_query: str,
    max_round: int = 20,
    max_stall: int = 3,
    **kwargs
) -> Dict[str, Any]:
    """
    创建 Magentic-One 编排的初始状态
    
    Args:
        user_query: 用户查询
        max_round: 最大轮次
        max_stall: 最大停滞次数
        **kwargs: 其他状态字段
        
    Returns:
        初始化的状态字典
    """
    state: Dict[str, Any] = {
        # MessagesState 字段
        "messages": [{"role": "user", "content": user_query}],
        
        # 常量配置
        "max_retries": kwargs.get("max_retries", 3),
        "timeout_seconds": kwargs.get("timeout_seconds", 300),
        
        # Agent 注册表
        "agent_registry_cache": kwargs.get("agent_registry_cache", []),
        "registry_last_update": datetime.now().isoformat(),
        
        # 执行计划
        "execution_plan": [],
        "current_task_index": 0,
        
        # 执行状态
        "plan_generated": False,
        "all_tasks_completed": False,
        "failed_tasks": [],
        
        # 混合编排
        "replanning_count": 0,
        "max_replanning": kwargs.get("max_replanning", 2),
        "replanning_enabled": kwargs.get("replanning_enabled", True),
        "last_replan_reason": "",
        
        # Magentic-One 编排
        "magentic_round": 0,
        "magentic_stall_count": 0,
        "magentic_max_round": max_round,
        "magentic_max_stall": max_stall,
        "magentic_mode": "inner_loop",  # 默认 inner loop
        "progress_ledger": None,
        "reset_count": 0,
        "complexity_level": "unknown",
        
        # 路由控制
        "next": "planner",
    }
    
    # 合并额外参数
    state.update(kwargs)
    return state


def create_distributed_state(
    user_query: str,
    **kwargs
) -> Dict[str, Any]:
    """
    创建标准分布式编排的初始状态
    
    Args:
        user_query: 用户查询
        **kwargs: 其他状态字段
        
    Returns:
        初始化的状态字典
    """
    state: Dict[str, Any] = {
        # MessagesState 字段
        "messages": [{"role": "user", "content": user_query}],
        
        # 常量配置
        "max_retries": kwargs.get("max_retries", 3),
        "timeout_seconds": kwargs.get("timeout_seconds", 300),
        
        # Agent 注册表
        "agent_registry_cache": kwargs.get("agent_registry_cache", []),
        "registry_last_update": datetime.now().isoformat(),
        
        # 执行计划
        "execution_plan": [],
        "current_task_index": 0,
        
        # 执行状态
        "plan_generated": False,
        "all_tasks_completed": False,
        "failed_tasks": [],
        
        # 混合编排
        "replanning_count": 0,
        "max_replanning": kwargs.get("max_replanning", 2),
        "replanning_enabled": kwargs.get("replanning_enabled", True),
        "last_replan_reason": "",
        
        # Magentic-One 字段（未启用）
        "magentic_round": 0,
        "magentic_stall_count": 0,
        "magentic_max_round": 0,
        "magentic_max_stall": 0,
        "magentic_mode": "",
        "progress_ledger": None,
        "reset_count": 0,
        "complexity_level": "unknown",
        
        # 路由控制
        "next": "planner",
    }
    
    # 合并额外参数
    state.update(kwargs)
    return state


def is_magentic_mode(state: DistributedState) -> bool:
    """
    判断当前是否启用 Magentic-One 模式
    
    Args:
        state: 状态对象
        
    Returns:
        是否启用 Magentic-One
    """
    return state.get("magentic_max_round", 0) > 0


def get_magentic_status(state: DistributedState) -> Dict[str, Any]:
    """
    获取 Magentic-One 的运行状态
    
    Args:
        state: 状态对象
        
    Returns:
        状态摘要
    """
    return {
        "round": state.get("magentic_round", 0),
        "max_round": state.get("magentic_max_round", 0),
        "stall_count": state.get("magentic_stall_count", 0),
        "max_stall": state.get("magentic_max_stall", 0),
        "mode": state.get("magentic_mode", ""),
        "reset_count": state.get("reset_count", 0),
        "has_progress_ledger": state.get("progress_ledger") is not None,
        "complexity": state.get("complexity_level", "unknown"),
    }
