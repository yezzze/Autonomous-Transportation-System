"""
MCP (Model Context Protocol) 客户端

用于连接和调用 MCP 服务器提供的工具
"""

import logging
import asyncio
from typing import Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class MCPClient:
    """
    MCP 协议客户端
    
    管理与单个 MCP 服务器的连接和工具调用
    """
    
    def __init__(self, server_name: str):
        self.server_name = server_name
        self.session: Optional[ClientSession] = None
        self.tools = []
        self._read = None
        self._write = None
        
    async def connect(self, server_command: list[str]):
        """
        连接到 MCP 服务器
        
        Args:
            server_command: 启动 MCP 服务器的命令，如 ["npx", "@modelcontextprotocol/server-brave-search"]
        """
        logger.info(f"🔌 连接 MCP 服务器 [{self.server_name}]: {' '.join(server_command)}")
        
        try:
            server_params = StdioServerParameters(
                command=server_command[0],
                args=server_command[1:] if len(server_command) > 1 else []
            )
            
            # 创建 stdio 连接
            stdio = stdio_client(server_params)
            self._read, self._write = await stdio.__aenter__()
            
            # 初始化 session
            self.session = ClientSession(self._read, self._write)
            await self.session.__aenter__()
            await self.session.initialize()
            
            # 列出可用工具
            tools_result = await self.session.list_tools()
            self.tools = tools_result.tools
            
            logger.info(f"✅ 发现 {len(self.tools)} 个 MCP 工具: {[t.name for t in self.tools]}")
            return self.tools
            
        except Exception as e:
            logger.error(f"❌ MCP 连接失败 [{self.server_name}]: {e}")
            raise
    
    async def call_tool(self, tool_name: str, arguments: dict = None):
        """
        调用 MCP 工具
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            
        Returns:
            工具执行结果（字符串）
        """
        if not self.session:
            raise RuntimeError(f"MCP 客户端 [{self.server_name}] 未连接")
        
        logger.info(f"🔧 调用 MCP 工具: {self.server_name}.{tool_name}")
        logger.debug(f"参数: {arguments}")
        
        try:
            result = await self.session.call_tool(tool_name, arguments or {})
            
            # 提取文本内容
            content = []
            for item in result.content:
                if hasattr(item, 'text'):
                    content.append(item.text)
            
            result_text = "\n".join(content)
            logger.info(f"✅ 工具执行成功，返回 {len(result_text)} 字符")
            return result_text
            
        except Exception as e:
            logger.error(f"❌ 工具调用失败 [{tool_name}]: {e}")
            raise
    
    async def list_tools(self):
        """列出所有可用工具"""
        if not self.session:
            raise RuntimeError(f"MCP 客户端 [{self.server_name}] 未连接")
        
        tools_result = await self.session.list_tools()
        return tools_result.tools
    
    async def disconnect(self):
        """断开连接"""
        logger.info(f"🔌 断开 MCP 服务器 [{self.server_name}]")
        
        if self.session:
            try:
                await self.session.__aexit__(None, None, None)
            except:
                pass
        
        self.session = None
        self.tools = []


