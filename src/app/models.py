"""
应用管理层数据模型

对应架构图中的应用管理层（APP）组件：
- AgentImage:    智能体镜像（AW 仓库存储单元）
- GuidanceFile:  编排指导文件（ALRE 存储的应用执行逻辑）
- AppInfo:       应用完整信息（APPM 管理的应用实体）
- DeploymentStatus / RunStatus: 应用部署与运行生命周期状态枚举
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


# ======================================================================
# 应用状态
# ======================================================================

DeploymentStatus = Literal[
    "undeployed", "deploying", "deployed", "undeploying", "deployment_error"
]
RunStatus = Literal[
    "not_running", "starting", "running", "stopping", "stopped", "completed", "run_error"
]
DEPLOYMENT_STATUSES = {
    "undeployed", "deploying", "deployed", "undeploying", "deployment_error"
}
RUN_STATUSES = {
    "not_running", "starting", "running", "stopping", "stopped", "completed", "run_error"
}
AgentType = Literal["business", "resource"]


# ======================================================================
# 智能体镜像（AW 存储单元）
# ======================================================================

@dataclass
class AgentImage:
    """
    智能体镜像

    对应 AW（智能体仓库）中存储的镜像信息。
    注册到 ARDC 后才能被编排引擎发现和调用。
    """
    image_id: str
    name: str
    version: str
    capability: str              # 能力类型，与 AgentRegistryClient 中的 capability 对应
    type: AgentType = "business"  # business=业务智能体，resource=资源智能体
    description: str = ""
    exposed_external: bool = False  # 是否允许外部主体使用
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    registered: bool = False     # 是否已注册到 ARDC

    def __post_init__(self):
        if self.type not in {"business", "resource"}:
            raise ValueError("AgentImage.type must be 'business' or 'resource'")

    @classmethod
    def create(
        cls,
        name: str,
        version: str,
        capability: str,
        description: str = "",
        exposed_external: bool = False,
        metadata: Optional[Dict] = None,
        type: AgentType = "business",
    ) -> "AgentImage":
        image_id = f"img_{capability}_{uuid.uuid4().hex[:8]}"
        return cls(
            image_id=image_id,
            name=name,
            version=version,
            capability=capability,
            type=type,
            description=description,
            exposed_external=exposed_external,
            metadata=metadata or {},
        )

    def to_dict(self) -> Dict:
        return asdict(self)


# ======================================================================
# 编排指导文件（ALRE 存储单元）
# ======================================================================

@dataclass
class GuidanceFile:
    """
    编排指导文件（载运装备信息模型）

    ALRE 中存储的应用执行逻辑描述，是启动编排的驱动文件。
    包含任务描述、所需 Agent 能力、编排模式偏好、约束条件等。
    """
    app_id: str
    task_description: str                   # 任务总体描述（输入给编排引擎）
    agents_required: List[str] = field(default_factory=list)  # 所需 Agent 能力列表
    orchestration_mode: str = "adaptive"    # "adaptive" | "sequential" | "magentic"
    constraints: Dict[str, Any] = field(default_factory=dict)
    # 约束示例：{"max_rounds": 10, "timeout_seconds": 120, "allowed_agents": [...]}
    metadata: Dict[str, Any] = field(default_factory=dict)
    # skills.md 内容：应用专属技能指引，注入给编排引擎 planner
    skills_content: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


# ======================================================================
# 周期调度执行记录
# ======================================================================

@dataclass
class ScheduleExecutionRecord:
    """
    单次周期执行记录

    WorkflowScheduler 每次触发工作流时创建一条记录，
    完成后更新 finished_at / status / result_summary。
    持久化到 data/schedule_history.json。
    """
    run_id: str                         # 唯一标识，如 "run_abc123"
    app_id: str                         # 所属应用
    workflow_handle: str                # 工作流句柄
    schedule_workflow_handle: str = ""      # 所属调度会话工作流句柄
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: Optional[str] = None   # 完成时间（运行中为 None）
    status: str = "running"             # "running" | "completed" | "failed" | "cancelled"
    result_summary: str = ""            # 结果摘要（截断到 500 字符）
    error: Optional[str] = None         # 错误信息

    def to_dict(self) -> Dict:
        return asdict(self)


# ======================================================================
# 应用完整信息（APPM 管理实体）
# ======================================================================

@dataclass
class AppInfo:
    """
    应用信息

    APPM（应用管理器）管理的应用实体，聚合镜像列表和指导文件引用。
    """
    app_id: str
    name: str
    deployment_status: DeploymentStatus = "undeployed"
    run_status: RunStatus = "not_running"
    # 关联的镜像 ID 列表（一个应用可能包含多个 Agent 镜像）
    image_ids: List[str] = field(default_factory=list)
    # 关联的指导文件（ALRE 存储）
    guidance_file: Optional[GuidanceFile] = None
    # 当前 workflow 的运行句柄（task ID 或 handle 字符串）
    workflow_handle: Optional[str] = None
    # 应用对外暴露的接口 URL（编排成功后填充）
    app_interface_url: Optional[str] = None
    deployment_error_message: Optional[str] = None
    run_error_message: Optional[str] = None
    # 标记进程停止前是否启用了周期调度，供显式自动恢复使用。
    schedule_enabled: bool = False
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def __post_init__(self):
        self._validate_deployment_status(self.deployment_status)
        self._validate_run_status(self.run_status)

    @classmethod
    def create(cls, name: str, guidance_file: Optional[GuidanceFile] = None) -> "AppInfo":
        app_id = f"app_{uuid.uuid4().hex[:8]}"
        return cls(app_id=app_id, name=name, guidance_file=guidance_file)

    @staticmethod
    def _validate_deployment_status(status: str) -> None:
        if status not in DEPLOYMENT_STATUSES:
            raise ValueError(f"invalid deployment_status: {status!r}")

    @staticmethod
    def _validate_run_status(status: str) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run_status: {status!r}")

    def update_deployment_status(
        self, status: DeploymentStatus, error: Optional[str] = None
    ) -> None:
        self._validate_deployment_status(status)
        self.deployment_status = status
        self.updated_at = datetime.utcnow().isoformat()
        self.deployment_error_message = error if status == "deployment_error" else None

    def update_run_status(self, status: RunStatus, error: Optional[str] = None) -> None:
        self._validate_run_status(status)
        self.run_status = status
        self.updated_at = datetime.utcnow().isoformat()
        self.run_error_message = error if status == "run_error" else None

    def to_dict(self) -> Dict:
        d = {
            "app_id": self.app_id,
            "name": self.name,
            "deployment_status": self.deployment_status,
            "run_status": self.run_status,
            "image_ids": self.image_ids,
            "workflow_handle": self.workflow_handle,
            "app_interface_url": self.app_interface_url,
            "deployment_error_message": self.deployment_error_message,
            "run_error_message": self.run_error_message,
            "schedule_enabled": self.schedule_enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.guidance_file:
            d["guidance_file"] = self.guidance_file.to_dict()
        return d
