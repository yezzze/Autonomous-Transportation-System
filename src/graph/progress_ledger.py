"""
Progress Ledger - Magentic-One 核心组件

实现每轮的 5 维度进度分析：
1. is_request_satisfied: 任务完成了吗？
2. is_in_loop: 陷入循环了吗？
3. is_progress_being_made: 有进展吗？
4. next_speaker: 下一个该谁发言？
5. instruction_or_question: 给他什么指令？
"""

from typing import TypedDict, Literal, Union, Dict, Any, List
import json
from langchain_core.messages import HumanMessage, AIMessage
from src.agents.llm import get_llm_by_type


class ProgressLedgerItem(TypedDict):
    """单个进度项"""
    reason: str
    answer: Union[str, bool]


class ProgressLedger(TypedDict):
    """完整的进度账本"""
    is_request_satisfied: ProgressLedgerItem
    is_in_loop: ProgressLedgerItem
    is_progress_being_made: ProgressLedgerItem
    next_speaker: ProgressLedgerItem
    instruction_or_question: ProgressLedgerItem


PROGRESS_LEDGER_PROMPT = """
我们正在执行以下任务：

{task}

我们有以下团队成员：

{team}

请分析当前进度，回答以下问题（需要详细推理）：

1. **任务是否完成**？(is_request_satisfied)
   - 原始请求是否已经成功且完整地解决？
   - True 表示完成，False 表示未完成

2. **是否陷入循环**？(is_in_loop)
   - 是否在重复相同的请求或得到相同的响应？
   - 循环可能跨越多轮，包括重复滚动等动作
   - True 表示陷入循环，False 表示正常

3. **是否有进展**？(is_progress_being_made)
   - 最近的消息是否增加了价值？
   - 是否有证据表明被困在循环中？
   - 是否存在重大障碍（如无法读取必需文件）？
   - True 表示有进展，False 表示停滞

4. **下一个应该谁发言**？(next_speaker)
   - 从以下选项中选择：{names}
   - 基于当前状态，哪个成员最适合推进任务？

5. **给他什么指令或问题**？(instruction_or_question)
   - 直接对该成员说话
   - 包含他需要的任何具体信息

## 输出格式

必须输出纯 JSON 格式，不要有任何其他内容：

{{
    "is_request_satisfied": {{
        "reason": "推理过程",
        "answer": true/false
    }},
    "is_in_loop": {{
        "reason": "推理过程",
        "answer": true/false
    }},
    "is_progress_being_made": {{
        "reason": "推理过程",
        "answer": true/false
    }},
    "next_speaker": {{
        "reason": "推理过程",
        "answer": "agent_name"
    }},
    "instruction_or_question": {{
        "reason": "推理过程",
        "answer": "具体指令"
    }}
}}
"""


async def create_progress_ledger(
    state: Dict[str, Any],
    max_retries: int = 3
) -> ProgressLedger:
    """
    生成进度账本
    
    Args:
        state: 当前状态（包含 task, messages, agent_registry）
        max_retries: 最大重试次数
        
    Returns:
        ProgressLedger 对象
    """
    # 提取参数（注意：messages 是 LangChain Message 对象，需用 .content）
    messages = state.get("messages", [])
    task = messages[0].content if messages else "未知任务"
    chat_history = messages
    agent_registry = state.get("agent_registry_cache", [])
    
    # 构建团队描述
    team_desc = "\n".join([
        f"- {a['id']}: {a['description']}" 
        for a in agent_registry
    ]) if agent_registry else "- 无可用 Agent"
    
    agent_names = ", ".join([a['id'] for a in agent_registry]) if agent_registry else "无"
    
    # 构建 Prompt
    prompt = PROGRESS_LEDGER_PROMPT.format(
        task=task,
        team=team_desc,
        names=agent_names
    )
    
    # 添加对话历史上下文
    messages = chat_history + [HumanMessage(content=prompt)]
    
    # 使用推理模型生成 Progress Ledger
    llm = get_llm_by_type("reasoning")
    
    for attempt in range(max_retries):
        try:
            response = await llm.ainvoke(messages)
            
            # 提取 JSON
            text = response.content
            if text.startswith("```json"):
                text = text.removeprefix("```json").removeprefix("```")
            if text.endswith("```"):
                text = text.removesuffix("```")
            text = text.strip()
            
            # 解析 JSON
            ledger_dict = json.loads(text)
            
            # 验证格式
            _validate_progress_ledger(ledger_dict)
            
            return ledger_dict
            
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"⚠️  Progress Ledger 解析失败（尝试 {attempt + 1}/{max_retries}）: {e}")
                continue
            else:
                raise RuntimeError(f"Progress Ledger 生成失败: {e}")


def _validate_progress_ledger(ledger: dict) -> None:
    """验证 Progress Ledger 格式"""
    required_keys = [
        "is_request_satisfied",
        "is_in_loop", 
        "is_progress_being_made",
        "next_speaker",
        "instruction_or_question"
    ]
    
    for key in required_keys:
        if key not in ledger:
            raise ValueError(f"缺少必需字段: {key}")
        
        item = ledger[key]
        if not isinstance(item, dict):
            raise ValueError(f"{key} 必须是字典")
        
        if "reason" not in item or "answer" not in item:
            raise ValueError(f"{key} 必须包含 reason 和 answer 字段")


def extract_next_speaker(ledger: ProgressLedger) -> str:
    """提取下一个发言者"""
    return str(ledger["next_speaker"]["answer"])


def is_task_completed(ledger: ProgressLedger) -> bool:
    """判断任务是否完成"""
    return bool(ledger["is_request_satisfied"]["answer"])


def is_stalling(ledger: ProgressLedger) -> bool:
    """判断是否停滞（陷入循环或无进展）"""
    return (
        bool(ledger["is_in_loop"]["answer"]) or 
        not bool(ledger["is_progress_being_made"]["answer"])
    )
