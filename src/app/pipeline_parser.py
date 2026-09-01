"""
Pipeline 拓扑解析器

从 Skills.md 的 ## Pipeline 段落中解析固定执行链路，
支持串行（A -> B -> C）和并行（[A, B] -> C）语法。

语法规范：
    search_agent_001                         # 指定已注册 Agent
    search_agent_001:搜索最新竞品资讯     # agent_id:自定义描述
    search_agent_001{"limit": 3}:搜索资讯     # JSON 对象作为调用参数
    capability(search):搜索最新资讯          # 仅声明能力，延迟绑定 Agent
    capability(vision){"target_cluster": "edge-b"}:识别图像
    [search_agent_001, capability(compute):计算指标]
      -> nlp_agent_001:综合分析                 # 并行组 -> 下一步

返回结构：
    PipelineTopology = List[PipelineStep]
    PipelineStep = AgentStep | List[AgentStep]   (列表 = 并行组)
    AgentStep = {"capability": str, "description": str,
                 "agent_id"?: str, "parameters"?: dict}

示例：
    ## Pipeline
    search_agent_001:搜索与用户查询相关的最新资讯
    -> capability(nlp):对搜索结果做摘要分析
    -> nlp_agent_001:生成包含竞品对比表格的结构化报告
"""

import logging
import json
import re
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# 单个 Agent 步骤；capability(...) 节点不含 agent_id
AgentStep = Dict[str, Any]

# 单个执行步骤：单 Agent 或并行组
PipelineStep = Union[AgentStep, List[AgentStep]]

# 完整拓扑
PipelineTopology = List[PipelineStep]


def _split_top_level(text: str, delimiter: str) -> List[str]:
    """按顶层分隔符切分，忽略 JSON 对象/数组与并行组内的分隔符。"""
    parts: List[str] = []
    start = 0
    brace_depth = 0
    bracket_depth = 0
    in_json_string = False
    escaped = False
    index = 0

    while index < len(text):
        char = text[index]
        nested = brace_depth > 0 or bracket_depth > 0
        if in_json_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_json_string = False
            index += 1
            continue

        if char == '"' and nested:
            in_json_string = True
            index += 1
            continue
        if char == "{":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth:
            bracket_depth -= 1
        elif brace_depth == 0 and bracket_depth == 0 and text.startswith(delimiter, index):
            parts.append(text[start:index])
            index += len(delimiter)
            start = index
            continue
        index += 1

    parts.append(text[start:])
    return parts


def _parse_token_parts(token: str) -> tuple[str, str, Optional[Dict[str, Any]]]:
    """拆分 selector、描述和可选 JSON 参数。"""
    raw = token.strip()
    parameters: Optional[Dict[str, Any]] = None
    json_start = raw.find("{")
    description_start = raw.find(":")

    # 只有描述分隔冒号之前的花括号才表示 parameters；
    # 描述文本本身可以包含花括号。
    if json_start >= 0 and (description_start < 0 or json_start < description_start):
        selector = raw[:json_start].strip()
        decoder = json.JSONDecoder()
        try:
            decoded, consumed = decoder.raw_decode(raw[json_start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Agent token 中的 parameters 不是合法 JSON 对象: {token}") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"Agent token 中的 parameters 必须是 JSON 对象: {token}")
        remainder = raw[json_start + consumed:].strip()
        if remainder and not remainder.startswith(":"):
            raise ValueError(f"Agent token 的 JSON 参数后存在无效内容: {token}")
        description = remainder[1:].strip() if remainder else ""
        parameters = decoded
    else:
        selector, separator, description = raw.partition(":")
        selector = selector.strip()
        description = description.strip() if separator else ""

    return selector, description, parameters


def _parse_agent_step(token: str) -> AgentStep:
    """
    解析单个 Agent token：
      "search_agent_001"         → 查询注册表得到 capability
      "search_agent_001:描述"    → 固定 Agent 及自定义描述
      'search_agent_001{"limit": 3}:描述' → 附带 parameters
      "capability(search):描述"  → 仅声明 capability，不生成 agent_id
    """
    selector, description, parameters = _parse_token_parts(token)

    capability_match = re.fullmatch(r"capability\(([^()]*)\)", selector)
    if capability_match:
        capability = capability_match.group(1).strip()
        if not capability:
            raise ValueError("capability(...) 中的 capability 不能为空")
        step: AgentStep = {
            "capability": capability,
            "description": description,
        }
        if parameters is not None:
            step["parameters"] = parameters
        return step

    if selector.startswith("capability("):
        raise ValueError(f"无效的 capability token: {selector}")

    agent_id = selector
    if not agent_id:
        raise ValueError("agent_id 不能为空")

    # 延迟导入，避免 pipeline_parser 在模块加载时引入注册表依赖。
    from src.service.agent_registry import get_registry_client

    agent_info = get_registry_client().get_agent_by_id(agent_id)
    if not agent_info:
        raise ValueError(f"Agent {agent_id} 未在注册表中找到")

    step = {
        "capability": agent_info["capability"],
        "description": description,
        "agent_id": agent_id,
    }
    if parameters is not None:
        step["parameters"] = parameters
    return step


def _parse_parallel_group(token: str) -> List[AgentStep]:
    """
    解析并行组 token（已去除括号）：
      "search_agent_001:描述, capability(compute):描述" → [AgentStep, AgentStep]
    """
    parts = _split_top_level(token, ",")
    return [_parse_agent_step(p) for p in parts if p.strip()]


def _parse_pipeline_line(line: str) -> Optional[PipelineTopology]:
    """
    解析一行（或多行拼接的）Pipeline 声明。

    支持两种写法：
    1. 多行（每行一步，`->` 开头可选）：
         search_agent_001:描述
         -> capability(nlp):描述
    2. 单行：
         search_agent_001:描述 -> capability(nlp):描述
    """
    steps: PipelineTopology = []
    # 统一将 -> 作为分隔符，支持行首的额外 ->
    raw_steps = _split_top_level(line.strip().lstrip("->").strip(), "->")

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
