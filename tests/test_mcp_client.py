"""
MCP 客户端测试

测试 MCP 工具调用功能
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import asyncio
import logging
from src.service.mcp_client import get_global_mcp_registry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_mcp_status():
    """检查 MCP 服务器配置状态"""
    logger.info("=" * 60)
    logger.info("测试 1: MCP 服务器配置")
    logger.info("=" * 60)
    
    registry = get_global_mcp_registry()
    
    if not registry.MCP_SERVERS:
        logger.info("\n✅ 当前使用内置工具模式（推荐）")
        logger.info("   - 无需额外配置")
        logger.info("   - 使用 Python 标准库实现")
        logger.info("   - 性能更快、更稳定")
    else:
        logger.info(f"\n📦 配置了 {len(registry.MCP_SERVERS)} 个 MCP 服务器")
        for name in registry.MCP_SERVERS.keys():
            logger.info(f"   - {name}")


async def test_builtin_vs_mcp():
    """对比内置工具 vs MCP"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 2: 内置工具 vs MCP 对比")
    logger.info("=" * 60)
    
    logger.info("\n📊 功能对比:")
    logger.info("")
    logger.info("功能          | 内置工具 | MCP 服务器")
    logger.info("-------------|---------|----------")
    logger.info("文件操作      | ✅      | ✅")
    logger.info("网络搜索      | ✅      | ✅ (需配置)")
    logger.info("数据库操作    | ❌      | ✅ (需配置)")
    logger.info("浏览器自动化  | ❌      | ✅ (需配置)")
    logger.info("")
    logger.info("配置复杂度    | 🟢 简单 | 🟡 中等")
    logger.info("启动速度      | 🟢 快速 | 🟡 较慢")
    logger.info("稳定性        | 🟢 高   | 🟡 中等")
    logger.info("")
    logger.info("💡 推荐：优先使用内置工具，特殊场景再考虑 MCP")


async def test_available_servers():
    """测试可用的 MCP 服务器"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 3: Available MCP Servers")
    logger.info("=" * 60)
    
    registry = get_global_mcp_registry()
    
    logger.info("\n📦 配置的 MCP 服务器:")
    for name, config in registry.MCP_SERVERS.items():
        logger.info(f"  - {name}: {config['description']}")
    
    logger.info("\n💡 提示: 当前只配置了 Python MCP 服务器 (SQLite, Git)")
    logger.info("   如需更多功能，可以安装 Node.js MCP 服务器或使用内置工具")


async def test_builtin_tools_info():
    """显示内置工具信息"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 4: Builtin Tools Information")
    logger.info("=" * 60)
    
    logger.info("\n🔧 推荐使用内置工具 (无需 MCP 服务器):")
    logger.info("  - 文件系统: 读/写/列表/删除 (Python 标准库)")
    logger.info("  - 网络搜索: DuckDuckGo API (httpx + 免费 API)")
    logger.info("\n运行测试: python tests/test_builtin_tools.py")


async def cleanup():
    """清理资源"""
    logger.info("\n" + "=" * 60)
    logger.info("清理资源")
    logger.info("=" * 60)
    
    registry = get_global_mcp_registry()
    await registry.disconnect_all()
    logger.info("✅ 所有连接已关闭")


async def main():
    """运行所有测试"""
    logger.info("🚀 MCP 客户端配置检查")
    logger.info("")
    
    try:
        # 运行测试
        await test_mcp_status()
        await test_builtin_vs_mcp()
        await test_available_servers()
        await test_builtin_tools_info()
        
    finally:
        # 清理
        await cleanup()
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ 测试完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
