"""
MCP 集成示例：完整的分布式任务执行流程

演示：
1. 用户请求 → LangGraph 调度器
2. 任务分解 → Agent 匹配
3. 智能协议选择（MCP vs A2A）
4. 任务执行 → 结果聚合
"""

import asyncio
import logging
from src.graph.distributed_builder import build_distributed_graph
from src.graph.distributed_types import DistributedState
from langchain_core.messages import HumanMessage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def demo_search_with_mcp():
    """
    演示 1: 搜索任务自动使用 MCP
    
    预期流程:
    用户请求 → Planner 分解 → Executor 匹配 MCP → Brave Search API → 返回结果
    """
    logger.info("\n" + "=" * 80)
    logger.info("演示 1: 搜索任务 (MCP 优先)")
    logger.info("=" * 80 + "\n")
    
    # 构建分布式图
    graph = build_distributed_graph()
    
    # 用户请求
    user_query = "搜索 LangGraph 最新功能和使用方法"
    
    initial_state: DistributedState = {
        "messages": [HumanMessage(content=user_query)],
        "plan_generated": False,
        "execution_plan": [],
        "current_task_index": 0,
        "task_results": [],
        "all_tasks_completed": False
    }
    
    # 执行图
    logger.info(f"📝 用户请求: {user_query}\n")
    
    async for event in graph.astream(initial_state, stream_mode="updates"):
        for node_name, node_output in event.items():
            logger.info(f"📍 节点 [{node_name}] 执行完成")
            
            # 打印关键信息
            if node_name == "planner":
                plan = node_output.get("execution_plan", [])
                logger.info(f"   生成了 {len(plan)} 个子任务")
                
            elif node_name == "executor":
                results = node_output.get("task_results", [])
                if results:
                    last_result = results[-1]
                    logger.info(f"   协议: {last_result.get('protocol', 'unknown')}")
                    logger.info(f"   状态: {last_result.get('status', 'unknown')}")
            
            logger.info("")
    
    logger.info("=" * 80)
    logger.info("✅ 演示 1 完成")
    logger.info("=" * 80 + "\n")


async def demo_file_operations_with_mcp():
    """
    演示 2: 文件操作自动使用 MCP
    
    预期流程:
    用户请求 → Planner 分解 → Executor 匹配 MCP → Filesystem Tool → 返回结果
    """
    logger.info("\n" + "=" * 80)
    logger.info("演示 2: 文件操作 (MCP 优先)")
    logger.info("=" * 80 + "\n")
    
    graph = build_distributed_graph()
    
    user_query = """
    完成以下任务：
    1. 在 /tmp 目录创建一个测试文件 test_mcp.txt
    2. 写入内容 "Hello MCP from LangGraph!"
    3. 读取文件并确认内容
    """
    
    initial_state: DistributedState = {
        "messages": [HumanMessage(content=user_query)],
        "plan_generated": False,
        "execution_plan": [],
        "current_task_index": 0,
        "task_results": [],
        "all_tasks_completed": False
    }
    
    logger.info(f"📝 用户请求:\n{user_query}\n")
    
    async for event in graph.astream(initial_state, stream_mode="updates"):
        for node_name, node_output in event.items():
            logger.info(f"📍 节点 [{node_name}] 执行完成")
            
            if node_name == "executor":
                results = node_output.get("task_results", [])
                if results:
                    last_result = results[-1]
                    task_info = last_result.get("task_info", {})
                    logger.info(f"   任务: {task_info.get('task_title', 'unknown')}")
                    logger.info(f"   协议: {last_result.get('protocol', 'unknown')}")
                    logger.info(f"   状态: {last_result.get('status', 'unknown')}")
                    
                    # 打印文件内容
                    if "读取" in task_info.get('task_title', ''):
                        content = last_result.get("result", "")
                        logger.info(f"   文件内容: {content}")
            
            logger.info("")
    
    logger.info("=" * 80)
    logger.info("✅ 演示 2 完成")
    logger.info("=" * 80 + "\n")


