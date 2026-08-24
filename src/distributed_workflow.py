"""
分布式 Agent 调度器的工作流入口

复用原 LangManus 的 workflow.py 结构，但替换为分布式版本
"""
import logging
import asyncio
from typing import Any, Callable, Dict, Optional
from src.graph.distributed_builder import build_distributed_graph
from src.graph.magentic_builder import build_magentic_graph
from src.graph.adaptive_orchestrator import evaluate_task_complexity
from src.service.viz_bus import get_viz_bus

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
    viz_enabled: bool = True,
    workflow_id: Optional[str] = None,
    state_callback: Optional[Callable[[Dict[str, Any], str], None]] = None,
    route_instances: Optional[list] = None,
    route_prevalidated: bool = False,
    frozen_plan_signature: Optional[list] = None,
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

    if route_instances is not None and orchestration_mode == "magentic":
        from src.service.workflow_routing import WorkflowRoutingError
        raise WorkflowRoutingError(
            "NON_LINEAR_WORKFLOW: 基于实际实例绑定的应用工作流不支持 Magentic 动态拓扑"
        )
    
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

        # 跨主体编排状态（§2.2）
        "cross_host_sessions": {},
        "session_timeout_seconds": timeout_seconds,
        "failed_cross_host_tasks": [],
        "failed_remote_aoe_urls": {},

        # 应用层 Skills 指引
        "skills_content": skills_content,
        # 固定拓扑（Pipeline 模式，非空时 Planner 直接使用，跳过 LLM）
        "pipeline_topology": pipeline_topology,
        "planning_preview": False,
        "route_binding_required": route_instances is not None and not route_prevalidated,
        "route_prevalidated": route_prevalidated,
        "route_instances": list(route_instances or []),
        "frozen_plan_signature": list(frozen_plan_signature or []),

        # Magentic-One 相关字段（如果使用）
        "magentic_round": 0,
        "magentic_stall_count": 0,
        "magentic_max_round": 20,
        "magentic_max_stall": 3,
        "magentic_mode": "inner_loop",
        "progress_ledger": None,
        "reset_count": 0
    }
    
    # ========== 注册到可视化总线(支持联动可视化页面) ==========
    bus = None
    if viz_enabled:
        bus = get_viz_bus()
        title = (user_input[:60] + "...") if len(user_input) > 60 else user_input
        workflow_id = bus.register(title=title, workflow_id=workflow_id)
        # 把 orchestration_mode/complexity 提前写入,首屏就能展示
        initial_state["orchestration_mode"] = orchestration_mode
        # 如果 viz_enabled 为 True，则将 initial_state 推送至 VizBus，供
        # 前端首屏展示。当 viz_enabled=False（例如被调度器禁用独立可视化
        # 时），则不在 VizBus 注册/推送，这样列表不会被频繁刷屏。
        bus.update_state(workflow_id, initial_state, node_name="__init__")
    if state_callback:
        state_callback(dict(initial_state), "__init__")

    # 执行工作流(改为 astream,逐节点推送 state)
    try:
        config = {"recursion_limit": 50} if orchestration_mode == "magentic" else {}
        last_state = dict(initial_state)
        async for chunk in graph.astream(initial_state, config=config):
            # chunk 形如 { "node_name": <updated_state_or_partial> }
            for node_name, node_state in chunk.items():
                if isinstance(node_state, dict):
                    # LangGraph 给的可能是节点返回的 partial,也可能是完整 state
                    last_state.update(node_state)
                # 推送更新：
                # - 当 viz_enabled=True 时，把逐节点的完整/部分 state 推给 VizBus，
                #   前端即可实时收到 execution_plan / current_task_index 等信息。
                # - 不论 viz_enabled, 如果调用方传入了 state_callback（例如
                #   WorkflowScheduler），都会触发回调以便调度器能把子运行的
                #  状态合并到 schedule 汇总视图中。
                if bus and workflow_id:
                    bus.update_state(workflow_id, last_state, node_name=node_name)
                if state_callback:
                    state_callback(dict(last_state), node_name)

        result = last_state
        result["orchestration_mode"] = orchestration_mode
        result["complexity_level"] = complexity if adaptive_mode else "unknown"
        if bus and workflow_id:
            bus.finish(workflow_id, status="done", final_state=result)
        if state_callback:
            state_callback(dict(result), "__finish__")

        if workflow_id:
            logger.info(f"✅ 工作流执行完成 [wf_id={workflow_id}], 使用模式: {orchestration_mode.upper()}")
        else:
            logger.info(f"✅ 工作流执行完成 [wf_id=disabled], 使用模式: {orchestration_mode.upper()}")
        logger.debug(f"最终状态：{result}")
        return result
    except Exception as e:
        try:
            sessions = (last_state if "last_state" in locals() else initial_state).get(
                "cross_host_sessions", {}
            )
            if sessions:
                from src.graph.distributed_nodes import _cleanup_registered_remote_workflows
                await _cleanup_registered_remote_workflows(sessions, timeout_seconds)
        except Exception as cleanup_error:
            logger.warning("跨主体注册资源清理失败: %s", cleanup_error)
        if workflow_id:
            logger.error(f"工作流执行出错 [wf_id={workflow_id}]：{e}", exc_info=True)
        else:
            logger.error(f"工作流执行出错 [wf_id=disabled]：{e}", exc_info=True)
        if bus and workflow_id:
            bus.finish(workflow_id, status="failed", error=str(e))
        if state_callback:
            snapshot = dict(last_state) if "last_state" in locals() else {}
            snapshot["error"] = str(e)
            state_callback(snapshot, "__error__")
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
