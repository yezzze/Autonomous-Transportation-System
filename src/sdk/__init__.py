"""
记忆中心 (MC) SDK — 编排层控制面组件

严格对应 智能体编排层接口流程v2.md:
  §2  单主体工作流 — memory_scope → create_bundle → collect_outbox → commit
  §3  跨主体工作流 — delegated_bundle → 接收合入 → 请求方主归属写回
  §5  工作流停止 — collect_all_outboxes → close_workflow_session
  §7  记忆包管理流程
  §9  MemoryBundle 数据结构
  §10 DelegatedMemoryBundle 数据结构
  §12 MC 接口定义（本地/委派/查询）

核心组件:
  SandboxMemory      — Agent 侧 SDK，操作 /sandbox/memory/ 文件系统
  MemoryCenterService — MC 服务端，存储 ↔ 临时目录管理
  mc_router          — FastAPI Router，REST API 端点

使用方式:
    # Agent 沙箱内
    from src.sdk import SandboxMemory
    mem = SandboxMemory()
    task = await mem.read_task_description()
    await mem.write_result({"status": "done"})

    # 服务端挂载路由
    from src.sdk import mc_router
    app.include_router(mc_router)
"""

from .memory_models import (
    # 记忆包
    MemoryBundle,
    MemoryBundlePolicy,
    DelegatedPolicy,
    # 沙箱目录
    Manifest,
    CallerInfo,
    DelegatedContext,
    WritebackEntry,
    # 记忆域
    MemoryScope,
    MemoryScopeContext,
    # 委派
    DelegatedMemoryBundle,
    # API 请求/响应
    CreateScopeRequest,
    CreateBundleRequest,
    CreateBundleResponse,
    OutboxData,
    MemorySearchRequest,
    MemoryWriteRequest,
    BatchMemoryWriteRequest,
    SessionPathResponse,
)

from .memory_sdk import SandboxMemory
from .mc_service import MemoryCenterService

try:
    from .mc_router import mc_router, set_mc_service
except ImportError:
    mc_router = None

    def set_mc_service(*args, **kwargs):
        raise RuntimeError("mc_router requires FastAPI dependencies to be installed")

__all__ = [
    # Agent SDK
    "SandboxMemory",
    # MC 服务端
    "MemoryCenterService",
    "set_mc_service",
    # FastAPI Router
    "mc_router",
    # 数据模型
    "MemoryBundle",
    "MemoryBundlePolicy",
    "DelegatedPolicy",
    "Manifest",
    "CallerInfo",
    "DelegatedContext",
    "WritebackEntry",
    "MemoryScope",
    "MemoryScopeContext",
    "DelegatedMemoryBundle",
    "CreateScopeRequest",
    "CreateBundleRequest",
    "CreateBundleResponse",
    "OutboxData",
    "MemorySearchRequest",
    "MemoryWriteRequest",
    "BatchMemoryWriteRequest",
    "SessionPathResponse",
]
