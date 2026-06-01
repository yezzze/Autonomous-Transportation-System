"""
分布式 Agent 调度器的节点实现
"""
import asyncio
import logging
import json
import time
import uuid
from typing import Literal
from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command

# 直接导入 llm 模块，避免触发 agents.__init__.py
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
from src.agents.llm import get_llm_by_type
from src.service.agent_registry import get_registry_client
from src.service.llm_agent_simulator import get_llm_agent_simulator
from .distributed_types import DistributedState, TaskAssignment

logger = logging.getLogger(__name__)

# 配置：是否使用 LLM 模拟智能体（默认启用）
USE_LLM_SIMULATOR = os.getenv("USE_LLM_SIMULATOR", "true").lower() == "true"
LLM_SIMULATOR_MODEL = os.getenv("LLM_SIMULATOR_MODEL", "basic")  # "basic" or "reasoning"

def _agent_is_local(agent: dict | None) -> bool:
    """仅根据 agent_registry.json 中的 is_local 字段判断是否本地。"""
    return bool(agent and agent.get("is_local", False))


def _local_aoe_endpoint() -> tuple[str, int]:
    """解析当前本地 AOE 地址，用于区分本地/远端子工作流。"""
    import urllib.parse

    local_url = os.getenv("LOCAL_AOE_URL", "http://localhost:8000")
    parsed = urllib.parse.urlparse(local_url)
    return parsed.hostname or "localhost", parsed.port or 8000

# Demo 模式：Agent ID → 车辆名称映射（用于可视化事件）
_AGENT_TO_VEHICLE: dict[str, str] = {
    "perception_self_001": "self",
    "perception_vehicleB_001": "vehicleB",
    "perception_vehicleC_001": "vehicleC",
    "cognition_main_001": "cognition",
}

# 模块级：跟踪本节点当前发出的跨主体会话（session_id → remote_aoe_url）
# 用于 stop_app 时通知对端取消正在运行的子任务
_active_remote_sessions: dict[str, str] = {}


def get_active_remote_sessions() -> dict[str, str]:
    """返回当前活跃的远端 AOE 会话字典副本（供 stop_app 使用）"""
    return dict(_active_remote_sessions)


# ============================================================
# 跨主体工作流：子任务分发骨架
# ============================================================

async def dispatch_subtask_to_remote_aoe(
    subtask: dict,
    remote_aoe_url: str,
    session_timeout: int = 30,
) -> dict:
    """
    将子任务图分发到远端 AOE（智能体编排引擎）

    接口文档参考：智能体编排层接口流程 §2.2 跨主体工作流编排

    Args:
        subtask:          子任务图描述（包含 task_id, description, required_agents 等）
        remote_aoe_url:   远端 AOE 的 HTTP 地址，如 "http://192.168.1.10:9000"
        session_timeout:  HTTP 请求超时（秒）

    Returns:
        {
            "status": "completed"|"timeout"|"error",
            "workflow_handle": str,
            "result": str,
            "session_id": str,
            "remote_aoe_url": str,
        }
    """
    import httpx

    task_id = subtask.get("task_id", "unknown")
    session_id = subtask.get("session_id", str(uuid.uuid4()))

    logger.info(
        f"[跨主体] 分发子任务 task_id={task_id} → 远端 AOE: {remote_aoe_url}"
    )
    # 注册到活跃会话，使 stop_app 能通知远端取消
    _active_remote_sessions[session_id] = remote_aoe_url
    try:
        async with httpx.AsyncClient(timeout=float(session_timeout)) as client:
            response = await client.post(
                f"{remote_aoe_url}/orchestration/dispatch",
                json={
                    "subtask": subtask,
                    "session_id": session_id,
                    "source_aoe_url": os.getenv("LOCAL_AOE_URL", "http://localhost:8000"),
                },
            )
            response.raise_for_status()
            data = response.json()

        logger.info(
            f"[跨主体] 子任务完成: task_id={task_id}, "
            f"status={data.get('status')}"
        )

        # 异步清理：通知远端 AOE 退订引用（非阻塞，忽略失败）
        asyncio.create_task(
            _cleanup_remote_session(remote_aoe_url, session_id, session_timeout)
        )

        return {
            "status": data.get("status", "completed"),
            "workflow_handle": data.get("workflow_handle", ""),
            "result": data.get("result", ""),
            "session_id": session_id,
            "remote_aoe_url": remote_aoe_url,
        }

    except httpx.TimeoutException:
        logger.warning(
            f"[跨主体] 子任务超时: task_id={task_id}, "
            f"url={remote_aoe_url}, timeout={session_timeout}s"
        )
        return {
            "status": "timeout",
            "workflow_handle": "",
            "result": f"远端 AOE 超时（{session_timeout}s）",
            "session_id": session_id,
            "remote_aoe_url": remote_aoe_url,
            "task_id": task_id,
        }

    except Exception as e:
        logger.error(
            f"[跨主体] 子任务失败: task_id={task_id}, "
            f"url={remote_aoe_url}, error={e}"
        )
        return {
            "status": "error",
            "workflow_handle": "",
            "result": f"远端 AOE 调用失败: {str(e)}",
            "session_id": session_id,
            "remote_aoe_url": remote_aoe_url,
            "task_id": task_id,
        }

    finally:
        # 任务结束（无论成功/失败/超时）后从活跃会话中移除
        _active_remote_sessions.pop(session_id, None)


