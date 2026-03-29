"""
Magentic-One 编排节点

完整实现 Magentic-One 的编排逻辑：
- Outer Loop: 规划阶段
- Inner Loop: 执行阶段
- Progress Ledger: 每轮进度分析
- Dynamic Agent Selection: 动态选择下一个 Agent
"""

from typing import Literal
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import Command

from .distributed_types import DistributedState
from .progress_ledger import (
    create_progress_ledger, 
    extract_next_speaker,
    is_task_completed,
    is_stalling
)
from src.agents.llm import get_llm_by_type
import logging

logger = logging.getLogger(__name__)


# ===== Magentic-One 节点 =====

def magentic_planner_node(state: DistributedState) -> Command[Literal["magentic_orchestrator"]]:
    """
    Magentic-One 规划节点（Outer Loop）
    
    与 Sequential 模式不同，Magentic-One 不需要预先规划所有任务。
    这里只做两件事：
    1. 从 Agent Registry 加载可用 Agent
    2. 初始化 Magentic-One 状态变量
    
    实际的任务规划由 Orchestrator 动态完成。
    """
    logger.info("=== Magentic Planner (Outer Loop) ===")
    logger.info("📝 Magentic-One 模式：跳过预规划，直接进入动态编排")
    
    # 1. 查询 Agent Registry
    from src.service.agent_registry import get_registry_client
    registry_client = get_registry_client()
    available_agents = registry_client.query_agents()
    
    if not available_agents:
        logger.error("❌ 没有可用的 Agent，无法启动 Magentic-One")
        return Command(
            update={
                "messages": state["messages"] + [HumanMessage(
                    content="错误：当前没有可用的 Agent，无法执行任务。",
                    name="planner"
                )],
                "all_tasks_completed": True
            },
            goto="__end__"
        )
    
    # 2. 只初始化 Magentic 状态，不生成 execution_plan
    update = {
        "agent_registry_cache": available_agents,
        "registry_last_update": datetime.now().isoformat(),
        "execution_plan": [],  # Magentic-One 不使用预定义计划
        "plan_generated": False,  # 标记为未使用传统规划
        "magentic_round": 0,
        "magentic_stall_count": 0,
        "magentic_mode": "inner_loop",
        "progress_ledger": None
    }
    
    logger.info(f"✅ 已加载 {len(available_agents)} 个可用 Agent")
    
    # 生成 UI 展示日志
    try:
        ui_log = "任务分解：\n使用 Magentic-One 动态编排模式\n将根据执行情况动态选择 Agent"
        logger.info(f"UI_LOG_EVENT:{ui_log}")
    except Exception as e:
        logger.warning(f"生成 UI 日志失败: {e}")
    
    return Command(update=update, goto="magentic_orchestrator")


