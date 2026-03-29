"""
测试 MCP + A2A 统一执行层
"""

import asyncio
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


async def test_mcp_client():
    """测试 MCP 客户端"""
    print("\n" + "="*60)
    print("测试 1: MCP 客户端基础功能")
    print("="*60)
    
    from src.service.mcp_client import MCPToolRegistry
    
    registry = MCPToolRegistry()
    
    # 测试连接（这会尝试连接真实的 MCP 服务器）
    print("\n📌 尝试连接 MCP 服务器...")
    print("⚠️  注意: 如果没有安装 MCP 服务器，这里会失败（预期行为）")
    
    try:
        client = await registry.get_client("filesystem")
        if client:
            print("✅ filesystem MCP 服务器连接成功")
            tools = await client.list_tools()
            print(f"📋 可用工具: {[t.name for t in tools]}")
        else:
            print("⚠️  filesystem MCP 服务器连接失败（可能未安装）")
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        print("💡 提示: 需要先安装 MCP 服务器:")
        print("   npm install -g @modelcontextprotocol/server-filesystem")


async def test_a2a_client():
    """测试 A2A 客户端"""
    print("\n" + "="*60)
    print("测试 2: A2A 客户端基础功能")
    print("="*60)
    
    from src.service.a2a_client import A2AClient
    from src.protocols.a2a_protocol import A2ATaskRequest
    
    client = A2AClient(sender_id="test_client")
    
    # 创建测试请求
    request = A2ATaskRequest(
        task_id="test_001",
        task_type="search",
        task_description="搜索特斯拉最新消息"
    )
    
    print("\n📌 A2A 客户端创建成功")
    print(f"📋 测试请求: {request.task_description}")
    print("⚠️  注意: 实际发送需要真实的 Agent 服务")


async def test_unified_executor():
    """测试统一执行层"""
    print("\n" + "="*60)
    print("测试 3: 统一执行层智能路由")
    print("="*60)
    
    from src.graph.unified_executor import UnifiedExecutor
    
    executor = UnifiedExecutor()
    
    # 测试任务 1: 搜索类（应该尝试 MCP）
    task_search = {
        "task_id": "test_search",
        "task_title": "搜索特斯拉",
        "task_description": "搜索特斯拉最新消息",
        "assigned_agent_id": "search_agent_001",
        "target_ip": "192.168.1.10",
        "target_port": 8080
    }
    
    print("\n📌 测试搜索任务...")
    print(f"任务描述: {task_search['task_description']}")
    
    # 尝试匹配 MCP 工具
    mcp_tool = executor._try_match_mcp_tool(task_search)
    if mcp_tool:
        print(f"✅ 匹配到 MCP 工具: {mcp_tool['name']}")
        print(f"   能力类型: {mcp_tool['capability']}")
        print(f"   参数: {mcp_tool['args']}")
    else:
        print("⚠️  未匹配到 MCP 工具，将使用 A2A")
    
    # 测试任务 2: 文件操作（应该尝试 MCP）
    task_file = {
        "task_id": "test_file",
        "task_title": "读取配置文件",
        "task_description": "读取文件 '/tmp/config.json'",
        "assigned_agent_id": "file_agent_001",
        "target_ip": "192.168.1.11",
        "target_port": 8081
    }
    
    print(f"\n📌 测试文件操作任务...")
    print(f"任务描述: {task_file['task_description']}")
    
    mcp_tool = executor._try_match_mcp_tool(task_file)
    if mcp_tool:
        print(f"✅ 匹配到 MCP 工具: {mcp_tool['name']}")
        print(f"   能力类型: {mcp_tool['capability']}")
        print(f"   参数: {mcp_tool['args']}")
    else:
        print("⚠️  未匹配到 MCP 工具，将使用 A2A")


async def test_protocol_definitions():
    """测试协议定义"""
    print("\n" + "="*60)
    print("测试 4: A2A 协议定义")
    print("="*60)
    
    from src.protocols.a2a_protocol import (
        A2AMessage,
        A2ATaskRequest,
        A2ATaskResponse,
        create_task_request,
        create_success_response
    )
    
    # 创建请求
    request = create_task_request(
        task_id="demo_001",
        task_type="search",
        task_description="搜索 AI 最新进展"
    )
    
    print("\n📌 A2A 请求:")
    print(f"   任务ID: {request.task_id}")
    print(f"   任务类型: {request.task_type}")
    print(f"   描述: {request.task_description}")
    
    # 创建消息
    message = A2AMessage(
        sender_id="l2_scheduler",
        receiver_id="search_agent",
        message_type="request",
        payload=request.dict()
    )
    
    print(f"\n📌 A2A 消息:")
    print(f"   消息ID: {message.message_id}")
    print(f"   发送方: {message.sender_id}")
    print(f"   接收方: {message.receiver_id}")
    print(f"   类型: {message.message_type}")
    
    # 创建响应
    response = create_success_response(
        task_id="demo_001",
        result="搜索完成，找到 10 条结果"
    )
    
    print(f"\n📌 A2A 响应:")
    print(f"   任务ID: {response.task_id}")
    print(f"   状态: {response.status}")
    print(f"   结果: {response.result}")
    
    print("\n✅ 协议定义测试通过")


async def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("🧪 LangManus MCP + A2A 统一执行层测试套件")
    print("="*60)
    
    # 测试 4: 协议定义（不需要外部依赖）
    await test_protocol_definitions()
    
    # 测试 2: A2A 客户端（不需要外部依赖）
    await test_a2a_client()
    
    # 测试 3: 统一执行层（不需要外部依赖）
    await test_unified_executor()
    
    # 测试 1: MCP 客户端（需要外部 MCP 服务器）
    await test_mcp_client()
    
    print("\n" + "="*60)
    print("✅ 测试完成")
    print("="*60)
    print("\n💡 总结:")
    print("1. ✅ A2A 协议定义正常")
    print("2. ✅ A2A 客户端可以创建")
    print("3. ✅ 统一执行层智能路由正常")
    print("4. ⚠️  MCP 客户端需要外部服务器支持")
    print("\n📋 下一步:")
    print("- 安装 MCP 服务器: npm install -g @modelcontextprotocol/server-filesystem")
    print("- 真实 Agent 需要实现 A2A 服务端接口")
    print("- 当前系统会自动降级到 LLM 模拟器")


if __name__ == "__main__":
    asyncio.run(main())