async def _cleanup_remote_session(
    remote_aoe_url: str,
    session_id: str,
    timeout: int = 10,
):
    """向远端 AOE 发送会话清理请求（退订 ALCM 引用计数）"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            await client.delete(
                f"{remote_aoe_url}/orchestration/session/{session_id}"
            )
        logger.debug(f"[跨主体] 会话清理成功: session_id={session_id}")
    except Exception as e:
        logger.debug(f"[跨主体] 会话清理失败（非关键）: {e}")


def find_alternative_remote_aoe(
    task: dict,
    failed_urls: list[str],
) -> "str | None":
    """
    §2.3 跨主体重编排核心：从 ARDC（含 Gossip peer agents）中寻找可替代的远端 AOE URL。

    策略：
    1. 以任务的 assigned_agent_id 查 capability
    2. 在全量 agent 列表（本地 + peer）中筛选同能力的在线 agent
    3. 跳过本地节点和已知失败 IP
    4. 返回第一个可用节点的 AOE URL（http://{ip}:{REMOTE_AOE_PORT}）

    Returns:
        可替代的 AOE URL，或 None（无可用替代节点）
    """
    import urllib.parse
    from src.service.agent_registry import get_registry_client

    registry = get_registry_client()

    # 获取任务所需 capability
    agent_id = task.get("assigned_agent_id", "")
    agent_info = registry.get_agent_by_id(agent_id)
    capability = agent_info.get("capability", "") if agent_info else ""

    # 取全量 agents（本地 + 所有 gossip peer）
    all_agents = registry.get_all_agents()
    candidates = [
        a for a in all_agents
        if a.get("status") == "online"
        and (not capability or a.get("capability") == capability)
    ]

    # 提取已失败 URL 的 hostname 集合
    tried_hosts: set[str] = set()
    for url in failed_urls:
        try:
            h = urllib.parse.urlparse(url).hostname
            if h:
                tried_hosts.add(h)
        except Exception:
            pass

    remote_port = int(os.getenv("REMOTE_AOE_PORT", "9000"))

    for agent in candidates:
        if _agent_is_local(agent):
            continue  # 跳过本机节点
        ip = agent.get("ip", "")
        if ip in tried_hosts:
            continue  # 跳过已失败的节点
        alt_url = f"http://{ip}:{remote_port}"
        logger.info(
            f"[§2.3 重编排] 找到替代节点: {alt_url} "
            f"(agent={agent['id']}, capability={capability})"
        )
        return alt_url

    logger.warning(
        f"[§2.3 重编排] 未找到替代节点 "
        f"(capability={capability}, tried={tried_hosts})"
    )
    return None


def identify_cross_host_tasks(
    execution_plan: list,
    available_agents: list,
) -> dict:
    """
    识别需要跨主体路由的任务

    遍历执行计划，将目标 Agent（或子工作流所属节点）不在本地的任务提取出来，
    生成 cross_host_sessions 映射。

    对于 sub_workflow_id 非空的任务，使用子工作流的 owner_ip 判断；
    否则使用 agent 的 ip 判断。

    Args:
        execution_plan:  TaskAssignment 列表
        available_agents: AgentInfo 列表（含 ip 字段）

    Returns:
        cross_host_sessions: {subtask_key: remote_aoe_url}
    """
    # 构建 agent_id → agent 映射
    agent_map = {a["id"]: a for a in available_agents if a.get("id")}
    local_aoe_host, local_aoe_port = _local_aoe_endpoint()

    cross_host = {}
    for task in execution_plan:
        swf_id = task.get("sub_workflow_id", "")
        if swf_id:
            # 子工作流任务：使用 target_ip（已由 Planner 填入 owner_ip）
            ip = task.get("target_ip", "localhost")
            port = task.get("target_port", 0)
            is_local = ip == local_aoe_host and int(port or 0) == int(local_aoe_port)
        else:
            # 普通 agent 任务
            agent_id = task.get("assigned_agent_id", "")
            agent = agent_map.get(agent_id)
            ip = agent.get("ip", "localhost") if agent else "localhost"
            port = 0
            is_local = _agent_is_local(agent)

        if not is_local:
            if port:
                cross_host[task["task_id"]] = f"http://{ip}:{port}"
            else:
                remote_port = int(os.getenv("REMOTE_AOE_PORT", "9000"))
                cross_host[task["task_id"]] = f"http://{ip}:{remote_port}"
    return cross_host


def _build_sub_workflow_prompt_section(sub_workflows: list) -> str:
    """构建子工作流 prompt 段落（供 Planner LLM 使用）"""
    if not sub_workflows:
        return ""
    lines = ["## 可用的子工作流（优先使用，作为整体调用）\n"]
    for swf in sub_workflows:
        pipeline = swf.get("pipeline", [])
        # 构建 pipeline 可读描述
        steps = []
        for step in pipeline:
            if isinstance(step, list):
                inner = ", ".join(s.get("description") or s.get("capability", "") for s in step)
                steps.append(f"[{inner}]")
            else:
                steps.append(step.get("description") or step.get("capability", ""))
        pipeline_desc = " -> ".join(steps)
        lines.append(
            f"- **{swf['id']}** (能力: {swf['capability']}, "
            f"位于: {swf['owner_ip']}:{swf['owner_port']}): "
            f"{swf['description']}\n"
            f"  流水线: {pipeline_desc}"
        )
    return "\n".join(lines) + "\n"


# ============================================================
# 分布式 Planner 节点 - 任务分解与 Agent 匹配
# ============================================================

async def distributed_planner_node(state: DistributedState) -> Command[Literal["executor", "__end__"]]:
    """
    分布式规划器节点
    
    功能：
    1. 理解用户请求
    2. 查询 L3 Agent 注册表
    3. 将任务分解并匹配到具体的 Agent IP
    4. 生成执行计划
    
    原 LangManus 的复用：
    - LLM 调用结构（get_llm_by_type）
    - 消息格式（MessagesState）
    
    新增逻辑：
    - L3 注册表查询
    - 任务-Agent 匹配算法
    - 生成包含 target_ip 的执行计划
    """
    logger.info("=== Distributed Planner 开始工作 ===")

    # ⚡ Pipeline 快速路径：预定义拓扑，跳过 LLM
    pipeline_topology = state.get("pipeline_topology", [])
    if pipeline_topology:
        logger.info(f"⚡ Pipeline 模式：跳过 LLM Planner，直接执行 {len(pipeline_topology)} 步拓扑")
        registry_client = get_registry_client()
        user_request = state["messages"][-1].content
        execution_plan: list[TaskAssignment] = []

        for step_idx, step in enumerate(pipeline_topology):
            if isinstance(step, list):
                # 并行组
                group_id = f"pg_{step_idx}"
                for sub_step in step:
                    agent_id = sub_step.get("agent_id", "")
                    agent_info = registry_client.get_agent_by_id(agent_id)
                    desc = sub_step.get("description") or sub_step.get("capability", "")
                    task_desc = f"{desc}\n\n用户请求：{user_request}" if desc else f"用户请求：{user_request}"
                    execution_plan.append({
                        "task_id": f"pipeline_{step_idx}_{len(execution_plan):03d}",
                        "task_title": f"[并行] {sub_step.get('capability', '')}",
                        "task_description": task_desc,
                        "assigned_agent_id": agent_id,
                        "target_ip": agent_info["ip"] if agent_info else "127.0.0.1",
                        "target_port": agent_info["port"] if agent_info else 8001,
                        "status": "pending",
                        "result": "",
                        "retry_count": 0,
                        "parallel_group": group_id,
                        "sub_workflow_id": "",
                    })
            else:
                # 串行步骤
                agent_id = step.get("agent_id", "")
                agent_info = registry_client.get_agent_by_id(agent_id)
                desc = step.get("description") or step.get("capability", "")
                task_desc = f"{desc}\n\n用户请求：{user_request}" if desc else f"用户请求：{user_request}"
                execution_plan.append({
                    "task_id": f"pipeline_{step_idx}_{len(execution_plan):03d}",
                    "task_title": step.get("capability", ""),
                    "task_description": task_desc,
                    "assigned_agent_id": agent_id,
                    "target_ip": agent_info["ip"] if agent_info else "127.0.0.1",
                    "target_port": agent_info["port"] if agent_info else 8001,
                    "status": "pending",
                    "result": "",
                    "retry_count": 0,
                    "parallel_group": "",
                    "sub_workflow_id": "",
                })

        plan_summary = f"⚡ Pipeline 模式：{len(execution_plan)} 步固定拓扑（无 LLM Planner）"

        # ── §2.2 跨主体识别：根据 Agent IP 预填 cross_host_sessions ────────
        all_agents = registry_client.get_all_agents()
        cross_host = identify_cross_host_tasks(execution_plan, all_agents)
        if cross_host:
            logger.info(f"[跨主体] 识别到 {len(cross_host)} 个跨节点任务: {cross_host}")

        return Command(
            update={
                "messages": [HumanMessage(content=plan_summary, name="planner")],
                "execution_plan": execution_plan,
                "current_task_index": 0,
                "plan_generated": True,
                "all_tasks_completed": False,
                "cross_host_sessions": cross_host,
                "agent_registry_cache": all_agents,
            },
            goto="executor"
        )

    # 1. 查询 L3 Agent 注册表
    registry_client = get_registry_client()
    available_agents = registry_client.query_agents()  # 获取所有在线 Agent
    
    if not available_agents:
        logger.error("没有可用的 L3 Agent！")
        return Command(
            update={
                "messages": [HumanMessage(
                    content="错误：当前没有可用的 L3 Agent，无法执行任务。",
                    name="planner"
                )],
                "plan_generated": False,
                "all_tasks_completed": True
            },
            goto="__end__"
        )
    
    logger.info(f"从注册表查询到 {len(available_agents)} 个可用 Agent")

    # 1.1 查询可用子工作流
    available_sub_workflows = registry_client.get_all_sub_workflows()
    if available_sub_workflows:
        logger.info(f"从注册表查询到 {len(available_sub_workflows)} 个可用子工作流")

    # 2. 构建 Agent 能力描述（用于 LLM 决策）
    agent_capabilities_desc = "\n".join([
        f"- **{agent['id']}** (IP: {agent['ip']}:{agent['port']}): {agent['description']}"
        for agent in available_agents
    ])
    
    # 3. 构建 Prompt（修改自原 planner.md）
    user_request = state["messages"][-1].content

    # 读取应用层 Skills 指引（若有）
    skills_content = state.get("skills_content", "").strip()
    skills_section = ""
    if skills_content:
        skills_section = f"""
