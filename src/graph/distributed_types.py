"""
分布式 Agent 调度器的 State 定义
"""
from typing import List, Dict, Any, Literal
from typing_extensions import TypedDict
from langgraph.graph import MessagesState


class AgentInfo(TypedDict):
    """L3 层 Agent 的信息"""
    id: str  # Agent 唯一标识符
    ip: str  # Agent 的 IP 地址
    port: int  # Agent 的端口号
    capability: str  # Agent 的能力描述（如 "search", "compute", "vision"）
    status: Literal["online", "offline", "busy"]  # Agent 当前状态
    description: str  # Agent 详细描述


class SubWorkflowInfo(TypedDict):
    """子工作流定义（可被远端发现和调用的命名 Pipeline）"""
    id: str           # 子工作流唯一标识，如 "swf_perception_fusion"
    capability: str   # 能力标签（用于 Planner 匹配）
    description: str  # 详细描述
    owner_ip: str     # 所属节点 IP
    owner_port: int   # 所属节点端口
    pipeline: list    # PipelineTopology（复用现有格式）
    status: str       # "online" | "offline"


class TaskAssignment(TypedDict):
    """单个任务分配"""
    task_id: str  # 任务唯一标识符
    task_title: str  # 任务标题
    task_description: str  # 任务详细描述
    assigned_agent_id: str  # 分配的 Agent ID
    target_ip: str  # 目标 IP 地址
    target_port: int  # 目标端口
    status: Literal["pending", "running", "completed", "failed"]  # 任务状态
    result: str  # 任务执行结果
    retry_count: int  # 重试次数
    parallel_group: str  # 并行组标识（同值=并行，空字符串=串行）
    sub_workflow_id: str  # 非空时表示此任务路由到子工作流（而非单个 agent）


class DistributedState(MessagesState):
    """
    分布式 Agent 调度器的 State
    
    继承自 MessagesState，保留消息历史功能
    新增分布式调度所需的字段
    """
    
    # ========== 常量配置 ==========
    max_retries: int  # 最大重试次数
    timeout_seconds: int  # 请求超时时间
    
    # ========== L3 Agent 注册表 ==========
    agent_registry_cache: List[AgentInfo]  # 缓存的 Agent 列表
    registry_last_update: str  # 注册表最后更新时间
    
    # ========== 执行计划 ==========
    execution_plan: List[TaskAssignment]  # 任务执行计划
    current_task_index: int  # 当前执行的任务索引
    
    # ========== 执行状态 ==========
    plan_generated: bool  # 是否已生成计划
    all_tasks_completed: bool  # 是否所有任务已完成
    failed_tasks: List[str]  # 失败的任务 ID 列表
    
    # ========== 混合编排 ==========
    replanning_count: int  # 重新规划次数
    max_replanning: int  # 最大重新规划次数（默认 2）
    replanning_enabled: bool  # 是否启用智能重规划
    last_replan_reason: str  # 上次重规划的原因
    
    # ========== Magentic-One 编排 ==========
    magentic_round: int  # 当前轮次
    magentic_stall_count: int  # 停滞计数
    magentic_max_round: int  # 最大轮次（默认 20）
    magentic_max_stall: int  # 最大停滞次数（默认 3）
    magentic_mode: str  # 当前模式: inner_loop/outer_loop
    progress_ledger: dict  # Progress Ledger
    reset_count: int  # 重置次数
    complexity_level: str  # 任务复杂度: simple/medium/complex
    
    # ========== 跨主体编排 ==========
    # key: 子任务图标识 (str)，value: 远端 AOE URL (str)
    cross_host_sessions: Dict[str, str]
    # 跨主体会话超时（秒），由其他主体创建的工作流需要超时机制
    session_timeout_seconds: int
    # 跨主体调用失败的任务 ID 列表（用于 Phase 4 故障切换后触发重规划）
    failed_cross_host_tasks: List[str]
    # §2.3 跨主体重编排：task_id → 已尝试过但失败的远端 AOE URL 列表
    # 用于 find_alternative_remote_aoe 时避免重复尝试已知失败的节点
    failed_remote_aoe_urls: Dict[str, List[str]]

    # ========== 应用层指引 ==========
    skills_content: str  # Skills.md 注入内容（来自 GuidanceFile）
    pipeline_topology: List  # 固定拓扑链路（来自 PipelineParser，空列表=使用LLM Planner）

    # ========== 路由控制 ==========
    next: str  # 下一个节点名称


# 保留原有的路由定义（用于兼容性）
class Router(TypedDict):
    """路由决策"""
    next: Literal["planner", "executor", "monitor", "reporter", "FINISH"]
