"""
Pipeline 拓扑解析器

从 Skills.md 的 ## Pipeline 段落中解析固定执行链路，
支持串行（A -> B -> C）和并行（[A, B] -> C）语法。

语法规范：
    search                          # 仅指定 capability（描述自动由用户输入补充）
    search:搜索最新竞品资讯          # capability:自定义描述
    [search, compute:计算指标] -> nlp:综合分析  # 并行组 -> 下一步

返回结构：
    PipelineTopology = List[PipelineStep]
    PipelineStep = AgentStep | List[AgentStep]   (列表 = 并行组)
    AgentStep = {"capability": str, "description": str, "agent_id": str}

示例：
    ## Pipeline
    search:搜索与用户查询相关的最新资讯
    -> nlp:对搜索结果做摘要分析
    -> nlp:生成包含竞品对比表格的结构化报告
"""

import logging
import re
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# 单个 Agent 步骤
AgentStep = Dict[str, str]  # {"capability": str, "description": str, "agent_id": str}

# 单个执行步骤：单 Agent 或并行组
PipelineStep = Union[AgentStep, List[AgentStep]]

# 完整拓扑
PipelineTopology = List[PipelineStep]

# capability → agent_id 默认映射（与 agent_registry.json 一致）
_CAPABILITY_TO_AGENT_ID = {
    "search": "search_agent_001",
    "nlp": "nlp_agent_001",
    "compute": "compute_agent_001",
    "vision": "vision_agent_001",
    "code": "code_agent_001",
    "code_execution": "code_agent_001",
    "web": "web_agent_001",
    "web_interaction": "web_agent_001",
    # 车辆协同感知 Demo
    "perception": "perception_self_001",
    "cognition": "cognition_main_001",
}


def _resolve_agent_id(capability: str) -> str:
    """将 capability 映射到 agent_id（未知 capability 生成通用 ID）"""
    return _CAPABILITY_TO_AGENT_ID.get(capability.lower(), f"{capability}_agent_001")


def _parse_agent_step(token: str) -> AgentStep:
    """
    解析单个 Agent token：
      "search"           → capability=search, description=""
      "search:描述文字"   → capability=search, description="描述文字"
    """
    token = token.strip()
    if ":" in token:
        capability, description = token.split(":", 1)
        capability = capability.strip()
        description = description.strip()
    else:
        capability = token
        description = ""
    return {
        "capability": capability.lower(),
        "description": description,
        "agent_id": _resolve_agent_id(capability),
    }


def _parse_parallel_group(token: str) -> List[AgentStep]:
    """
    解析并行组 token（已去除括号）：
      "search:描述, compute:描述" → [AgentStep, AgentStep]
    """
    parts = token.split(",")
    return [_parse_agent_step(p) for p in parts if p.strip()]


def _parse_pipeline_line(line: str) -> Optional[PipelineTopology]:
    """
    解析一行（或多行拼接的）Pipeline 声明。

    支持两种写法：
    1. 多行（每行一步，`->` 开头可选）：
         search:描述
         -> nlp:描述
    2. 单行：
         search:描述 -> nlp:描述
    """
    steps: PipelineTopology = []
    # 统一将 -> 作为分隔符，支持行首的额外 ->
    raw_steps = re.split(r"\s*->\s*", line.strip().lstrip("->").strip())

    for raw in raw_steps:
        raw = raw.strip()
        if not raw:
            continue
        # 并行组：[A, B] 或 [A:desc, B:desc]
        match = re.match(r"^\[(.+)\]$", raw)
        if match:
            group = _parse_parallel_group(match.group(1))
            if group:
                steps.append(group)
        else:
            step = _parse_agent_step(raw)
            if step["capability"]:
                steps.append(step)

    return steps if steps else None


def parse_pipeline(skills_content: str) -> Optional[PipelineTopology]:
    """
    从 Skills.md 内容中提取 ## Pipeline 段落并解析拓扑。

    规则：
    - 找到第一个 `## Pipeline` 标题
    - 收集其下方的内容（直到下一个 ## 标题或文档末尾）
    - 将多行 `->` 声明合并为完整拓扑链

    Returns:
        PipelineTopology（非空列表）；未找到 Pipeline 段落返回 None。
    """
    if not skills_content or not skills_content.strip():
        return None

    # 找 ## Pipeline 段落
    pattern = re.compile(
        r"^##\s+Pipeline\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(skills_content)
    if not match:
        return None

    # 截取段落内容（到下一个 ## 标题前）
    section_start = match.end()
    next_section = re.search(r"^##\s+", skills_content[section_start:], re.MULTILINE)
    section_end = section_start + next_section.start() if next_section else len(skills_content)
    section = skills_content[section_start:section_end].strip()

    if not section:
        return None

    # 将所有行拼接为一条 pipeline 字符串（合并多行 -> 声明）
    # 过滤空行和注释行（以 # 开头）
    lines = [
        ln.strip()
        for ln in section.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

    if not lines:
        return None

    # 合并：如果某行以 -> 开头，拼接到前一行；否则视为独立步骤追加
    merged = lines[0]
    for ln in lines[1:]:
        if ln.startswith("->"):
            merged += " " + ln
        else:
            # 多条独立 pipeline 声明取第一条，不合并（保持最简语义）
            break

    topology = _parse_pipeline_line(merged)
    if not topology:
        return None

    logger.info(
        f"[PipelineParser] 解析成功: {len(topology)} 个步骤, "
        f"steps={[s['capability'] if isinstance(s, dict) else [x['capability'] for x in s] for s in topology]}"
    )
    return topology


def topology_to_description(topology: PipelineTopology) -> str:
    """将拓扑转换为可读描述，用于日志/UI展示"""
    parts = []
    for step in topology:
        if isinstance(step, list):
            inner = ", ".join(f"{s['capability']}({s['description'][:20] or '…'})" for s in step)
            parts.append(f"[{inner}]")
        else:
            parts.append(f"{step['capability']}({step['description'][:20] or '…'})")
    return " -> ".join(parts)