## ⚡ 应用专属技能指引（优先遵守）

以下是本次任务所属应用的专属执行规范，**必须严格按照此指引规划任务，包括 Agent 选择顺序、输出格式要求等**：

{skills_content}

---
"""
        logger.info(f"[Planner] Skills 指引已注入 system_prompt, len={len(skills_content)}")
    
    system_prompt = f"""
---
当前时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
---

你是一个**分布式任务调度规划器**。你的任务是将用户请求分解为多个子任务，并将每个子任务分配给远程的 L3 Agent 或**子工作流**。
{skills_section}
## 可用的 L3 Agent 列表

{agent_capabilities_desc}

{_build_sub_workflow_prompt_section(available_sub_workflows)}

## 你的职责

1. **理解用户需求**：分析用户请求，识别需要哪些能力
2. **任务分解**：将复杂任务分解为多个独立的子任务
3. **匹配执行者**：为每个子任务选择最合适的 **Agent 或子工作流**
   - 如果某个子工作流的能力完全覆盖任务需求，**优先使用子工作流**（减少跨节点调用次数）
   - 否则分配给单个 Agent
4. **生成执行计划**：输出结构化的 JSON 计划

⚠️ **重要规则**：
- 如果用户询问的是关于**你自己的能力、身份、功能介绍**等元问题（如"你是谁"、"你能做什么"），请**不要生成任务**，返回空的任务列表即可
- 只有当用户请求**具体的执行任务**（如搜索、计算、分析）时，才生成任务并调度 Agent
- 元问题会由系统直接回答，无需调度 Agent

## 输出格式

直接输出 JSON 格式（不要用 ```json 包裹）：

```typescript
interface Task {{
  task_id: string;           // 唯一标识符，格式如 "task_001"
  task_title: string;        // 任务标题
  task_description: string;  // 详细的任务描述（告诉远程 Agent 做什么）
  assigned_agent_id: string; // 分配的 Agent ID（若使用子工作流，填子工作流 ID）
  capability_required: string; // 需要的能力
  sub_workflow_id?: string;  // 可选：若分配给子工作流，填子工作流 ID（如 "swf_xxx"）
}}

interface ExecutionPlan {{
  thought: string;           // 你的推理过程
  total_tasks: number;       // 任务总数
  tasks: Task[];            // 任务列表
}}
```

## 注意事项

- 确保每个任务的 `task_description` 足够详细，让远程 Agent 能够独立执行
- 根据 Agent 的能力进行合理匹配
- 如果任务匹配某个子工作流，设置 `sub_workflow_id` 字段，系统会自动路由到对应节点
- 任务之间如果有依赖关系，应该在 `task_description` 中说明
- 使用与用户相同的语言生成计划
"""
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"用户请求：{user_request}")
    ]
    
    # 4. 调用 LLM 生成计划（复用原 LangManus 的 LLM 调用）
    llm = get_llm_by_type("reasoning")  # 使用推理模型
    
    try:
        # qwq-plus 仅支持流式调用，需要手动收集响应
        full_response = ""
        try:
            # 尝试异步流式调用（适用于 qwq-plus）
            async for chunk in llm.astream(messages):
                if hasattr(chunk, 'content'):
                    full_response += chunk.content
        except Exception as stream_err:
            logger.warning(f"流式调用失败，尝试非流式调用: {stream_err}")
            # 回退到异步非流式调用
            response = await llm.ainvoke(messages)
            full_response = response.content
        
        plan_json_str = full_response
        
        # 清理 JSON 字符串（移除代码块标记）
        # 处理多种语言标记：```json, ```typescript, ```python 等
        import re
        # 移除开头的代码块标记
        plan_json_str = re.sub(r'^```[a-z]*\n?', '', plan_json_str.strip())
        # 移除结尾的代码块标记
        plan_json_str = re.sub(r'\n?```$', '', plan_json_str.strip())
        
        plan_json_str = plan_json_str.strip()
        
        logger.debug(f"LLM 生成的计划：\n{plan_json_str}")
        
        # 5. 解析计划
        plan_data = json.loads(plan_json_str)
        
        # 6. 将计划转换为 TaskAssignment 格式（补充 IP 和端口信息）
        execution_plan: list[TaskAssignment] = []
        
        for task in plan_data.get("tasks", []):
            agent_id = task.get("assigned_agent_id")
            swf_id = task.get("sub_workflow_id", "")

            # 子工作流路径：从子工作流列表查找 owner_ip/port
            if swf_id:
                swf_info = next(
                    (s for s in available_sub_workflows if s["id"] == swf_id),
                    None,
                )
                if not swf_info:
                    logger.warning(f"子工作流 {swf_id} 不存在，跳过任务 {task.get('task_id')}")
                    continue
                target_ip = swf_info["owner_ip"]
                target_port = swf_info["owner_port"]
                agent_id = swf_id  # 统一用子工作流 ID
            else:
                # 普通 Agent 路径
                agent_info = registry_client.get_agent_by_id(agent_id)
                if not agent_info:
                    logger.warning(f"Agent {agent_id} 不存在，跳过任务 {task.get('task_id')}")
                    continue
                target_ip = agent_info["ip"]
                target_port = agent_info["port"]

            task_assignment: TaskAssignment = {
                "task_id": task.get("task_id", f"task_{uuid.uuid4().hex[:8]}"),
                "task_title": task.get("task_title", "未命名任务"),
                "task_description": task.get("task_description", ""),
                "assigned_agent_id": agent_id,
                "target_ip": target_ip,
                "target_port": target_port,
                "status": "pending",
                "result": "",
                "retry_count": 0,
                "parallel_group": "",
                "sub_workflow_id": swf_id,
            }
            execution_plan.append(task_assignment)
        
        logger.info(f"成功生成执行计划，共 {len(execution_plan)} 个任务")

        # 生成 UI 展示用的分解日志
        try:
            ui_log = "任务分解：\n"
            for task in execution_plan:
                # 尝试使用中文角色名（如果有映射），这里直接使用 agent_id
                role = task['assigned_agent_id']
                # 使用标题或简短描述
                action = task['task_title']
                ui_log += f"{role}：{action}\n"
            
            # 使用特殊前缀以便 Web UI 捕获
            logger.info(f"UI_LOG_EVENT:{ui_log.strip()}")
        except Exception as e:
            logger.warning(f"生成 UI 日志失败: {e}")
        
        # 7. 特殊处理：如果没有任务（用户询问系统能力等元问题）
        if len(execution_plan) == 0:
            logger.info("没有生成任务，识别为系统介绍类问题，直接生成回答")
            
            # 使用基础 LLM 直接回答
            direct_answer_prompt = f"""
你是一个**分布式任务调度器**（Layer 2 Scheduler）。

用户询问：{user_request}

请直接回答用户的问题。说明你的核心功能：

1. **任务分解与规划**：将复杂用户请求分解为多个子任务
2. **智能体调度**：从 {len(available_agents)} 个可用 Agent 中选择最合适的执行者
3. **分布式执行**：通过 HTTP 协议调用远程 Agent 完成任务
4. **结果汇总**：收集所有任务结果并生成最终报告

当前可用的 Agent 能力：
{agent_capabilities_desc}

你可以处理的任务类型包括：
- 网络搜索和信息检索
- 数值计算和数据分析
- 图像识别和视觉理解
- 自然语言处理和文本总结
- 代码生成和技术分析
- 网页交互和自动化操作

请用友好的语气回答用户。
"""
            
            basic_llm = get_llm_by_type("basic")
            try:
                direct_response = ""
                async for chunk in basic_llm.astream([HumanMessage(content=direct_answer_prompt)]):
                    if hasattr(chunk, 'content'):
                        direct_response += chunk.content
            except:
                direct_response = (await basic_llm.ainvoke([HumanMessage(content=direct_answer_prompt)])).content
            
            return Command(
                update={
                    "messages": [HumanMessage(content=direct_response, name="system")],
                    "execution_plan": [],
                    "plan_generated": False,
                    "all_tasks_completed": True,
                    "agent_registry_cache": available_agents
                },
                goto="__end__"
            )
        
        # 8. §2.2 跨主体识别：根据 Agent IP 预填 cross_host_sessions
        cross_host = identify_cross_host_tasks(execution_plan, available_agents)
        if cross_host:
            logger.info(f"[跨主体] 识别到 {len(cross_host)} 个跨节点任务: {cross_host}")

        # 9. 正常情况：更新状态并跳转到执行器
        plan_summary = f"""
## 📋 任务规划完成

**思考过程**：{plan_data.get('thought', 'N/A')}

**任务总数**：{len(execution_plan)}

**任务列表**：
"""
        for i, task in enumerate(execution_plan, 1):
            plan_summary += f"\n{i}. **{task['task_title']}** → Agent: `{task['assigned_agent_id']}` (IP: {task['target_ip']}:{task['target_port']})"

        return Command(
            update={
                "messages": [HumanMessage(content=plan_summary, name="planner")],
                "execution_plan": execution_plan,
                "current_task_index": 0,
                "plan_generated": True,
                "all_tasks_completed": False,
                "agent_registry_cache": available_agents,
                "registry_last_update": datetime.now().isoformat(),
                "cross_host_sessions": cross_host,
            },
            goto="executor"
        )
        
    except json.JSONDecodeError as e:
        logger.error(f"无法解析 LLM 返回的 JSON：{e}")
        return Command(
            update={
                "messages": [HumanMessage(
                    content=f"错误：生成的计划格式不正确。\n{str(e)}",
                    name="planner"
                )],
                "plan_generated": False,
                "all_tasks_completed": True
            },
            goto="__end__"
        )
    except Exception as e:
        logger.error(f"Planner 节点出错：{e}", exc_info=True)
        return Command(
            update={
                "messages": [HumanMessage(
                    content=f"错误：规划器执行失败。\n{str(e)}",
                    name="planner"
                )],
                "plan_generated": False,
                "all_tasks_completed": True
            },
            goto="__end__"
        )


