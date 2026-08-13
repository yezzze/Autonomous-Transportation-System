"""
统一执行层 (Unified Executor)

智能选择 MCP 或 A2A 协议执行任务
"""

import logging
import re
from typing import Literal, Optional
from ..protocols.a2a_protocol import A2ATaskRequest, create_task_request
from ..service.mcp_client import get_global_mcp_registry
from ..service.a2a_client import get_global_a2a_client
from ..service.builtin_tools import get_builtin_tool_registry

logger = logging.getLogger(__name__)


class UnifiedExecutor:
    """
    统一执行层
    
    决策逻辑（已优化，跳过 MCP 检测）：
    1. 优先使用内置工具（Built-in Tools）- 本地执行，速度快
    2. 如果内置工具不支持，直接使用 A2A 协议调用远程 Agent
    3. 如果 A2A 也失败，使用 LLM 模拟器兜底
    
    注意：MCP 协议已禁用，因为 Python 版本的 MCP 服务器不稳定
    """
    
    def __init__(self):
        self.mcp_registry = get_global_mcp_registry()  # 保留但不使用
        self.a2a_client = get_global_a2a_client()
        self.builtin_tools = get_builtin_tool_registry()
        self.mcp_enabled = False  # 新增：MCP 全局开关（默认禁用）
        
    async def execute_task(self, task: dict, on_decision=None) -> dict:
        """
        执行任务，自动选择最佳协议
        
        Args:
            task: 任务信息字典，包含：
                - task_id: 任务ID
                - task_title: 任务标题
                - task_description: 任务描述
                - assigned_agent_id: 分配的 Agent ID
                - target_ip: Agent IP
                - target_port: Agent 端口
                
        Returns:
            执行结果字典，包含：
                - status: success/error/timeout
                - result: 执行结果
                - protocol: mcp/a2a
                - tool_or_agent: 使用的工具或 Agent
        """
        
        logger.info(f"📋 开始执行任务: {task['task_title']}")
        
        # 策略：直接使用 A2A 协议调用远程 Agent（跳过内置工具和 MCP 检测）
        # 原因：内置工具和 MCP 检测会增加延迟，直接使用分布式 Agent 更高效
        
        # 在实际发起调用前，如果提供了回调(on_decision)，先触发回调告知决策结果
        try:
            # 决策逻辑：优先尝试内置工具 / MCP，再使用 A2A
            tool_conf = self._try_match_builtin_tool(task) or self._try_match_mcp_tool(task)
            if tool_conf:
                proto = "builtin" if tool_conf.get("name") and tool_conf.get("capability") else "mcp"
                executor_name = f"{tool_conf.get('capability','')}.{tool_conf.get('name','')}".strip('.')
            else:
                proto = "a2a"
                executor_name = task.get("assigned_agent_id", "unknown")
        except Exception:
            proto = "UNKNOWN"
            executor_name = task.get("assigned_agent_id", "unknown")

        if on_decision:
            try:
                on_decision({"protocol": proto, "executor": executor_name})
            except Exception:
                pass

        logger.info(f"🤖 使用 {proto.upper()} 调用: {executor_name}")
        if proto == "builtin":
            # call builtin tool
            try:
                return await self._execute_with_builtin(task, tool_conf)
            except Exception:
                # fallback to A2A
                return await self._execute_with_a2a(task)
        elif proto == "mcp":
            try:
                return await self._execute_with_mcp(task, tool_conf)
            except Exception:
                return await self._execute_with_a2a(task)
        else:
            return await self._execute_with_a2a(task)
    
    def _try_match_mcp_tool(self, task: dict) -> Optional[dict]:
        """
        尝试匹配 MCP 工具
        
        Args:
            task: 任务信息
            
        Returns:
            工具配置字典或 None
        """
        description = task["task_description"].lower()
        
        # 搜索类任务
        if any(keyword in description for keyword in ["搜索", "查找", "search", "query", "find"]):
            query = self._extract_search_query(description)
            if query:
                return {
                    "capability": "search",
                    "name": "brave_web_search",
                    "args": {"query": query}
                }
        
        # 文件操作
        if any(keyword in description for keyword in ["读取", "写入", "文件", "file", "read", "write"]):
            file_path = self._extract_file_path(description)
            if file_path:
                if "读取" in description or "read" in description:
                    return {
                        "capability": "filesystem",
                        "name": "read_file",
                        "args": {"path": file_path}
                    }
                elif "写入" in description or "write" in description:
                    return {
                        "capability": "filesystem",
                        "name": "write_file",
                        "args": {"path": file_path, "content": ""}
                    }
        
        # 浏览器操作
        if any(keyword in description for keyword in ["打开网页", "访问", "navigate", "browse"]):
            url = self._extract_url(description)
            if url:
                return {
                    "capability": "puppeteer",
                    "name": "puppeteer_navigate",
                    "args": {"url": url}
                }
        
        # 代码执行/计算
        if any(keyword in description for keyword in ["计算", "执行", "运行", "compute", "calculate"]):
            # 这里可以根据具体需求选择合适的工具
            # 暂时不使用 MCP，让 Agent 处理
            return None
        
        return None
    
    def _try_match_builtin_tool(self, task: dict) -> Optional[dict]:
        """
        尝试匹配内置工具(优先使用,无需额外服务器)
        
        Args:
            task: 任务信息
            
        Returns:
            工具配置字典或 None
        """
        description = task["task_description"].lower()
        
        # 搜索类任务
        if any(keyword in description for keyword in ["搜索", "查找", "search", "query", "find"]):
            query = self._extract_search_query(description)
            if query:
                return {
                    "capability": "search",
                    "name": "search",
                    "args": {"query": query}
                }
        
        # 文件操作
        if any(keyword in description for keyword in ["读取", "写入", "文件", "file", "read", "write", "列出", "list"]):
            file_path = self._extract_file_path(description)
            if file_path:
                if "读取" in description or "read" in description:
                    return {
                        "capability": "filesystem",
                        "name": "read_file",
                        "args": {"path": file_path}
                    }
                elif "写入" in description or "write" in description:
                    return {
                        "capability": "filesystem",
                        "name": "write_file",
                        "args": {"path": file_path, "content": "测试内容"}
                    }
            elif "列出" in description or "list" in description:
                return {
                    "capability": "filesystem",
                    "name": "list_directory",
                    "args": {"path": "/tmp"}
                }
        
        return None
    
    async def _execute_with_builtin(self, task: dict, tool_config: dict) -> dict:
        """
        通过内置工具执行任务
        
        Args:
            task: 任务信息
            tool_config: 工具配置
            
        Returns:
            执行结果
        """
        try:
            # 调用内置工具
            result = await self.builtin_tools.call_tool(
                tool_config["capability"],
                tool_config["name"],
                tool_config["args"]
            )
            
            return {
                "status": "success",
                "result": result,
                "protocol": "builtin",
                "tool_used": f"{tool_config['capability']}.{tool_config['name']}"
            }
            
        except Exception as e:
            logger.error(f"❌ 内置工具执行失败: {e}")
            return {
                "status": "error",
                "result": None,
                "error_message": str(e),
                "protocol": "builtin",
                "tool_used": f"{tool_config['capability']}.{tool_config['name']}"
            }
    
    async def _execute_with_mcp(self, task: dict, tool_config: dict) -> dict:
        """
        通过 MCP 执行任务
        
        Args:
            task: 任务信息
            tool_config: 工具配置
            
        Returns:
            执行结果
        """
        try:
            # 获取或创建 MCP 客户端
            client = await self.mcp_registry.get_client(tool_config["capability"])
            
            if not client:
                return {
                    "status": "error",
                    "result": None,
                    "error_message": f"无法连接到 MCP 服务器: {tool_config['capability']}",
                    "protocol": "mcp",
                    "tool_used": tool_config["name"]
                }
            
            # 调用工具
            result = await client.call_tool(tool_config["name"], tool_config["args"])
            
            return {
                "status": "success",
                "result": result,
                "protocol": "mcp",
                "tool_used": tool_config["name"]
            }
            
        except Exception as e:
            logger.error(f"❌ MCP 执行失败: {e}")
            return {
                "status": "error",
                "result": None,
                "error_message": str(e),
                "protocol": "mcp",
                "tool_used": tool_config["name"]
            }
    
    async def _execute_with_a2a(self, task: dict) -> dict:
        """
        通过 A2A 执行任务。

        Agent URL 解析优先级：
        1. task 中已有 target_ip + target_port（來自 Planner）
        2. MessageRouter.route_direct(agent_id)（精确匹配 agent registry）
        3. MessageRouter.route_by_capability(capability)（能力路由，轮询负载均衡）
        """
        try:
            # 构造 A2A 请求
            request = create_task_request(
                task_id=task["task_id"],
                task_type=self._infer_task_type(task),
                task_description=task["task_description"],
                context={},
                parameters=task.get("parameters", {}),
            )

            # ─── Agent URL 解析（通过 MessageRouter）────────────────
            agent_url = self._resolve_agent_url(task)

            # 发送请求
            response = await self.a2a_client.send_task_request(agent_url, request)

            return {
                # UnifiedExecutor 对外仍返回 status，保持 UI/编排层现有契约；
                # A2ATaskResponse 内部字段已改为 state，以贴近 A2A TaskStatus.state。
                "status": response.state,
                "result": response.result,
                "error_message": response.error_message,
                "protocol": "a2a",
                "agent_used": task["assigned_agent_id"],
                # 透传 A2AClient 生成的 transport/QoS/A2A task metadata，
                # 上层可据此区分 a2a-python 与 legacy fallback，并查看耗时。
                "metadata": response.metadata,
            }

        except Exception as e:
            logger.error(f"❌ A2A 执行失败: {e}")
            return {
                # 异常路径也返回 metadata，避免调用方在成功/失败分支处理不同结构。
                "status": "error",
                "result": None,
                "error_message": str(e),
                "protocol": "a2a",
                "agent_used": task.get("assigned_agent_id", "unknown"),
                "metadata": {},
            }

    def _resolve_agent_url(self, task: dict) -> str:
        """
        解析 Agent 的服务 URL。

        优先级：
        1. task 自带 target_ip + target_port（最优先，Planner 已分配）
        2. MessageRouter.route_direct(assigned_agent_id)
        3. MessageRouter.route_by_capability(inferred_capability)
        4. 兜底：localhost:8001

        Args:
            task: 任务字典

        Returns:
            agent_url，如 "http://127.0.0.1:8001"
        """
        # 1. 直接使用 Planner 分配的地址
        target_ip = task.get("target_ip", "")
        target_port = task.get("target_port", 0)
        if target_ip and target_port and target_ip not in ("", "0.0.0.0"):
            return f"http://{target_ip}:{target_port}"

        # 2. 通过路由器精确匹配 agent_id
        try:
            from src.service.message_router import get_message_router
            router = get_message_router()

            agent_id = task.get("assigned_agent_id", "")
            if agent_id:
                url = router.route_direct(agent_id)
                if url:
                    logger.debug(f"[Router] 精确路由: {agent_id} → {url}")
                    return url

            # 3. 按能力路由（轮询）
            capability = self._infer_task_type(task)
            url = router.route_by_capability(capability)
            if url:
                logger.debug(f"[Router] 能力路由: {capability} → {url}")
                return url

        except Exception as e:
            logger.debug(f"[Router] 路由查询失败，使用兜底地址: {e}")

        # 4. 兜底
        return "http://localhost:8001"
    
    def _infer_task_type(self, task: dict) -> str:
        """
        推断任务类型
        
        Args:
            task: 任务信息
            
        Returns:
            任务类型字符串
        """
        agent_id = task.get("assigned_agent_id", "").lower()
        
        if "search" in agent_id:
            return "search"
        elif "nlp" in agent_id:
            return "nlp"
        elif "compute" in agent_id:
            return "compute"
        elif "vision" in agent_id:
            return "vision"
        elif "code" in agent_id:
            return "code"
        elif "web" in agent_id:
            return "web"
        
        # 从任务描述推断
        description = task.get("task_description", "").lower()
        if any(kw in description for kw in ["搜索", "search", "查找"]):
            return "search"
        elif any(kw in description for kw in ["分析", "理解", "总结", "nlp"]):
            return "nlp"
        elif any(kw in description for kw in ["计算", "compute", "数学"]):
            return "compute"
        elif any(kw in description for kw in ["图片", "图像", "vision", "识别"]):
            return "vision"
        elif any(kw in description for kw in ["代码", "code", "编程"]):
            return "code"
        elif any(kw in description for kw in ["网页", "web", "浏览器"]):
            return "web"
        
        return "general"
    
    def _extract_search_query(self, description: str) -> Optional[str]:
        """
        提取搜索关键词
        
        Args:
            description: 任务描述
            
        Returns:
            搜索关键词或 None
        """
        # 移除常见的搜索前缀
        query = description
        for prefix in ["搜索", "查找", "search", "find", "query"]:
            query = query.replace(prefix, "").strip()
        
        # 移除引号
        query = query.replace('"', '').replace("'", '').replace('"', '').replace('"', '')
        
        return query if query else None
    
    def _extract_file_path(self, description: str) -> Optional[str]:
        """
        提取文件路径
        
        Args:
            description: 任务描述
            
        Returns:
            文件路径或 None
        """
        # 查找引号内的内容
        patterns = [
            r'["\']([^"\']+)["\']',  # 双引号或单引号
            r'[""]([^""]+)[""]',  # 中文双引号
            r'文件\s*[:：]\s*(\S+)',  # "文件: xxx"
            r'路径\s*[:：]\s*(\S+)',  # "路径: xxx"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, description)
            if match:
                return match.group(1)
        
        return None
    
    def _extract_url(self, description: str) -> Optional[str]:
        """
        提取 URL
        
        Args:
            description: 任务描述
            
        Returns:
            URL 或 None
        """
        # URL 正则
        url_pattern = r'https?://[^\s]+'
        match = re.search(url_pattern, description)
        if match:
            return match.group(0)
        
        # 查找引号内的内容
        quote_pattern = r'["\']([^"\']+)["\']'
        match = re.search(quote_pattern, description)
        if match:
            potential_url = match.group(1)
            if potential_url.startswith(('http://', 'https://', 'www.')):
                return potential_url
        
        return None
