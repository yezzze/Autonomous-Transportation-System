"""
内置工具实现

使用 Python 标准库实现常用工具，无需额外的 MCP 服务器
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)


class BuiltinFileSystemTool:
    """
    内置文件系统工具
    
    使用 Python 标准库实现，无需 MCP 服务器
    """
    
    def __init__(self, allowed_path: str = "/tmp"):
        self.allowed_path = Path(allowed_path).resolve()
        logger.info(f"📁 文件系统工具初始化，允许路径: {self.allowed_path}")
    
    def _check_path(self, path: str) -> Path:
        """检查路径是否在允许的范围内"""
        full_path = Path(path).resolve()
        try:
            full_path.relative_to(self.allowed_path)
            return full_path
        except ValueError:
            raise PermissionError(f"路径 {path} 不在允许的范围内: {self.allowed_path}")
    
    async def read_file(self, path: str) -> str:
        """读取文件"""
        full_path = self._check_path(path)
        logger.info(f"📖 读取文件: {full_path}")
        
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"✅ 读取成功，{len(content)} 字符")
            return content
        except Exception as e:
            logger.error(f"❌ 读取失败: {e}")
            raise
    
    async def write_file(self, path: str, content: str) -> str:
        """写入文件"""
        full_path = self._check_path(path)
        logger.info(f"📝 写入文件: {full_path}")
        
        try:
            # 确保目录存在
            full_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            logger.info(f"✅ 写入成功，{len(content)} 字符")
            return f"文件已写入: {full_path}"
        except Exception as e:
            logger.error(f"❌ 写入失败: {e}")
            raise
    
    async def list_directory(self, path: str = ".") -> str:
        """列出目录内容"""
        full_path = self._check_path(path)
        logger.info(f"📂 列出目录: {full_path}")
        
        try:
            items = []
            for item in sorted(full_path.iterdir()):
                item_type = "📁" if item.is_dir() else "📄"
                size = item.stat().st_size if item.is_file() else 0
                items.append(f"{item_type} {item.name} ({size} bytes)")
            
            result = "\n".join(items) if items else "目录为空"
            logger.info(f"✅ 找到 {len(items)} 个项目")
            return result
        except Exception as e:
            logger.error(f"❌ 列出失败: {e}")
            raise
    
    async def delete_file(self, path: str) -> str:
        """删除文件"""
        full_path = self._check_path(path)
        logger.info(f"🗑️  删除文件: {full_path}")
        
        try:
            if full_path.is_file():
                full_path.unlink()
                logger.info(f"✅ 删除成功")
                return f"文件已删除: {full_path}"
            else:
                raise ValueError(f"不是文件: {full_path}")
        except Exception as e:
            logger.error(f"❌ 删除失败: {e}")
            raise


class BuiltinSearchTool:
    """
    内置搜索工具
    
    使用 httpx 调用公开的搜索 API
    """
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        logger.info("🔍 搜索工具初始化")
    
    async def search(self, query: str, max_results: int = 5) -> str:
        """
        搜索网络内容
        
        使用 DuckDuckGo Instant Answer API（免费，无需 API key）
        """
        logger.info(f"🔍 搜索: {query}")
        
        try:
            # 使用 DuckDuckGo API
            url = "https://api.duckduckgo.com/"
            params = {
                "q": query,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1
            }
            
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()
            
            # 构建结果
            results = []
            
            # Abstract
            if data.get("Abstract"):
                results.append(f"📝 摘要: {data['Abstract']}")
                if data.get("AbstractURL"):
                    results.append(f"   来源: {data['AbstractURL']}")
            
            # Related Topics
            if data.get("RelatedTopics"):
                results.append("\n📚 相关主题:")
                for i, topic in enumerate(data["RelatedTopics"][:max_results], 1):
                    if isinstance(topic, dict) and "Text" in topic:
                        results.append(f"  {i}. {topic['Text']}")
                        if topic.get("FirstURL"):
                            results.append(f"     {topic['FirstURL']}")
            
            result_text = "\n".join(results) if results else "未找到相关信息"
            logger.info(f"✅ 搜索完成，返回 {len(results)} 条结果")
            return result_text
            
        except Exception as e:
            logger.error(f"❌ 搜索失败: {e}")
            # 返回友好的错误信息，而不是抛出异常
            return f"搜索遇到问题: {str(e)}\n建议：可以尝试使用其他关键词或稍后再试。"
    
    async def close(self):
        """关闭 HTTP 客户端"""
        await self.client.aclose()


class BuiltinToolRegistry:
    """
    内置工具注册表
    
    管理所有内置工具
    """
    
    def __init__(self):
        self.filesystem = BuiltinFileSystemTool()
        self.search = BuiltinSearchTool()
        logger.info("🔧 内置工具注册表初始化完成")
    
    async def call_tool(self, capability: str, tool_name: str, args: Dict[str, Any]) -> str:
        """
        调用内置工具
        
        Args:
            capability: 能力类型 (filesystem, search)
            tool_name: 工具名称
            args: 参数
            
        Returns:
            工具执行结果
        """
        logger.info(f"🔧 调用内置工具: {capability}.{tool_name}")
        
        try:
            if capability == "filesystem":
                if tool_name == "read_file":
                    return await self.filesystem.read_file(args["path"])
                elif tool_name == "write_file":
                    return await self.filesystem.write_file(args["path"], args["content"])
                elif tool_name == "list_directory":
                    return await self.filesystem.list_directory(args.get("path", "."))
                elif tool_name == "delete_file":
                    return await self.filesystem.delete_file(args["path"])
                else:
                    raise ValueError(f"未知的文件系统工具: {tool_name}")
            
            elif capability == "search":
                if tool_name in ["search", "web_search", "brave_web_search"]:
                    return await self.search.search(args["query"], args.get("max_results", 5))
                else:
                    raise ValueError(f"未知的搜索工具: {tool_name}")
            
            else:
                raise ValueError(f"未知的能力类型: {capability}")
        
        except Exception as e:
            logger.error(f"❌ 工具调用失败: {e}")
            raise
    
    async def close(self):
        """关闭所有工具"""
        await self.search.close()


# 全局单例
_builtin_registry: Optional[BuiltinToolRegistry] = None


def get_builtin_tool_registry() -> BuiltinToolRegistry:
    """获取全局内置工具注册表"""
    global _builtin_registry
    if _builtin_registry is None:
        _builtin_registry = BuiltinToolRegistry()
    return _builtin_registry