# ============================================================
# 分布式 Executor 节点 - 远程 Agent 调用
# ============================================================

async def distributed_executor_node(state: DistributedState) -> Command[Literal["monitor", "__end__"]]:
    """
    分布式执行器节点（统一执行层版本）
    
    功能：
    1. 从执行计划中取出当前任务
    2. 使用 UnifiedExecutor 智能选择 MCP 或 A2A
    3. 处理响应和错误
    
    协议选择逻辑：
    - MCP: 明确的工具调用（搜索、文件操作等）
    - A2A: 需要 Agent 推理的复杂任务
    - 自动降级: MCP 失败后自动切换到 A2A
    """
    from .unified_executor import UnifiedExecutor
    
    logger.info("=== Unified Executor 开始执行 ===")
    
    # 1. 检查是否有任务需要执行
    if not state.get("execution_plan"):
        logger.error("执行计划为空！")
        return Command(
            update={
                "messages": [HumanMessage(
                    content="错误：没有可执行的任务。",
                    name="executor"
                )],
                "all_tasks_completed": True
            },
            goto="__end__"
        )
    
    current_index = state.get("current_task_index", 0)
    execution_plan = state["execution_plan"]
    
    if current_index >= len(execution_plan):
        logger.info("所有任务已完成！")
        return Command(
            update={
                "all_tasks_completed": True
            },
            goto="monitor"
        )
    
    # 2. 获取当前任务
    current_task = execution_plan[current_index]
    logger.info(f"执行任务 {current_index + 1}/{len(execution_plan)}: {current_task['task_title']}")

    # --- 状态流转保护：如果当前任务尚未被标记为 running（仍为 pending），
    # 先把任务（或并行组内的所有任务）标为 running 并返回更新，以便上层
    # 的 state 发布逻辑把这一变化推到可视化层。再次进入本节点时会执行实际任务。
    cur_status = current_task.get("status", "pending")
    if cur_status not in ("running", "completed", "failed"):
        # 并行组：把从 current_index 开始的同组任务都标成 running（若尚未完成）
        if current_task.get("parallel_group"):
            # 通过 UnifiedExecutor 预判本次任务将使用的协议/工具，便于可视化展示
            try:
                ue_for_meta = UnifiedExecutor()
            except Exception:
                ue_for_meta = None

            updated_plan = [dict(t) for t in state.get("execution_plan", [])]
            idx = current_index
            pg = current_task.get("parallel_group")
            while idx < len(updated_plan) and updated_plan[idx].get("parallel_group") == pg:
                if updated_plan[idx].get("status") not in ("running", "completed", "failed"):
                    updated_plan[idx]["status"] = "running"
                    # 预填 metadata（如果尚未存在）
                    if ue_for_meta and not updated_plan[idx].get("metadata"):
                        try:
                            task_meta = updated_plan[idx]
                            tool_conf = ue_for_meta._try_match_builtin_tool(task_meta) or ue_for_meta._try_match_mcp_tool(task_meta)
                            if tool_conf:
                                proto = "builtin" if tool_conf.get("name") and tool_conf.get("capability") else "mcp"
                                updated_plan[idx]["metadata"] = {
                                    "protocol": proto,
                                    "executor": f"{tool_conf.get('capability','')}.{tool_conf.get('name','')}".strip(".")
                                }
                            else:
                                updated_plan[idx]["metadata"] = {
                                    "protocol": "UNKNOWN",
                                    "executor": updated_plan[idx].get("assigned_agent_id", "unknown")
                                }
                        except Exception:
                            updated_plan[idx]["metadata"] = {
                                "protocol": "UNKNOWN",
                                "executor": updated_plan[idx].get("assigned_agent_id", "unknown")
                            }
                idx += 1
            return Command(
                update={
                    "execution_plan": updated_plan,
                    "current_task_index": current_index,
                },
                goto="executor"
            )
        else:
            # 串行单任务：标记为 running 并返回，同时预填执行方式 metadata 以便前端显示
            try:
                ue_for_meta = UnifiedExecutor()
            except Exception:
                ue_for_meta = None

            updated_plan = [dict(t) for t in state.get("execution_plan", [])]
            if updated_plan and current_index < len(updated_plan):
                updated_plan[current_index]["status"] = "running"
                if ue_for_meta and not updated_plan[current_index].get("metadata"):
                    try:
                        task_meta = updated_plan[current_index]
                        tool_conf = ue_for_meta._try_match_builtin_tool(task_meta) or ue_for_meta._try_match_mcp_tool(task_meta)
                        if tool_conf:
                            proto = "builtin" if tool_conf.get("name") and tool_conf.get("capability") else "mcp"
                            updated_plan[current_index]["metadata"] = {
                                "protocol": proto,
                                "executor": f"{tool_conf.get('capability','')}.{tool_conf.get('name','')}".strip(".")
                            }
                        else:
                            updated_plan[current_index]["metadata"] = {
                                "protocol": "UNKNOWN",
                                "executor": updated_plan[current_index].get("assigned_agent_id", "unknown")
                            }
                    except Exception:
                        updated_plan[current_index]["metadata"] = {
                            "protocol": "UNKNOWN",
                            "executor": updated_plan[current_index].get("assigned_agent_id", "unknown")
                        }
            return Command(
                update={
                    "execution_plan": updated_plan,
                    "current_task_index": current_index,
                },
                goto="executor"
            )

    # ⚡ 并行组：收集同组任务，并发执行
    parallel_group = current_task.get("parallel_group", "")
    if parallel_group:
        # 收集从 current_index 开始的连续同组任务
        group_tasks: list[tuple[int, dict]] = []
        idx = current_index
        while idx < len(execution_plan) and execution_plan[idx].get("parallel_group") == parallel_group:
            group_tasks.append((idx, execution_plan[idx]))
            idx += 1

        logger.info(f"⚡ 并行执行 {len(group_tasks)} 个任务（组: {parallel_group}）")
        _par_executor = UnifiedExecutor()

        async def _exec_one(task_idx_and_task: tuple[int, dict]) -> dict:
            task_idx, task = task_idx_and_task
            _t0 = time.monotonic()
            try:
                # 回调：在决策后立即更新 VizBus（写入 execution_plan 的 metadata）
                def _on_decision(predicted: dict):
                    try:
                        from src.service.viz_bus import get_viz_bus
                        bus = get_viz_bus()
                        entry = bus.latest_running()
                        if not entry:
                            return
                        wid = entry.id
                        snap = dict(state or {}) if 'state' in locals() else {}
                        snap = snap or {}
                        snap['execution_plan'] = [dict(t) for t in state.get('execution_plan', [])]
                        if 0 <= task_idx < len(snap['execution_plan']):
                            snap['execution_plan'][task_idx].setdefault('metadata', {})
                            snap['execution_plan'][task_idx]['metadata'].update({
                                'protocol': predicted.get('protocol', 'UNKNOWN'),
                                'executor': predicted.get('executor', snap['execution_plan'][task_idx].get('assigned_agent_id', 'unknown'))
                            })
                        bus.update_state(wid, snap, node_name='executor.decision')
                    except Exception:
                        pass

                _res = await _par_executor.execute_task((task_idx, task)[1] if False else task, on_decision=_on_decision)
                _latency = (time.monotonic() - _t0) * 1000
                try:
                    from src.runtime.qos_monitor import get_qos_monitor
                    get_qos_monitor().record_call(
                        agent_id=task.get("assigned_agent_id", "unknown"),
                        latency_ms=_latency,
                        success=(_res.get("status") == "success"),
                    )
                except Exception:
                    pass
                return _res
            except Exception as _e:
                _latency = (time.monotonic() - _t0) * 1000
                try:
                    from src.runtime.qos_monitor import get_qos_monitor
                    get_qos_monitor().record_call(
                        agent_id=task.get("assigned_agent_id", "unknown"),
                        latency_ms=_latency,
                        success=False,
                    )
                except Exception:
                    pass
                return {"status": "error", "error_message": str(_e), "protocol": "error", "result": ""}

        par_results = await asyncio.gather(*[_exec_one((i, t)) for (i, t) in group_tasks])

        updated_plan = state["execution_plan"].copy()
        failed_tasks = state.get("failed_tasks", []).copy()
        result_texts: list[str] = []

        for (task_idx, task), par_rd in zip(group_tasks, par_results):
            if par_rd.get("status") == "success":
                updated_plan[task_idx]["status"] = "completed"
                updated_plan[task_idx]["result"] = par_rd.get("result", "")
                result_texts.append(f"✅ {task['task_title']}: {par_rd.get('result', '')[:500]}")
            else:
                updated_plan[task_idx]["status"] = "failed"
                err = par_rd.get("error_message", "未知错误")
                updated_plan[task_idx]["result"] = f"失败: {err}"
                result_texts.append(f"❌ {task['task_title']}: {err}")
                failed_tasks.append(task["task_id"])

        response_text = (
            f"### ⚡ 并行任务组 `{parallel_group}` 执行完毕（{len(group_tasks)} 个任务）\n\n"
            + "\n\n".join(result_texts)
        )
        return Command(
            update={
                "messages": [HumanMessage(content=response_text, name="executor")],
                "execution_plan": updated_plan,
                "current_task_index": idx,
                "failed_tasks": failed_tasks,
            },
            goto="monitor"
        )

    # 3. 使用统一执行层执行任务（串行）
    executor = UnifiedExecutor()

    # ─── 跨主体路由检查 ───────────────────────────────────────────────
    cross_host_sessions = state.get("cross_host_sessions", {})
    remote_aoe_url = cross_host_sessions.get(current_task["task_id"])
    failed_cross_host_tasks = list(state.get("failed_cross_host_tasks", []))
    failed_remote_aoe_urls: dict[str, list[str]] = dict(state.get("failed_remote_aoe_urls", {}))

    if remote_aoe_url:
        logger.info(
            f"[跨主体] 任务 {current_task['task_id']} 路由到远端 AOE: {remote_aoe_url}"
        )
        session_timeout = state.get("session_timeout_seconds", 60)
        xh_result = await dispatch_subtask_to_remote_aoe(
            subtask={
                **current_task,
                "timeout_seconds": session_timeout,
            },
            remote_aoe_url=remote_aoe_url,
            session_timeout=session_timeout,
        )
        _task_latency = 0.0  # 远端执行，延迟由远端负责
        if xh_result.get("status") == "completed":
            task_status = "completed"
            result_message = xh_result.get("result", "")
            result_data = {"status": "success", "protocol": "cross_host", "agent_used": remote_aoe_url}
            logger.info(f"[跨主体] ✅ 子任务完成: {current_task['task_id']}")
        else:
            # 超时或错误 → 降级为本地 LLM 规划
            task_status = "failed"
            err_status = xh_result.get("status", "error")
            result_message = xh_result.get("result", f"跨主体执行失败({err_status})")
            result_data = {"status": "error", "protocol": "cross_host", "error_message": result_message}
            if current_task["task_id"] not in failed_cross_host_tasks:
                failed_cross_host_tasks.append(current_task["task_id"])
            # §2.3 重编排：记录本次失败的远端 URL，供 find_alternative_remote_aoe 排除
            task_failed_urls = list(failed_remote_aoe_urls.get(current_task["task_id"], []))
            if remote_aoe_url and remote_aoe_url not in task_failed_urls:
                task_failed_urls.append(remote_aoe_url)
            failed_remote_aoe_urls[current_task["task_id"]] = task_failed_urls
            logger.warning(
                f"[跨主体] ❌ 子任务失败（{err_status}），标记进入重规划: "
                f"{current_task['task_id']}, 已失败节点: {task_failed_urls}"
            )
        try:
            from src.runtime.qos_monitor import get_qos_monitor
            get_qos_monitor().record_call(
                agent_id=current_task.get("assigned_agent_id", "unknown"),
                latency_ms=0.0,
                success=(task_status == "completed"),
            )
        except Exception:
            pass

    # 初始化 Demo 故障注入标志（跨所有执行路径共用）
    _demo_vehicleB_inject_fail = False

    # ─── 本地子工作流执行路径 ──────────────────────────────────────────
    _local_swf_id = current_task.get("sub_workflow_id", "")
    if not remote_aoe_url and _local_swf_id:
        logger.info(f"[子工作流] 本地执行子工作流: {_local_swf_id}")
        _task_start = time.monotonic()
        try:
            from src.service.agent_registry import get_registry_client as _get_reg
            _swf_def = _get_reg().get_sub_workflow_by_id(_local_swf_id)
            if not _swf_def:
                raise ValueError(f"子工作流 {_local_swf_id} 未在本地注册")
            from src.distributed_workflow import run_distributed_workflow
            _swf_result = await run_distributed_workflow(
                user_input=current_task["task_description"],
                pipeline_topology=_swf_def["pipeline"],
                adaptive_mode=False,
                timeout_seconds=state.get("timeout_seconds", 30),
            )
            _task_latency = (time.monotonic() - _task_start) * 1000
            # 从子工作流结果中提取最终报告
            _swf_messages = _swf_result.get("messages", [])
            result_message = _swf_messages[-1].content if _swf_messages else str(_swf_result)
            task_status = "completed"
            result_data = {"status": "success", "protocol": "sub_workflow", "agent_used": _local_swf_id}
            logger.info(f"[子工作流] ✅ 本地子工作流执行完成: {_local_swf_id}")
        except Exception as e:
            _task_latency = (time.monotonic() - _task_start) * 1000
            task_status = "failed"
            result_message = f"子工作流执行失败: {str(e)}"
            result_data = {"status": "error", "protocol": "sub_workflow", "error_message": str(e)}
            logger.error(f"[子工作流] ❌ {result_message}")
        try:
            from src.runtime.qos_monitor import get_qos_monitor
            get_qos_monitor().record_call(
                agent_id=_local_swf_id, latency_ms=_task_latency,
                success=(task_status == "completed"),
            )
        except Exception:
            pass

    if not remote_aoe_url and not _local_swf_id:
        # ─── 本地 Agent 执行路径 ──────────────────────────

        _task_start = time.monotonic()

        # 回调：在决策后立即更新 VizBus（写入 execution_plan 的 metadata）
        def _on_decision_serial(predicted: dict):
            try:
                from src.service.viz_bus import get_viz_bus
                bus = get_viz_bus()
                entry = bus.latest_running()
                if not entry:
                    return
                wid = entry.id
                snap = dict(state or {}) if 'state' in locals() else {}
                snap = snap or {}
                snap['execution_plan'] = [dict(t) for t in state.get('execution_plan', [])]
                if 0 <= current_index < len(snap['execution_plan']):
                    snap['execution_plan'][current_index].setdefault('metadata', {})
                    snap['execution_plan'][current_index]['metadata'].update({
                        'protocol': predicted.get('protocol', 'UNKNOWN'),
                        'executor': predicted.get('executor', snap['execution_plan'][current_index].get('assigned_agent_id', 'unknown'))
                    })
                bus.update_state(wid, snap, node_name='executor.decision')
            except Exception:
                pass

        try:
            if _demo_vehicleB_inject_fail:
                raise RuntimeError("VehicleB 感知节点连接中断（演示模式）")

            result_data = await executor.execute_task(current_task, on_decision=_on_decision_serial)
            _task_latency = (time.monotonic() - _task_start) * 1000
            try:
                from src.runtime.qos_monitor import get_qos_monitor
                get_qos_monitor().record_call(
                    agent_id=current_task.get("assigned_agent_id", "unknown"),
                    latency_ms=_task_latency,
                    success=(result_data.get("status") == "success"),
                )
            except Exception:
                pass

            if result_data["status"] == "success":
                task_status = "completed"
                result_message = result_data["result"]
                protocol_info = result_data.get("protocol", "unknown").upper()
                tool_or_agent = result_data.get("tool_used") or result_data.get("agent_used", "unknown")
                logger.info(f"✅ 任务执行成功 [协议: {protocol_info}, 执行者: {tool_or_agent}]")
            else:
                task_status = "failed"
                error_msg = result_data.get("error_message", "未知错误")
                result_message = f"任务执行失败: {error_msg}"
                logger.error(f"❌ {result_message}")
                if result_data.get("protocol") == "a2a":
                    logger.warning("⚠️ MCP 和 A2A 都失败了")

        except Exception as e:
            logger.error(f"❌ Unified Executor 执行异常: {e}", exc_info=True)
            task_status = "failed"
            result_message = f"执行异常: {str(e)}"
            result_data = {
                "status": "error",
                "protocol": "unknown",
                "error_message": str(e)
            }
    
    # 4. 兼容性处理：如果统一执行层不可用，降级到 LLM 模拟器
    # Demo VehicleB 故障不允许 LLM 模拟器救活
    if task_status == "failed" and USE_LLM_SIMULATOR and not _demo_vehicleB_inject_fail:
        logger.info(f"🤖 降级到 LLM 智能体模拟器（模型: {LLM_SIMULATOR_MODEL}）")
        
        try:
            simulator = get_llm_agent_simulator(
                use_reasoning_model=(LLM_SIMULATOR_MODEL == "reasoning")
            )
            
            # 提取前序任务结果作为上下文
            previous_results = "\n\n".join([
                f"### {task['task_title']}\n{task['result']}"
                for task in execution_plan[:current_index]
                if task.get('status') == 'completed' and task.get('result')
            ])
            
            context = {
                "user_request": state["messages"][0].content if state["messages"] else "",
                "previous_results": previous_results
            }
            
            # 调用 LLM 生成智能体响应（使用异步版本，避免阻塞事件循环）
            mock_result = await simulator.simulate_agent_call(
                agent_id=current_task.get('assigned_agent_id', ''),
                task_title=current_task.get('task_title', ''),
                task_description=current_task.get('task_description', ''),
                context=context
            )
            
            task_status = "completed"
            result_message = mock_result
            result_data = {
                "status": "success",
                "protocol": "llm_simulator",
                "tool_used": f"LLM_{LLM_SIMULATOR_MODEL}"
            }
            
            logger.info("✅ LLM 模拟器执行成功")
            
        except Exception as llm_error:
            logger.error(f"❌ LLM 模拟器也失败: {llm_error}")
            result_message = f"所有执行方式都失败: {llm_error}"
            task_status = "failed"
    
    # 5. 更新任务状态
    updated_plan = state["execution_plan"].copy()
    updated_plan[current_index]["status"] = task_status
    updated_plan[current_index]["result"] = result_message
    updated_plan[current_index]["retry_count"] += 1
    
    # 记录使用的协议信息
    if 'result_data' in locals() and result_data:
        updated_plan[current_index]["metadata"] = {
            "protocol": result_data.get("protocol", "unknown"),
            "executor": result_data.get("tool_used") or result_data.get("agent_used", "unknown")
        }
    
    # 6. 决定下一步
    failed_tasks = state.get("failed_tasks", [])
    if task_status == "failed":
        failed_tasks.append(current_task["task_id"])

    # 构造结果展示（简洁版）
    protocol_info = ""
    if 'result_data' in locals() and result_data:
        protocol = result_data.get("protocol", "").upper()
        executor_name = result_data.get("tool_used") or result_data.get("agent_used", "")
        protocol_info = f"\n**执行方式**: {protocol} ({executor_name})"
    
    response_text = f"""
### ✅ 任务 {current_index + 1}/{len(execution_plan)}: {current_task['task_title']}
{protocol_info}

{result_message[:1000]}{"..." if len(result_message) > 1000 else ""}
"""
    
    # 7. 移动到下一个任务
    next_index = current_index + 1
    
    return Command(
        update={
            "messages": [HumanMessage(content=response_text, name="executor")],
            "execution_plan": updated_plan,
            "current_task_index": next_index,
            "failed_tasks": failed_tasks,
            "failed_cross_host_tasks": failed_cross_host_tasks,
            "failed_remote_aoe_urls": failed_remote_aoe_urls,
        },
        goto="monitor"
    )