async def magentic_orchestrator_node(state: DistributedState) -> Command[Literal["magentic_executor", "magentic_reporter", "__end__"]]:
    """
    Magentic-One 编排节点（Inner Loop）
    
    核心逻辑：
    1. 生成 Progress Ledger（5 维度分析）
    2. 判断任务是否完成
    3. 检测是否停滞（循环/无进展）
    4. 动态选择下一个 Agent
    """
    logger.info("=== Magentic Orchestrator (Inner Loop) ===")
    
    # 增加轮次
    round_count = state.get("magentic_round", 0) + 1
    stall_count = state.get("magentic_stall_count", 0)
    max_round = state.get("magentic_max_round", 20)
    max_stall = state.get("magentic_max_stall", 3)
    
    logger.info(f"📊 Round {round_count}/{max_round}, Stall {stall_count}/{max_stall}")
    
    # 检查是否超过最大轮次
    if round_count > max_round:
        logger.warning("⚠️  达到最大轮次，任务终止")
        return Command(
            update={
                "all_tasks_completed": True,
                "messages": [HumanMessage(
                    content=f"警告：已达到最大轮次（{max_round}），任务未完成。",
                    name="orchestrator"
                )]
            },
            goto="magentic_reporter"
        )
    
    # 1. 生成 Progress Ledger
    try:
        progress_ledger = await create_progress_ledger(state)
        
        logger.info(f"📋 Progress Ledger 生成成功")
        logger.debug(f"  - 任务完成: {progress_ledger['is_request_satisfied']['answer']}")
        logger.debug(f"  - 陷入循环: {progress_ledger['is_in_loop']['answer']}")
        logger.debug(f"  - 有进展: {progress_ledger['is_progress_being_made']['answer']}")
        logger.debug(f"  - 下一个: {progress_ledger['next_speaker']['answer']}")
        
    except Exception as e:
        logger.error(f"❌ Progress Ledger 生成失败: {e}")
        # 失败时触发重置
        return await _reset_and_replan(state, reason=f"Progress Ledger 生成失败: {e}")
    
    # 2. 判断任务是否完成
    if is_task_completed(progress_ledger):
        logger.info("✅ 任务已完成")
        return Command(
            update={
                "all_tasks_completed": True,
                "progress_ledger": progress_ledger
            },
            goto="magentic_reporter"
        )
    
    # 3. 检测停滞
    if is_stalling(progress_ledger):
        stall_count += 1
        logger.warning(f"⚠️  检测到停滞（第 {stall_count} 次）")
    else:
        stall_count = max(0, stall_count - 1)  # 有进展则减少停滞计数
    
    # 4. 停滞过多，触发重置和重规划
    if stall_count >= max_stall:
        logger.warning(f"🔄 停滞次数过多（{stall_count}），触发重置和重规划")
        return await _reset_and_replan(state, reason="停滞检测")
    
    # 5. 动态选择下一个 Agent
    next_agent_id = extract_next_speaker(progress_ledger)
    instruction = str(progress_ledger["instruction_or_question"]["answer"])
    
    logger.info(f"🎯 选择下一个 Agent: {next_agent_id}")
    logger.info(f"📝 指令: {instruction[:100]}...")
    
    # 生成 UI 展示日志（显示当前轮次的分配）
    try:
        ui_log = f"任务分解：\nRound {round_count} - {next_agent_id}：{instruction[:60]}..."
        logger.info(f"UI_LOG_EVENT:{ui_log}")
    except Exception as e:
        logger.warning(f"生成 UI 日志失败: {e}")
    
    # 查找 Agent 信息
    agent_info = None
    for agent in state.get("agent_registry_cache", []):
        if agent["id"] == next_agent_id:
            agent_info = agent
            break
    
    if not agent_info:
        logger.error(f"❌ Agent {next_agent_id} 不存在")
        return Command(
            update={"all_tasks_completed": True},
            goto="magentic_reporter"
        )
    
    # 6. 创建新任务并分配给选中的 Agent
    from .distributed_types import TaskAssignment
    import uuid
    
    new_task: TaskAssignment = {
        "task_id": f"magentic_task_{uuid.uuid4().hex[:8]}",
        "task_title": f"Round {round_count} - {next_agent_id}",
        "task_description": instruction,
        "assigned_agent_id": agent_info["id"],
        "target_ip": agent_info["ip"],
        "target_port": agent_info["port"],
        "status": "pending",
        "result": "",
        "retry_count": 0
    }
    
    # 更新状态
    execution_plan = state.get("execution_plan", [])
    execution_plan.append(new_task)
    
    # 添加编排器的指令到对话历史
    instruction_msg = AIMessage(
        content=f"[Orchestrator → {next_agent_id}] {instruction}",
        name="orchestrator"
    )
    
    return Command(
        update={
            "execution_plan": execution_plan,
            "current_task_index": len(execution_plan) - 1,
            "magentic_round": round_count,
            "magentic_stall_count": stall_count,
            "progress_ledger": progress_ledger,
            "messages": state["messages"] + [instruction_msg]
        },
        goto="magentic_executor"
    )


async def magentic_executor_node(state: DistributedState) -> Command[Literal["magentic_orchestrator", "magentic_reporter"]]:
    """
    Magentic-One 执行节点
    
    执行当前任务，然后返回 Orchestrator 进行下一轮决策
    """
    logger.info("=== Magentic Executor ===")
    
    # 复用原有的 executor 逻辑
    from .distributed_nodes import distributed_executor_node
    
    result = await distributed_executor_node(state)
    
    # 执行完成后，返回 Orchestrator 继续循环
    update = result.update or {}
    
    return Command(
        update=update,
        goto="magentic_orchestrator"
    )


def magentic_reporter_node(state: DistributedState) -> Command[Literal["__end__"]]:
    """
    Magentic-One 报告节点
    
    生成最终报告
    """
    logger.info("=== Magentic Reporter ===")
    
    # 复用原有的 reporter 逻辑
    from .distributed_nodes import distributed_reporter_node
    
    result = distributed_reporter_node(state)
    
    return Command(
        update=result.update,
        goto="__end__"
    )


# ===== 辅助函数 =====

async def _reset_and_replan(state: DistributedState, reason: str) -> Command[Literal["magentic_planner", "magentic_reporter"]]:
    """
    重置并重新规划
    
    类似 Magentic-One 的 reset_and_replan 逻辑
    """
    reset_count = state.get("reset_count", 0) + 1
    max_resets = 3  # 最多重置 3 次
    
    logger.info(f"🔄 执行重置和重规划（第 {reset_count} 次），原因: {reason}")
    
    if reset_count > max_resets:
        logger.error(f"❌ 已达到最大重置次数（{max_resets}），任务失败")
        return Command(
            update={
                "all_tasks_completed": False,
                "failed_tasks": [{"reason": f"重置次数过多: {reason}"}],
                "reset_count": reset_count
            },
            goto="magentic_reporter"
        )
    
    # 1. 清空对话历史（保留初始任务）
    initial_message = state["messages"][0]
    
    # 2. 重新生成计划
    # 这里可以调用 LLM 重新分析失败原因并生成新计划
    
    # 3. 重置计数器，回到 planner 重新规划
    return Command(
        update={
            "messages": [initial_message],
            "execution_plan": [],
            "current_task_index": 0,
            "magentic_round": 0,
            "magentic_stall_count": 0,
            "reset_count": reset_count
        },
        goto="magentic_planner"  # 回到 planner 重新规划
    )