async def demo_mixed_tasks():
    """
    演示 3: 混合任务（MCP + A2A）
    
    预期流程:
    - 搜索任务 → MCP (Brave Search)
    - 数据分析任务 → A2A (Compute Agent)
    - 文件保存任务 → MCP (Filesystem)
    """
    logger.info("\n" + "=" * 80)
    logger.info("演示 3: 混合任务 (MCP + A2A 智能路由)")
    logger.info("=" * 80 + "\n")
    
    graph = build_distributed_graph()
    
    user_query = """
    请帮我完成以下分析任务：
    1. 搜索 "Python 异步编程最佳实践"
    2. 分析搜索结果，总结 3 个关键要点
    3. 将总结保存到 /tmp/python_async_summary.txt
    """
    
    initial_state: DistributedState = {
        "messages": [HumanMessage(content=user_query)],
        "plan_generated": False,
        "execution_plan": [],
        "current_task_index": 0,
        "task_results": [],
        "all_tasks_completed": False
    }
    
    logger.info(f"📝 用户请求:\n{user_query}\n")
    logger.info("预期执行流程:")
    logger.info("  任务1: 搜索 → MCP (Brave Search)")
    logger.info("  任务2: 分析 → A2A (NLP Agent)")
    logger.info("  任务3: 保存 → MCP (Filesystem)\n")
    
    async for event in graph.astream(initial_state, stream_mode="updates"):
        for node_name, node_output in event.items():
            logger.info(f"📍 节点 [{node_name}] 执行完成")
            
            if node_name == "executor":
                results = node_output.get("task_results", [])
                if results:
                    last_result = results[-1]
                    task_info = last_result.get("task_info", {})
                    protocol = last_result.get("protocol", "unknown")
                    
                    logger.info(f"   任务 {len(results)}: {task_info.get('task_title', 'unknown')}")
                    logger.info(f"   协议: {protocol}")
                    logger.info(f"   状态: {last_result.get('status', 'unknown')}")
                    
                    # 高亮协议选择
                    if protocol == "mcp":
                        logger.info(f"   ✨ 使用 MCP 工具: {last_result.get('tool_used', 'unknown')}")
                    elif protocol == "a2a":
                        logger.info(f"   🤖 使用 A2A Agent: {last_result.get('agent_used', 'unknown')}")
            
            logger.info("")
    
    logger.info("=" * 80)
    logger.info("✅ 演示 3 完成")
    logger.info("=" * 80 + "\n")


async def demo_mcp_fallback():
    """
    演示 4: MCP 失败后自动降级到 A2A
    
    预期流程:
    用户请求 → Executor 尝试 MCP → MCP 失败 → 自动降级到 A2A → 返回结果
    """
    logger.info("\n" + "=" * 80)
    logger.info("演示 4: 自动降级机制 (MCP → A2A)")
    logger.info("=" * 80 + "\n")
    
    graph = build_distributed_graph()
    
    # 故意使用一个可能触发 MCP 但又可能失败的请求
    user_query = "搜索并总结最近 AI 领域的突破性进展（需要深度分析）"
    
    initial_state: DistributedState = {
        "messages": [HumanMessage(content=user_query)],
        "plan_generated": False,
        "execution_plan": [],
        "current_task_index": 0,
        "task_results": [],
        "all_tasks_completed": False
    }
    
    logger.info(f"📝 用户请求: {user_query}\n")
    logger.info("说明: 这个任务会先尝试 MCP，如果 MCP 不可用或失败，会自动降级到 A2A\n")
    
    async for event in graph.astream(initial_state, stream_mode="updates"):
        for node_name, node_output in event.items():
            logger.info(f"📍 节点 [{node_name}] 执行完成")
            
            if node_name == "executor":
                results = node_output.get("task_results", [])
                if results:
                    last_result = results[-1]
                    logger.info(f"   协议: {last_result.get('protocol', 'unknown')}")
                    logger.info(f"   状态: {last_result.get('status', 'unknown')}")
                    
                    # 检查是否发生了降级
                    if last_result.get("protocol") == "a2a":
                        logger.info("   ⚠️  发生了协议降级: MCP → A2A")
            
            logger.info("")
    
    logger.info("=" * 80)
    logger.info("✅ 演示 4 完成")
    logger.info("=" * 80 + "\n")


async def main():
    """运行所有演示"""
    logger.info("\n" + "🚀" * 40)
    logger.info("MCP 集成完整演示")
    logger.info("🚀" * 40 + "\n")
    
    logger.info("📋 演示内容:")
    logger.info("  1. 搜索任务 (MCP Brave Search)")
    logger.info("  2. 文件操作 (MCP Filesystem)")
    logger.info("  3. 混合任务 (MCP + A2A 智能路由)")
    logger.info("  4. 自动降级 (MCP 失败 → A2A)\n")
    
    logger.info("⚠️  注意事项:")
    logger.info("  - 确保已安装 MCP 服务器: ./install_mcp_servers.sh")
    logger.info("  - 配置 Brave API Key: export BRAVE_API_KEY='...'")
    logger.info("  - 启动 L3 Agent 节点: python src/agents/l3_agent_node.py\n")
    
    input("按 Enter 开始演示...")
    
    try:
        # 演示 1: 搜索
        await demo_search_with_mcp()
        input("\n按 Enter 继续下一个演示...")
        
        # 演示 2: 文件操作
        await demo_file_operations_with_mcp()
        input("\n按 Enter 继续下一个演示...")
        
        # 演示 3: 混合任务
        await demo_mixed_tasks()
        input("\n按 Enter 继续下一个演示...")
        
        # 演示 4: 自动降级
        await demo_mcp_fallback()
        
    except KeyboardInterrupt:
        logger.info("\n\n⚠️  演示被中断")
    except Exception as e:
        logger.error(f"\n\n❌ 演示出错: {e}", exc_info=True)
    
    logger.info("\n" + "🎉" * 40)
    logger.info("演示结束")
    logger.info("🎉" * 40 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
