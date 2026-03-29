"""
A2A (Agent-to-Agent) 客户端

用于 L2 Scheduler 和 L3 Agents 之间的标准化通信
"""

import httpx
import asyncio
import logging
from typing import AsyncIterator, Optional
import json
from ..protocols.a2a_protocol import (
    A2AMessage,
    A2ATaskRequest,
    A2ATaskResponse,
    A2AProgressNotification
)

logger = logging.getLogger(__name__)


class A2AClient:
    """
    A2A 协议客户端
    
    提供标准化的 Agent 间通信接口
    """
    
    def __init__(self, sender_id: str = "l2_scheduler"):
        self.sender_id = sender_id
        self.timeout = httpx.Timeout(60.0, connect=5.0)
        
    async def send_task_request(
        self,
        agent_url: str,
        request: A2ATaskRequest
    ) -> A2ATaskResponse:
        """
        发送 A2A 任务请求
        
        Args:
            agent_url: Agent 服务地址（如 http://192.168.1.10:8080）
            request: 任务请求对象
            
        Returns:
            任务响应对象
        """
        
        # 构造 A2A 消息
        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="request",
            payload=request.dict()
        )
        
        logger.info(f"📤 发送 A2A 请求 [{message.message_id[:8]}]: {request.task_description[:50]}...")
        logger.debug(f"目标 Agent: {agent_url}")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{agent_url}/a2a/execute",
                    json=message.dict()
                )
                response.raise_for_status()
                
                # 解析响应
                response_message = A2AMessage(**response.json())
                task_response = A2ATaskResponse(**response_message.payload)
                
                logger.info(f"✅ 收到 A2A 响应 [{message.message_id[:8]}]: {task_response.status}")
                return task_response
                
        except httpx.TimeoutException:
            logger.error(f"⏱️ A2A 请求超时: {agent_url}")
            return A2ATaskResponse(
                task_id=request.task_id,
                status="timeout",
                result=None,
                error_message=f"Request timeout after {self.timeout.read}s"
            )
            
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ A2A HTTP 错误: {e.response.status_code}")
            return A2ATaskResponse(
                task_id=request.task_id,
                status="error",
                result=None,
                error_message=f"HTTP {e.response.status_code}: {e.response.text}"
            )
            
        except Exception as e:
            logger.error(f"❌ A2A 请求失败: {e}")
            return A2ATaskResponse(
                task_id=request.task_id,
                status="error",
                result=None,
                error_message=str(e)
            )
    
    async def stream_task_execution(
        self,
        agent_url: str,
        request: A2ATaskRequest
    ) -> AsyncIterator[A2AProgressNotification]:
        """
        流式接收 A2A 执行进度
        
        Args:
            agent_url: Agent 服务地址
            request: 任务请求对象
            
        Yields:
            任务进度通知
        """
        
        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="request",
            payload=request.dict()
        )
        
        logger.info(f"📤 启动流式 A2A 请求 [{message.message_id[:8]}]")
        
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{agent_url}/a2a/execute/stream",
                    json=message.dict()
                ) as response:
                    response.raise_for_status()
                    
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                progress_data = json.loads(line)
                                yield A2AProgressNotification(**progress_data)
                            except json.JSONDecodeError:
                                logger.warning(f"⚠️ 无法解析进度数据: {line}")
                                
        except Exception as e:
            logger.error(f"❌ 流式 A2A 请求失败: {e}")
    
    async def send_notification(
        self,
        agent_url: str,
        notification_type: str,
        payload: dict
    ):
        """
        发送通知消息（不需要响应）
        
        Args:
            agent_url: Agent 服务地址
            notification_type: 通知类型
            payload: 通知内容
        """
        
        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="notification",
            payload={"type": notification_type, "data": payload}
        )
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{agent_url}/a2a/notify",
                    json=message.dict()
                )
                logger.info(f"📣 通知已发送: {notification_type}")
        except Exception as e:
            logger.warning(f"⚠️ 通知发送失败: {e}")
    
    def _extract_agent_id(self, url: str) -> str:
        """
        从 URL 提取 Agent ID
        
        Args:
            url: Agent URL（如 http://192.168.1.10:8080）
            
        Returns:
            Agent ID（如 agent_192_168_1_10）
        """
        try:
            # 提取 IP 和端口
            parts = url.split("//")[1].split(":")
            ip = parts[0].replace(".", "_")
            port = parts[1] if len(parts) > 1 else "80"
            return f"agent_{ip}_{port}"
        except:
            return "unknown_agent"


class A2AHealthChecker:
    """
    A2A Agent 健康检查器
    
    定期检查 Agent 是否在线
    """
    
    def __init__(self, check_interval: int = 30):
        self.check_interval = check_interval
        self.agent_status: dict[str, dict] = {}
        
    async def check_agent_health(self, agent_url: str) -> bool:
        """
        检查单个 Agent 健康状态
        
        Args:
            agent_url: Agent 服务地址
            
        Returns:
            是否健康
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{agent_url}/health")
                
                if response.status_code == 200:
                    data = response.json()
                    self.agent_status[agent_url] = {
                        "status": "online",
                        "load": data.get("load", 0.0),
                        "tasks": data.get("active_tasks", 0)
                    }
                    return True
                    
        except Exception as e:
            logger.warning(f"⚠️ Agent 健康检查失败 [{agent_url}]: {e}")
            
        self.agent_status[agent_url] = {"status": "offline"}
        return False
    
    async def check_all_agents(self, agent_urls: list[str]):
        """批量检查所有 Agent"""
        tasks = [self.check_agent_health(url) for url in agent_urls]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    def get_healthy_agents(self) -> list[str]:
        """获取所有健康的 Agent"""
        return [
            url for url, status in self.agent_status.items()
            if status.get("status") == "online"
        ]
    
    def get_best_agent(self, agent_urls: list[str]) -> Optional[str]:
        """根据负载选择最佳 Agent"""
        healthy = [url for url in agent_urls if self.agent_status.get(url, {}).get("status") == "online"]
        
        if not healthy:
            return None
        
        # 选择负载最低的 Agent
        return min(healthy, key=lambda url: self.agent_status[url].get("load", 1.0))


# 全局单例
_global_a2a_client: Optional[A2AClient] = None


def get_global_a2a_client() -> A2AClient:
    """获取全局 A2A 客户端"""
    global _global_a2a_client
    if _global_a2a_client is None:
        _global_a2a_client = A2AClient()
    return _global_a2a_client