# ============================================================
# Monitor 节点 - 监控和路由
# ============================================================

async def distributed_monitor_node(state: DistributedState) -> Command[Literal["executor", "reporter", "__end__"]]:
    """
    增强监控节点（混合编排模式）
    
    功能：
    1. 检查任务完成度
    2. 检测失败任务
    3. 应用规则路由（快速恢复）
    4. 必要时触发 LLM 重新规划
    
    编排模式：
    - 正常情况：静态顺序执行（高效）
    - 失败情况：规则路由 → LLM 重规划（鲁棒）
    """
    logger.info("=== Distributed Monitor 开始工作 ===")
    
    execution_plan = state.get("execution_plan", [])
    current_index = state.get("current_task_index", 0)
    replanning_count = state.get("replanning_count", 0)
    max_replanning = state.get("max_replanning", 2)
    replanning_enabled = state.get("replanning_enabled", True)
    
    # ========== 1. 检查任务完成度 ==========
    if current_index >= len(execution_plan):
        logger.info("✅ 所有任务已完成，跳转到 Reporter")
        return Command(
            update={"all_tasks_completed": True},
            goto="reporter"
        )
    
    # ========== 2. 检测失败任务 ==========
    failed_tasks_list = [t for t in execution_plan if t["status"] == "failed"]
    
    if not failed_tasks_list:
        # 无失败任务，正常继续
        logger.info(f"📊 任务进度：{current_index}/{len(execution_plan)}，继续执行")
        return Command(goto="executor")
    
    # ========== 3. 处理失败任务 ==========
    logger.warning(f"⚠️  检测到 {len(failed_tasks_list)} 个失败任务")
    
    # 3.1 尝试规则路由（无需 LLM）
    if replanning_enabled:
        rule_result = apply_failure_rules(state, failed_tasks_list)
        if rule_result["handled"]:
            logger.info(f"✅ 规则路由成功处理失败：{rule_result['action']}")
            return Command(
                update=rule_result["state_update"],
                goto="executor"
            )
    
    # 3.2 规则无法处理，考虑 LLM 重规划
    if replanning_enabled and replanning_count < max_replanning:
        logger.info(f"🔄 启动 LLM 重新规划（第 {replanning_count + 1}/{max_replanning} 次）")
        # 传入跨主体失败的 Agent 列表，使规划器不再分配给这些 Agent
        failed_cross_host = state.get("failed_cross_host_tasks", [])
        excluded = list({
            t["assigned_agent_id"]
            for t in failed_tasks_list
            if t["task_id"] in failed_cross_host
        })
        return await trigger_llm_replanning(state, failed_tasks_list, excluded_agents=excluded or None)
    
    # 3.3 超过重规划次数，失败
    if replanning_count >= max_replanning:
        logger.error(f"❌ 已达到最大重规划次数（{max_replanning}），任务失败")
        return Command(
            update={
                "all_tasks_completed": True,
                "failed_tasks": [t["task_id"] for t in failed_tasks_list],
                "messages": [HumanMessage(
                    content=f"错误：任务执行失败，已尝试重规划 {replanning_count} 次仍无法完成。\n\n失败任务：{[t['task_title'] for t in failed_tasks_list]}",
                    name="monitor"
                )]
            },
            goto="reporter"
        )
    
    # 3.4 未启用重规划，直接继续
    logger.info("继续执行剩余任务（未启用重规划）")
    return Command(goto="executor")


