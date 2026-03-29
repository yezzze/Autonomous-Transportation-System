"""
分布式 Agent 调度器的工作流入口

复用原 LangManus 的 workflow.py 结构，但替换为分布式版本
"""
import logging
import asyncio
from src.graph.distributed_builder import build_distributed_graph
from src.graph.magentic_builder import build_magentic_graph
from src.graph.adaptive_orchestrator import evaluate_task_complexity

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def enable_debug_logging():
    """启用调试级别日志"""
    logging.getLogger("src").setLevel(logging.DEBUG)


async def run_distributed_workflow(
    user_input: str,
    debug: bool = False,
    max_retries: int = 3,
    timeout_seconds: int = 30,
    replanning_enabled: bool = True,
    max_replanning: int = 2,
    adaptive_mode: bool = True,  # ← 新增：是否启用自适应编排
    skills_content: str = "",   # ← 应用 Skills 指引（注入 planner system_prompt）
    pipeline_topology: list = [],# ← 固定拓扑（非空时跳过 LLM Planner）
):
    """
    运行分布式 Agent 调度工作流（支持自适应编排）
    
    Args:
        user_input: 用户输入的请求
        debug: 是否启用调试模式
        max_retries: 远程调用最大重试次数
        timeout_seconds: 远程调用超时时间（秒）
        replanning_enabled: 是否启用智能重规划（默认 True）
        max_replanning: 最大重新规划次数（默认 2）
        adaptive_mode: 是否启用自适应编排（默认 True）
    
    Returns:
        最终状态，包含执行结果
    
    编排模式：
    - adaptive_mode=True: 根据任务复杂度自动选择
      - Simple → Sequential（串行）
      - Medium → Concurrent（并行，暂未实现，降级为 Sequential）
      - Complex → Magentic-One（动态反馈循环）
    - adaptive_mode=False: 固定使用 Sequential + 混合重规划
    """
    if not user_input:
        raise ValueError("用户输入不能为空")

    if debug:
        enable_debug_logging()

    # ========== Pipeline 模式：直接跳过复杂度评估 ==========
    if pipeline_topology:
        logger.info(f"⚡ Pipeline 模式：跳过复杂度评估，固定拓扑 {len(pipeline_topology)} 步")
        complexity = "pipeline"
        orchestration_mode = "sequential"
        graph = build_distributed_graph()
    # ========== 自适应编排：评估任务复杂度 ==========
    elif adaptive_mode:
        logger.info("🔍 启用自适应编排模式，正在评估任务复杂度...")
        complexity = await evaluate_task_complexity(user_input)
        logger.info(f"📊 任务复杂度评估结果: {complexity.upper()}")
        
        # 根据复杂度选择编排模式
        if complexity == "simple":
            orchestration_mode = "sequential"
            logger.info("✅ 选择编排模式: Sequential (串行)")
            graph = build_distributed_graph()
        elif complexity == "medium":
            orchestration_mode = "sequential"  # Concurrent 暂未实现
            logger.warning("⚠️  Concurrent 模式暂未实现，降级为 Sequential")
            graph = build_distributed_graph()
        else:  # complex
            orchestration_mode = "magentic"
            logger.info("✅ 选择编排模式: Magentic-One (动态反馈)")
            graph = build_magentic_graph()
    else:
        # 固定模式
        orchestration_mode = "sequential"
        logger.info(f"启动分布式工作流（混合模式），用户请求：{user_input}")
        logger.info(f"重规划设置：enabled={replanning_enabled}, max={max_replanning}")
        graph = build_distributed_graph()
    
    # 初始化状态
    initial_state = {
        # 用户消息
        "messages": [{"role": "user", "content": user_input}],
        
        # 配置参数
        "max_retries": max_retries,
        "timeout_seconds": timeout_seconds,
        
        # 初始化运行时状态
        "agent_registry_cache": [],
        "execution_plan": [],
        "current_task_index": 0,
        "plan_generated": False,
        "all_tasks_completed": False,
        "failed_tasks": [],
        "registry_last_update": "",
        "next": "planner",
        
        # 混合编排模式参数
        "replanning_count": 0,
        "max_replanning": max_replanning,
        "replanning_enabled": replanning_enabled,
        "last_replan_reason": "",
        
        # 自适应编排信息
        "complexity_level": complexity if adaptive_mode else "unknown",
        "orchestration_mode": orchestration_mode,

        # 应用层 Skills 指引
        "skills_content": skills_content,
        # 固定拓扑（Pipeline 模式，非空时 Planner 直接使用，跳过 LLM）
        "pipeline_topology": pipeline_topology,
        
        # Magentic-One 相关字段（如果使用）
        "magentic_round": 0,
        "magentic_stall_count": 0,
        "magentic_max_round": 20,
        "magentic_max_stall": 3,
        "magentic_mode": "inner_loop",
        "progress_ledger": None,
        "reset_count": 0
    }
    
    # 执行工作流
    try:
        # Magentic-One 和 Sequential 都使用异步调用（因为 executor 节点现在是异步的）
        if orchestration_mode == "magentic":
            result = await graph.ainvoke(
                initial_state,
                config={"recursion_limit": 50}  # 增加递归限制
            )
        else:
            # Sequential 也使用异步调用（executor 节点已改为异步）
            result = await graph.ainvoke(initial_state)
            
        logger.info(f"✅ 工作流执行完成，使用模式: {orchestration_mode.upper()}")
        logger.debug(f"最终状态：{result}")
        
        # 添加编排模式信息到结果
        result["orchestration_mode"] = orchestration_mode
        result["complexity_level"] = complexity if adaptive_mode else "unknown"
        
        return result
    except Exception as e:
        logger.error(f"工作流执行出错：{e}", exc_info=True)
        raise


def visualize_graph():
    """
    可视化分布式 Graph 结构
    
    Returns:
        Mermaid 格式的图表字符串
    """
    try:
        return distributed_graph.get_graph().draw_mermaid()
    except Exception as e:
        logger.error(f"可视化失败：{e}")
        return f"Error: {e}"


if __name__ == "__main__":
    # 打印 Graph 结构
    print("=== 分布式 Agent 调度器 Graph 结构 ===\n")
    print(visualize_graph())
    print("\n" + "="*50 + "\n")
    
    # 示例运行
    test_query = "帮我搜索 DeepSeek R1 模型的相关信息，并计算它的影响力指数"
    print(f"测试查询：{test_query}\n")
    
    result = run_distributed_workflow(
        user_input=test_query,
        debug=True
    )
    
    # 打印结果
    print("\n=== 执行结果 ===")
    for msg in result["messages"]:
        print(f"\n[{msg.type.upper()}]")
        print(msg.content)
        print("-" * 50)
