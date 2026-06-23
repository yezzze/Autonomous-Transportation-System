"""
A2A (Agent-to-Agent) 内部适配 DTO

项目主调用链已迁移到 a2a-python(a2a-sdk) 的标准 Agent Card + JSON-RPC。
本模块保留两类结构：
1. A2ATaskRequest/A2ATaskResponse：UnifiedExecutor 与 A2AClient 之间的内部 DTO。
2. A2AMessage：旧 /a2a/execute wire protocol 的兼容包装，待旧 Agent 迁移后删除。
"""

try:
    # pydantic v2 使用 ConfigDict；项目部分环境仍可能是 v1。
    # 这里做条件导入，保证 DTO 在两类运行环境下都能用字段名和 alias 初始化。
    from pydantic import BaseModel, ConfigDict, Field
except ImportError:
    from pydantic import BaseModel, Field
    ConfigDict = None
from typing import Literal, Any, Optional
from datetime import datetime
import uuid


class A2AMessage(BaseModel):
    """
    旧版自研 /a2a/execute wire protocol 的兼容消息格式。

    新版 Agent 间通信不再使用该包装；标准 A2A 调用由 a2a-python 负责。
    保留该结构只用于 legacy fallback，便于未升级 Agent 继续被调度。
    所有 Agent 迁移完成后，可以与 _LegacyA2AClient 一起删除。
    """
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    sender_id: str  # 发送方 Agent ID
    receiver_id: str  # 接收方 Agent ID
    message_type: Literal["request", "response", "notification", "error"]
    payload: dict
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    correlation_id: Optional[str] = None  # 用于关联请求-响应


class A2ATaskRequest(BaseModel):
    """
    项目内部任务请求 DTO。

    A2AClient 会将它转换为标准 A2A text message，或在 legacy fallback
    中作为旧 /a2a/execute payload 发送。
    """
    task_id: str
    task_type: str  # "search", "nlp", "compute", "vision", "code", "web"
    task_description: str
    context: dict = Field(default_factory=dict)  # 上下文信息
    timeout: int = 30  # 超时时间（秒）
    priority: Literal["low", "normal", "high"] = "normal"
    require_stream: bool = False  # 是否需要流式返回
    metadata: dict = Field(default_factory=dict)  # 额外元数据


class A2ATaskResponse(BaseModel):
    """
    项目内部任务响应 DTO。

    标准 A2A 的 Task/Message 响应和旧协议响应都会被归一化为该结构。
    字段名使用 state，与 A2A 规范里的 TaskStatus.state 保持一致；
    对业务层仍只表达本项目需要的四类结果状态。
    """
    task_id: str
    # alias="status" 仅用于接收旧 /a2a/execute payload 中的 status 字段。
    # 新代码应统一读写 response.state，避免继续扩散旧协议命名。
    state: Literal["success", "error", "timeout", "cancelled"] = Field(alias="status")
    result: Any
    error_message: Optional[str] = None
    metadata: dict = Field(default_factory=dict)  # 执行时间、成本等信息

    if ConfigDict is not None:
        # pydantic v2：允许 A2ATaskResponse(state="success", ...) 使用真实字段名初始化。
        model_config = ConfigDict(populate_by_name=True)
    else:
        # pydantic v1：同等语义，允许通过字段名而不只是 alias 初始化。
        class Config:
            allow_population_by_field_name = True


class A2ACapabilityDeclaration(BaseModel):
    """
    Agent 能力声明
    
    Agent 向注册中心声明自己支持的任务类型和能力
    """
    agent_id: str
    agent_type: str  # search, nlp, compute, vision, code, web
    capabilities: list[dict]  # 支持的任务类型和参数
    status: Literal["online", "busy", "offline"] = "online"
    load_level: float = 0.0  # 当前负载 0.0-1.0
    max_concurrent_tasks: int = 5
    metadata: dict = Field(default_factory=dict)


class A2AProgressNotification(BaseModel):
    """
    任务进度通知
    
    长时间运行的任务可以通过此消息通知进度
    """
    task_id: str
    progress: float  # 0.0-1.0
    current_step: str
    estimated_time_remaining: Optional[int] = None  # 秒
    metadata: dict = Field(default_factory=dict)


# 辅助函数
def create_task_request(
    task_id: str,
    task_type: str,
    task_description: str,
    **kwargs
) -> A2ATaskRequest:
    """快速创建任务请求"""
    return A2ATaskRequest(
        task_id=task_id,
        task_type=task_type,
        task_description=task_description,
        **kwargs
    )


def create_success_response(
    task_id: str,
    result: Any,
    **kwargs
) -> A2ATaskResponse:
    """快速创建成功响应"""
    return A2ATaskResponse(
        task_id=task_id,
        state="success",
        result=result,
        **kwargs
    )


def create_error_response(
    task_id: str,
    error_message: str,
    **kwargs
) -> A2ATaskResponse:
    """快速创建错误响应"""
    return A2ATaskResponse(
        task_id=task_id,
        state="error",
        result=None,
        error_message=error_message,
        **kwargs
    )