def apply_failure_rules(state: DistributedState, failed_tasks_list: list) -> dict:
    """
    应用规则路由处理失败（无需 LLM）
    
    规则优先级：
    1. 简单重试（retry_count < 3）
    2. 切换备用 Agent
    3. 添加前置任务（如数据清洗）
    
    返回：
    {
        "handled": bool,  # 是否成功处理
        "action": str,    # 采取的动作
        "state_update": dict  # 状态更新
    }
    """
    from src.service.agent_registry import get_registry_client

    execution_plan = state.get("execution_plan", [])
    registry_client = get_registry_client()

    # ── 规则 0：跨主体故障切换（§2.3 重编排，最高优先级）──────────────────
    # 当任务已被标记为跨主体失败时，优先尝试切换到其他远端节点，
    # 而不是在同一失败节点上重试（Rule 1 会重试同节点，无意义）。
    failed_cross_host = state.get("failed_cross_host_tasks", [])
    failed_remote_aoe_urls = state.get("failed_remote_aoe_urls", {})
    cross_host_sessions = dict(state.get("cross_host_sessions", {}))

    for task in failed_tasks_list:
        if task["task_id"] not in failed_cross_host:
            continue  # 非跨主体失败，跳过

        task_failed_urls = failed_remote_aoe_urls.get(task["task_id"], [])
        alt_url = find_alternative_remote_aoe(task, task_failed_urls)
        if alt_url:
            logger.info(
                f"规则 0（§2.3 重编排）：跨主体故障切换 "
                f"{task['task_id']} → {alt_url}"
            )
            cross_host_sessions[task["task_id"]] = alt_url
            task["status"] = "pending"
            # 找到此 task 在 execution_plan 中的索引，重置 current_task_index
            task_idx = next(
                (i for i, t in enumerate(execution_plan) if t["task_id"] == task["task_id"]),
                None,
            )
            state_update: dict = {
                "execution_plan": execution_plan,
                "cross_host_sessions": cross_host_sessions,
            }
            if task_idx is not None:
                state_update["current_task_index"] = task_idx
            return {
                "handled": True,
                "action": f"跨主体故障切换至 {alt_url}",
                "state_update": state_update,
            }
        # 无可用替代节点 → 不设 handled=True，让后续 LLM 重规划处理
        logger.warning(
            f"规则 0：任务 {task['task_id']} 无可用替代节点，降级至 LLM 重规划"
        )

    # ── 规则 1：简单重试（仅限非跨主体失败任务，避免在失败节点上无效重试）─
    for task in failed_tasks_list:
        if task["task_id"] in failed_cross_host:
            continue  # 跨主体失败且无替代节点 → 留给 LLM 重规划
        if task["retry_count"] < 3:
            logger.info(f"规则 1：任务 {task['task_id']} 重试（第 {task['retry_count'] + 1} 次）")
            task["status"] = "pending"
            task["retry_count"] += 1
            return {
                "handled": True,
                "action": f"重试任务 {task['task_title']}",
                "state_update": {"execution_plan": execution_plan}
            }
    
    # 规则 2：切换备用 Agent（如搜索引擎切换）
    for task in failed_tasks_list:
        capability = task.get("capability_required", "")
        if capability:
            # 查询同能力的其他 Agent
            alternative_agents = registry_client.query_agents(capability=capability)
            alternative_agents = [a for a in alternative_agents if a["id"] != task["assigned_agent_id"]]
            
            if alternative_agents:
                backup_agent = alternative_agents[0]
                logger.info(f"规则 2：切换到备用 Agent {backup_agent['id']}")
                task["assigned_agent_id"] = backup_agent["id"]
                task["target_ip"] = backup_agent["ip"]
                task["target_port"] = backup_agent["port"]
                task["status"] = "pending"
                task["retry_count"] = 0
                return {
                    "handled": True,
                    "action": f"切换到备用 Agent {backup_agent['id']}",
                    "state_update": {"execution_plan": execution_plan}
                }
    
    # 规则 3：添加数据清洗任务（针对数据格式错误）
    for task in failed_tasks_list:
        if "compute" in task.get("capability_required", "") and "格式" in task.get("result", ""):
            logger.info("规则 3：检测到数据格式问题，添加数据清洗任务")
            
            # 在失败任务前插入清洗任务
            task_index = execution_plan.index(task)
            cleaning_task: TaskAssignment = {
                "task_id": f"task_cleaning_{task['task_id']}",
                "task_title": "数据清洗",
                "task_description": f"清洗和格式化数据，为 {task['task_title']} 做准备",
                "assigned_agent_id": "compute_agent_001",  # 使用计算 Agent
                "target_ip": "192.168.1.11",
                "target_port": 8080,
                "status": "pending",
                "result": "",
                "retry_count": 0
            }
            
            execution_plan.insert(task_index, cleaning_task)
            task["status"] = "pending"  # 重置原任务
            task["retry_count"] = 0
            
            return {
                "handled": True,
                "action": "添加数据清洗前置任务",
                "state_update": {
                    "execution_plan": execution_plan,
                    "current_task_index": task_index  # 从清洗任务开始
                }
            }
    
    # 规则无法处理
    return {"handled": False, "action": "无适用规则", "state_update": {}}


