"""
记忆中心 (MC) 数据模型

严格对应 智能体编排层接口流程v2.md:
- §9  MemoryBundle 数据结构
- §10 DelegatedMemoryBundle 数据结构
- §11 DistributedState v2 新增字段
- §15 记忆写回策略
- §16 记忆权限策略
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal, Optional, Any
from datetime import datetime
import uuid


# ============================================================
# §16 记忆权限策略 (DelegatedPolicy)
# ============================================================

class DelegatedPolicy(BaseModel):
    """
    远端调用的默认策略 (§16)
    """
    allow_callee_local_memory_read: bool = True
    """允许被调用 Agent 读取自己的本地经验"""
    allow_callee_save_local_audit: bool = True
    """允许被调用设备保存本地审计和 Agent 自身运行经验"""
    allow_callee_save_caller_identity: bool = True
    """允许被调用设备保存调用方设备ID/WorkflowID"""
    allow_callee_save_delegated_context: bool = False
    """不允许被调用设备保存调用方未授权的私有原始上下文"""
    allow_callee_save_raw_input: bool = False
    """不允许被调用设备保存调用方未授权的原始输入"""
    caller_writeback_allowed: bool = True
    """调用方可接收远端执行结果、协作经验和候选记忆"""
    local_audit_writeback_allowed: bool = True
    """被调用方可保存本地审计和 Agent 自身运行经验"""
    caller_owns_collaboration_memory: bool = True
    """跨主体合作记忆以请求方为主归属"""


# ============================================================
# §9 MemoryBundle 的记忆策略 (MemoryBundle.policy)
# ============================================================

class MemoryBundlePolicy(BaseModel):
    """
    MemoryBundle 内嵌的记忆读写策略 (§9)
    """
    input_readonly: bool = True
    output_collect: bool = True
    allow_agent_read_local_memory: bool = True
    allow_agent_write_local_memory: bool = True
    allow_writeback_to_caller: bool = False
    allow_save_raw_input: bool = False


# ============================================================
# §8 沙箱目录文件模型
# ============================================================

class Manifest(BaseModel):
    """/sandbox/memory/manifest.json"""
    bundle_id: str
    workflow_id: str
    task_id: str
    agent_id: str
    agent_instance_id: str
    created_at: str
    expires_at: Optional[str] = None
    policy: MemoryBundlePolicy = Field(default_factory=MemoryBundlePolicy)


class CallerInfo(BaseModel):
    """input/caller_info.json — 调用方设备、Workflow、Session 信息"""
    caller_device_id: str
    caller_workflow_id: str
    caller_session_id: Optional[str] = None
    caller_task_id: Optional[str] = None


class DelegatedContext(BaseModel):
    """input/delegated_context.json — 调用方允许暴露的上下文"""
    task_summary: str
    allowed_memories: list = Field(default_factory=list)
    constraints: dict = Field(default_factory=dict)


# ============================================================
# §15 记忆写回策略 (WritebackEntry)
# ============================================================

class WritebackEntry(BaseModel):
    """
    output/writeback_to_caller.jsonl / writeback_to_local.jsonl 的一行

    §15.1 写回调用方:
        memory_type = "remote_collaboration_experience"
        target_owner = "caller"

    §15.2 写回本地审计 / Agent 自身经验:
        memory_type = "local_agent_audit"
        target_owner = "local_agent"
    """
    memory_type: str
    target_owner: Literal["caller", "local_agent"]
    content: str
    confidence: Optional[float] = None
    agent_id: Optional[str] = None
    caller_device_id: Optional[str] = None
    caller_workflow_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ============================================================
# §9 MemoryBundle 数据结构
# ============================================================

class MemoryBundle(BaseModel):
    """
    记忆包 (§9)

    描述一个 Agent 实例启动前 MC 为其生成的专属记忆包。
    由 ASD / ALCM 挂载到沙箱内 /sandbox/memory。
    """
    bundle_id: str = Field(default_factory=lambda: f"mb_{uuid.uuid4().hex[:12]}")
    owner_device_id: str = ""
    owner_user_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    agent_id: str = ""
    agent_instance_id: str = ""
    caller_device_id: Optional[str] = None
    caller_workflow_id: Optional[str] = None
    caller_task_id: Optional[str] = None
    mount_path: str = "/sandbox/memory"
    visibility: Literal["local", "delegated"] = "local"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    expires_at: Optional[str] = None
    policy: MemoryBundlePolicy = Field(default_factory=MemoryBundlePolicy)


# ============================================================
# §10 DelegatedMemoryBundle 数据结构
# ============================================================

class DelegatedMemoryBundle(BaseModel):
    """
    跨设备委派记忆包 (§10)

    当设备 A 调用设备 B 的 Agent 时使用。
    只发送经过脱敏和最小化的上下文。
    """
    delegation_id: str = Field(default_factory=lambda: f"dmb_{uuid.uuid4().hex[:12]}")
    caller_device_id: str
    caller_user_id: str = ""
    caller_workflow_id: str
    caller_task_id: str
    callee_device_id: str
    target_agent_id: str
    remote_session_id: str = ""
    delegated_context: DelegatedContext = Field(default_factory=DelegatedContext)
    policy: DelegatedPolicy = Field(default_factory=DelegatedPolicy)
    expires_at: Optional[str] = None


# ============================================================
# §11 DistributedState v2 新增 — MemoryScope
#
#   AOE->>MC: 创建 memory_scope(device_id, user_id, app_id, workflow_id)
#   MC->>STORE: 检索本体长期记忆
#   STORE->>MC: 返回候选记忆
# ============================================================

class MemoryScope(BaseModel):
    """当前 Workflow 记忆域 (§11 memory_scope 字段)"""
    owner_device_id: str
    owner_user_id: str
    app_id: str
    workflow_id: str
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @property
    def scope_key(self) -> str:
        return f"{self.owner_device_id}:{self.owner_user_id}:{self.workflow_id}"


class MemoryScopeContext(BaseModel):
    """
    MC 创建 scope 后返回给 AOE 的上下文摘要

    对应 §2 流程:
      MC->>STORE: 检索本体长期记忆
      STORE->>MC: 返回候选记忆
      MC->>AOE: AOE 基于...记忆摘要生成任务图
    """
    scope: MemoryScope
    user_memory_summary: list[dict] = Field(default_factory=list)
    agent_memory_summary: list[dict] = Field(default_factory=list)
    planner_memory_context: list[dict] = Field(default_factory=list)
    """Planner 可见的摘要级记忆 (§11 planner_memory_context)"""


# ============================================================
# API 请求 / 响应模型
# ============================================================

class CreateScopeRequest(BaseModel):
    """
    AOE → MC: 创建 memory_scope
    对应 §2 流程第1步（第149行）
    """
    device_id: str
    user_id: str
    app_id: str
    workflow_id: str


class CreateBundleRequest(BaseModel):
    """
    AOE → MC: create_memory_bundle(task_id, agent_id, memory_policy)

    对应 §2 流程 loop 块（第158行）
    """
    task_id: str
    agent_id: str
    agent_instance_id: str = ""
    workflow_id: str
    device_id: str
    user_id: str = ""
    memory_policy: Optional[MemoryBundlePolicy] = None
    caller_info: Optional[CallerInfo] = None


class CreateBundleResponse(BaseModel):
    """MC → AOE: 返回 bundle_id 与 memory_mount_spec"""
    bundle_id: str
    memory_mount_spec: dict


class OutboxData(BaseModel):
    """MC 回收 Agent output 后的结构化数据"""
    result: Optional[dict] = None
    execution_notes: Optional[str] = None
    writeback_to_caller: list[WritebackEntry] = Field(default_factory=list)
    """返回给调用方 MC 的候选记忆"""
    writeback_to_local: list[WritebackEntry] = Field(default_factory=list)
    """写回本地 Agent Memory 的候选记忆"""
    artifacts: dict[str, str] = Field(default_factory=dict)
    """filename → base64"""
    has_output: bool = False


class MemorySearchRequest(BaseModel):
    """查询本体记忆 / Agent 记忆 (§12.3 POST /memory/search)"""
    query: str
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    user_id: Optional[str] = None
    device_id: Optional[str] = None
    top_k: int = 10
    filters: dict = Field(default_factory=dict)


class MemoryWriteRequest(BaseModel):
    """写入本地记忆 (§12.3 POST /memory/write)"""
    content: str
    memory_type: str = "agent_experience"
    agent_id: str
    workflow_id: Optional[str] = None
    device_id: Optional[str] = None
    user_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class BatchMemoryWriteRequest(BaseModel):
    """批量写入本地记忆"""
    entries: list[MemoryWriteRequest]


class SessionPathResponse(BaseModel):
    """Agent 查询自己记忆目录的挂载信息"""
    instance_id: str
    agent_id: str
    bundle_id: str
    mount_path: str
    manifest_path: str
    exists: bool
