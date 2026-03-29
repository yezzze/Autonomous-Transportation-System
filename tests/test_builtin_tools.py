"""
内置工具测试

测试 Python 标准库实现的工具
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import asyncio
import logging
from src.service.builtin_tools import get_builtin_tool_registry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_filesystem():
    """测试文件系统工具"""
    logger.info("=" * 60)
    logger.info("测试 1: 文件系统操作")
    logger.info("=" * 60)
    
    registry = get_builtin_tool_registry()
    
    try:
        # 写入文件
        logger.info("\n📝 写入文件...")
        result = await registry.call_tool(
            "filesystem",
            "write_file",
            {
                "path": "/tmp/builtin_test.txt",
                "content": "这是内置工具测试内容\nHello Builtin Tools!"
            }
        )
        logger.info(f"✅ {result}")
        
        # 读取文件
        logger.info("\n📖 读取文件...")
        content = await registry.call_tool(
            "filesystem",
            "read_file",
            {"path": "/tmp/builtin_test.txt"}
        )
        logger.info(f"✅ 文件内容:\n{content}")
        
        # 列出目录
        logger.info("\n📂 列出目录...")
        items = await registry.call_tool(
            "filesystem",
            "list_directory",
            {"path": "/tmp"}
        )
        logger.info(f"✅ /tmp 目录内容:\n{items}")
        
        # 删除文件
        logger.info("\n🗑️  删除文件...")
        result = await registry.call_tool(
            "filesystem",
            "delete_file",
            {"path": "/tmp/builtin_test.txt"}
        )
        logger.info(f"✅ {result}")
        
    except Exception as e:
        logger.error(f"❌ 测试失败: {e}")


async def test_search():
    """测试搜索工具"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 2: 网络搜索")
    logger.info("=" * 60)
    
    registry = get_builtin_tool_registry()
    
    try:
        logger.info("\n🔍 搜索: Python programming")
        result = await registry.call_tool(
            "search",
            "search",
            {"query": "Python programming", "max_results": 3}
        )
        logger.info(f"✅ 搜索结果:\n{result}")
        
    except Exception as e:
        logger.error(f"❌ 测试失败: {e}")


async def cleanup():
    """清理资源"""
    logger.info("\n" + "=" * 60)
    logger.info("清理资源")
    logger.info("=" * 60)
    
    registry = get_builtin_tool_registry()
    await registry.close()
    logger.info("✅ 资源已清理")


async def main():
    """运行所有测试"""
    logger.info("\n" + "🚀" * 40)
    logger.info("内置工具测试")
    logger.info("🚀" * 40 + "\n")
    
    logger.info("测试使用 Python 标准库实现的工具:")
    logger.info("  - 文件系统操作（读/写/列表/删除）")
    logger.info("  - 网络搜索（DuckDuckGo API）\n")
    
    try:
        # 运行测试
        await test_filesystem()
        await test_search()
        
    finally:
        # 清理
        await cleanup()
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ 测试完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