async def trigger_llm_replanning(
    state: DistributedState,
    failed_tasks_list: list,
    excluded_agents: list | None = None,
) -> Command[Literal["executor", "reporter"]]:
    """
    触发 LLM 重新规划

    当规则路由无法处理时，使用 LLM 进行智能重新规划。

    Args:
        state:             当前工作流状态
        failed_tasks_list: 失败的任务列表
        excluded_agents:   需要排除的 Agent ID 列表（用于跨主体故障切换场景）
    """
    from src.agents.llm import get_llm_by_type
    import uuid
    
    execution_plan = state.get("execution_plan", [])
    replanning_count = state.get("replanning_count", 0)
    user_request = state["messages"][0].content
    completed_tasks = [t for t in execution_plan if t["status"] == "completed"]

    # 排除故障 Agent（跨主体故障切换场景）
    excluded_agents = excluded_agents or []
    available_agents = [
        a for a in state.get("agent_registry_cache", [])
        if a["id"] not in excluded_agents
    ]
    excluded_note = (
        f"\n\n⚠️ 以下 Agent 已故障，**必须排除**，不得分配任何任务：\n"
        + "\n".join(f"- {aid}" for aid in excluded_agents)
    ) if excluded_agents else ""

    # 构建重规划 Prompt
    replanning_prompt = f"""
你是一个任务重新规划专家。当前有任务执行失败，需要你分析原因并生成新的执行策略。{excluded_note}

## 原始用户请求
{user_request}

## 已成功完成的任务
{json.dumps([{{'id': t['task_id'], 'title': t['task_title'], 'result': t['result'][:200]}} for t in completed_tasks], ensure_ascii=False, indent=2)}

## 失败的任务
{json.dumps([{{'id': t['task_id'], 'title': t['task_title'], 'agent': t['assigned_agent_id'], 'error': t['result'], 'retry_count': t['retry_count']}} for t in failed_tasks_list], ensure_ascii=False, indent=2)}

## 可用的 Agent
{chr(10).join([f"- {a['id']} ({a['capability']}): {a['description']}" for a in available_agents])}

## 你的任务

分析失败原因，生成新的执行策略：

1. **是否需要更换 Agent？** 当前 Agent 可能不适合这个任务
2. **是否需要添加前置任务？** 可能缺少必要的准备步骤（如数据转换、身份验证）
3. **是否需要调整任务描述？** 可能指令不够清晰
4. **是否需要完全重新规划？** 原计划可能存在根本性问题

## 输出格式

直接输出 JSON 格式：

```json
{{
  "analysis": "失败原因分析",
  "strategy": "新的执行策略",
  "tasks": [
    {{
      "task_id": "task_xxx",
      "task_title": "任务标题",
      "task_description": "详细描述",
      "assigned_agent_id": "agent_id",
      "capability_required": "capability"
    }}
  ]
}}
```

注意：
- 新任务 ID 应该唯一
- 如果只需要修复失败任务，只返回失败任务的修正版本
- 如果需要完全重新规划，返回全新的任务列表
"""
    
    try:
        # 使用推理模型进行重规划
        llm = get_llm_by_type("reasoning")
        
        logger.info("🤖 调用 LLM 进行重新规划...")
        full_response = ""
        try:
            async for chunk in llm.astream([HumanMessage(content=replanning_prompt)]):
                if hasattr(chunk, 'content'):
                    full_response += chunk.content
        except:
            full_response = (await llm.ainvoke([HumanMessage(content=replanning_prompt)])).content
        
        # 清理 JSON
        plan_json_str = full_response.strip()
        if plan_json_str.startswith("```json"):
            plan_json_str = plan_json_str.removeprefix("```json")
        if plan_json_str.startswith("```"):
            plan_json_str = plan_json_str.removeprefix("```")
        if plan_json_str.endswith("```"):
            plan_json_str = plan_json_str.removesuffix("```")
        plan_json_str = plan_json_str.strip()
        
        replan_data = json.loads(plan_json_str)
        
        logger.info(f"📋 重规划分析：{replan_data.get('analysis', 'N/A')}")
        logger.info(f"📋 新策略：{replan_data.get('strategy', 'N/A')}")
        
        # 将新任务添加到执行计划
        from src.service.agent_registry import get_registry_client
        registry_client = get_registry_client()
        
        new_tasks = []
        for task_data in replan_data.get("tasks", []):
            agent_id = task_data.get("assigned_agent_id")
            agent_info = registry_client.get_agent_by_id(agent_id)
            
            if not agent_info:
                logger.warning(f"Agent {agent_id} 不存在，跳过")
                continue
            
            new_task: TaskAssignment = {
                "task_id": task_data.get("task_id", f"task_replan_{uuid.uuid4().hex[:8]}"),
                "task_title": task_data.get("task_title", "未命名任务"),
                "task_description": task_data.get("task_description", ""),
                "assigned_agent_id": agent_id,
                "target_ip": agent_info["ip"],
                "target_port": agent_info["port"],
                "status": "pending",
                "result": "",
                "retry_count": 0
            }
            new_tasks.append(new_task)
        
        # 移除失败任务，添加新任务
        updated_plan = [t for t in execution_plan if t["status"] != "failed"]
        updated_plan.extend(new_tasks)
        
        logger.info(f"✅ 重规划完成，新增 {len(new_tasks)} 个任务")
        
        return Command(
            update={
                "execution_plan": updated_plan,
                "current_task_index": len(updated_plan) - len(new_tasks),  # 从新任务开始
                "replanning_count": replanning_count + 1,
                "last_replan_reason": replan_data.get("analysis", "未知原因"),
                "messages": [HumanMessage(
                    content=f"🔄 重新规划完成\n\n**分析**：{replan_data.get('analysis')}\n\n**新策略**：{replan_data.get('strategy')}\n\n**新任务数**：{len(new_tasks)}",
                    name="monitor"
                )]
            },
            goto="executor"
        )
        
    except Exception as e:
        logger.error(f"❌ LLM 重规划失败：{e}", exc_info=True)
        # 重规划失败，继续原计划
        return Command(
            update={
                "replanning_count": replanning_count + 1,
                "messages": [HumanMessage(
                    content=f"警告：重规划失败（{str(e)}），继续执行原计划",
                    name="monitor"
                )]
            },
            goto="executor"
        )