class MCPToolRegistry:
    """
    MCP 工具注册表
    
    管理多个 MCP 服务器连接，提供统一的工具调用接口
    """
    
    # 预定义的 MCP 服务器配置
    # 注意：系统默认使用内置工具（更快速、无需额外配置）
    # MCP 服务器是可选的高级功能，如需启用请取消注释
    MCP_SERVERS = {
        # Node.js MCP 服务器（需要先安装 Node.js 和 npm）
        # "search": {
        #     "command": ["npx", "@modelcontextprotocol/server-brave-search"],
        #     "description": "Brave 网络搜索工具（需要 Brave API Key）"
        # },
        # "filesystem": {
        #     "command": ["npx", "@modelcontextprotocol/server-filesystem", "/tmp"],
        #     "description": "文件系统操作工具"
        # },
        # "puppeteer": {
        #     "command": ["npx", "@modelcontextprotocol/server-puppeteer"],
        #     "description": "浏览器自动化工具"
        # },
    }
    
    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
        self._tool_map: dict[str, str] = {}  # tool_name -> server_name
    
    async def get_client(self, capability: str) -> MCPClient:
        """
        获取或创建 MCP 客户端
        
        Args:
            capability: 能力类型（search, filesystem等）
            
        Returns:
            MCPClient 实例，如果 MCP_SERVERS 为空则返回 None
        """
        # 快速检查：如果没有配置任何 MCP 服务器，直接返回 None
        if not self.MCP_SERVERS:
            logger.debug(f"⏭️ MCP 服务器未配置，跳过连接")
            return None
        
        if capability not in self.clients:
            if capability not in self.MCP_SERVERS:
                logger.debug(f"⏭️ 未找到 MCP 能力 [{capability}]，跳过")
                return None
            
            # 创建并连接客户端
            client = MCPClient(capability)
            server_config = self.MCP_SERVERS[capability]
            
            try:
                tools = await client.connect(server_config["command"])
                self.clients[capability] = client
                
                # 更新工具映射
                for tool in tools:
                    self._tool_map[tool.name] = capability
                
                logger.info(f"✅ MCP 服务器 [{capability}] 已就绪")
            except Exception as e:
                logger.warning(f"⚠️ MCP 服务器 [{capability}] 连接失败: {e}")
                # 不抛出异常，允许系统降级到其他方式
                return None
        
        return self.clients.get(capability)
    
    async def call_tool_by_name(self, tool_name: str, arguments: dict = None) -> str:
        """
        通过工具名称调用工具（自动找到对应的服务器）
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            
        Returns:
            工具执行结果
        """
        # 查找工具所属的服务器
        capability = self._tool_map.get(tool_name)
        
        if not capability:
            # 尝试从工具名推断能力类型
            capability = self._infer_capability_from_tool_name(tool_name)
        
        if not capability:
            raise ValueError(f"未找到工具: {tool_name}")
        
        client = await self.get_client(capability)
        if not client:
            raise RuntimeError(f"无法连接到 MCP 服务器: {capability}")
        
        return await client.call_tool(tool_name, arguments)
    
    def _infer_capability_from_tool_name(self, tool_name: str) -> Optional[str]:
        """从工具名推断能力类型"""
        tool_lower = tool_name.lower()
        
        if "file" in tool_lower or "read" in tool_lower or "write" in tool_lower or "directory" in tool_lower:
            return "filesystem"
        elif "sql" in tool_lower or "query" in tool_lower or "database" in tool_lower:
            return "sqlite"
        elif "git" in tool_lower or "commit" in tool_lower or "branch" in tool_lower:
            return "git"
        elif "fetch" in tool_lower or "http" in tool_lower or "request" in tool_lower:
            return "fetch"
        elif "search" in tool_lower or "brave" in tool_lower:
            return "search"  # 如果启用了 Node.js 版本
        elif "browser" in tool_lower or "navigate" in tool_lower:
            return "puppeteer"  # 如果启用了 Node.js 版本
        
        return None
    
    async def list_all_tools(self) -> dict[str, list]:
        """列出所有已连接服务器的工具"""
        all_tools = {}
        for capability, client in self.clients.items():
            try:
                tools = await client.list_tools()
                all_tools[capability] = [{"name": t.name, "description": t.description} for t in tools]
            except:
                all_tools[capability] = []
        
        return all_tools
    
    async def disconnect_all(self):
        """断开所有连接"""
        logger.info("🔌 断开所有 MCP 连接")
        for client in self.clients.values():
            await client.disconnect()
        
        self.clients.clear()
        self._tool_map.clear()


# 全局单例
_global_registry: Optional[MCPToolRegistry] = None


def get_global_mcp_registry() -> MCPToolRegistry:
    """获取全局 MCP 工具注册表"""
    global _global_registry
    if _global_registry is None:
        _global_registry = MCPToolRegistry()
    return _global_registry
