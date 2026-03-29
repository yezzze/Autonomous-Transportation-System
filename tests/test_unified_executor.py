"""
统一执行层测试

测试 MCP 和 A2A 协议的智能路由
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import asyncio
import logging
from src.graph.unified_executor import UnifiedExecutor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_search_task():
    """测试搜索任务（应该使用 MCP）"""
    logger.info("=" * 60)
    logger.info("测试 1: 搜索任务 (MCP)")
    logger.info("=" * 60)
    
    executor = UnifiedExecutor()
    
    task = {
        "task_id": "task_001",
        "task_title": "搜索 LangGraph 信息",
        "task_description": "搜索 LangGraph 最新功能和使用方法",
        "assigned_agent_id": "search_agent_001",
        "target_ip": "localhost",
        "target_port": 8001
    }
    
    result = await executor.execute_task(task)
    
    logger.info(f"\n执行结果:")
    logger.info(f"  协议: {result['protocol']}")
    logger.info(f"  状态: {result['status']}")
    logger.info(f"  工具/Agent: {result.get('tool_used') or result.get('agent_used')}")
    if result['status'] == 'success':
        logger.info(f"  结果预览: {result['result'][:200]}...")
    else:
        logger.info(f"  错误: {result.get('error_message')}")


async def test_file_task():
    """测试文件操作任务（应该使用 MCP）"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 2: 文件操作任务 (MCP)")
    logger.info("=" * 60)
    
    executor = UnifiedExecutor()
    
    # 先写入文件
    write_task = {
        "task_id": "task_002",
        "task_title": "写入测试文件",
        "task_description": '写入文件 "/tmp/test_executor.txt"',
        "assigned_agent_id": "file_agent_001",
        "target_ip": "localhost",
        "target_port": 8002
    }
    
    result = await executor.execute_task(write_task)
    logger.info(f"写入结果: {result['status']}")
    
    # 再读取文件
    read_task = {
        "task_id": "task_003",
        "task_title": "读取测试文件",
        "task_description": '读取文件 "/tmp/test_executor.txt"',
        "assigned_agent_id": "file_agent_001",
        "target_ip": "localhost",
        "target_port": 8002
    }
    
    result = await executor.execute_task(read_task)
    
    logger.info(f"\n执行结果:")
    logger.info(f"  协议: {result['protocol']}")
    logger.info(f"  状态: {result['status']}")
    logger.info(f"  工具/Agent: {result.get('tool_used') or result.get('agent_used')}")
    if result['status'] == 'success':
        logger.info(f"  内容: {result['result']}")


async def test_compute_task():
    """测试计算任务（应该使用 A2A）"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 3: 计算任务 (A2A)")
    logger.info("=" * 60)
    
    executor = UnifiedExecutor()
    
    task = {
        "task_id": "task_004",
        "task_title": "数学计算",
        "task_description": "计算斐波那契数列的第 20 项",
        "assigned_agent_id": "compute_agent_001",
        "target_ip": "localhost",
        "target_port": 8003
    }
    
    result = await executor.execute_task(task)
    
    logger.info(f"\n执行结果:")
    logger.info(f"  协议: {result['protocol']}")
    logger.info(f"  状态: {result['status']}")
    logger.info(f"  工具/Agent: {result.get('tool_used') or result.get('agent_used')}")
    logger.info(f"  说明: 计算任务通常需要 Agent 推理，因此使用 A2A")


async def test_mcp_fallback():
    """测试 MCP 失败后回退到 A2A"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 4: MCP 失败回退 (MCP → A2A)")
    logger.info("=" * 60)
    
    executor = UnifiedExecutor()
    
    task = {
        "task_id": "task_005",
        "task_title": "模拟 MCP 失败",
        "task_description": "搜索一个不存在的工具",  # 会匹配 MCP，但可能失败
        "assigned_agent_id": "search_agent_001",
        "target_ip": "localhost",
        "target_port": 8001
    }
    
    result = await executor.execute_task(task)
    
    logger.info(f"\n执行结果:")
    logger.info(f"  协议: {result['protocol']}")
    logger.info(f"  状态: {result['status']}")
    logger.info(f"  工具/Agent: {result.get('tool_used') or result.get('agent_used')}")
    logger.info(f"  说明: 如果 MCP 失败，系统会自动降级到 A2A")


async def main():
    """运行所有测试"""
    logger.info("🚀 开始统一执行层测试")
    logger.info("测试 MCP 和 A2A 协议的智能路由\n")
    
    try:
        await test_search_task()
        await test_file_task()
        await test_compute_task()
        await test_mcp_fallback()
        
    except Exception as e:
        logger.error(f"❌ 测试出错: {e}", exc_info=True)
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ 测试完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