# ============================================================
# Reporter 节点 - 生成最终报告
# ============================================================

async def distributed_reporter_node(state: DistributedState) -> Command[Literal["__end__"]]:
    """
    报告生成节点
    
    功能：汇总所有任务结果，生成最终报告
    
    复用：LangManus 的 reporter_node 结构
    """
    logger.info("=== Reporter 生成最终报告 ===")
    
    execution_plan = state.get("execution_plan", [])
    failed_tasks = state.get("failed_tasks", [])
    
    # 统计信息
    total_tasks = len(execution_plan)
    completed_tasks = len([t for t in execution_plan if t["status"] == "completed"])
    failed_count = len(failed_tasks)
    
    # 生成报告
    report = f"""
# 🎯 分布式任务执行报告

## 📊 执行统计

- **总任务数**: {total_tasks}
- **成功**: {completed_tasks} ✅
- **失败**: {failed_count} ❌
- **成功率**: {(completed_tasks/total_tasks*100) if total_tasks > 0 else 0:.1f}%

## 📝 详细结果

"""
    
    for i, task in enumerate(execution_plan, 1):
        status_icon = "✅" if task["status"] == "completed" else "❌"
        report += f"""
### {i}. {task['task_title']} {status_icon}

- **Agent**: {task['assigned_agent_id']} ({task['target_ip']}:{task['target_port']})
- **状态**: {task['status']}

**结果**:
```
{task['result']}
```

---
"""
    
    # 失败任务提示
    if failed_tasks:
        report += f"\n⚠️  **注意**: 有 {failed_count} 个任务执行失败，请检查网络连接和远程 Agent 状态。\n"
    
    logger.info("报告生成完成")
    
    return Command(
        update={
            "messages": [HumanMessage(content=report, name="reporter")]
        },
        goto="__end__"
    )
