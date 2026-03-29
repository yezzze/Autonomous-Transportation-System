"""
基于 LLM 的智能体模拟器

替代固定的 Mock 数据，使用真实的 LLM API 模拟各种智能体的行为
"""
import logging
from typing import Dict, Any, Optional
from langchain_core.messages import SystemMessage, HumanMessage

from src.agents.llm import get_llm_by_type

logger = logging.getLogger(__name__)


class LLMAgentSimulator:
    """使用 LLM 模拟不同类型的智能体"""
    
    # 为每种智能体类型定义专门的系统提示词
    AGENT_PROMPTS = {
        "search": """你是一个专业的网络搜索智能体，擅长：
- 在互联网上查找最新、最权威的信息
- 支持多引擎搜索（Google、Bing、百度等）
- 过滤低质量内容，返回高价值结果
- 标注信息来源、时间、可信度

输出格式要求：
- 使用 Markdown 格式
- 包含标题、来源、时间、摘要
- 至少返回 3-5 条高质量结果
- 附带搜索统计信息

请根据用户的搜索需求，生成真实、详细的搜索结果。""",
        
        "nlp": """你是一个专业的自然语言处理智能体，擅长：
- 文本摘要和信息提取
- 情感分析和观点挖掘
- 文本分类和实体识别
- 多语言翻译和改写
- 内容生成和优化

输出格式要求：
- 使用 Markdown 格式
- 结构化输出（标题、要点、分析）
- 包含关键词、情感倾向、置信度
- 提供详细的分析理由

请根据用户的 NLP 任务需求，提供专业的文本处理结果。""",
        
        "compute": """你是一个专业的计算分析智能体，擅长：
- 数学计算和公式推导
- 数据统计和趋势分析
- 数值模拟和预测建模
- 图表生成建议（折线图、柱状图、散点图等）
- Python/NumPy/Pandas 代码示例

输出格式要求：
- 使用 Markdown 格式
- 包含计算过程和结果
- 提供数据表格和统计指标
- 建议可视化方案
- 附带 Python 代码示例（使用 ```python 代码块）

请根据用户的计算分析需求，提供详细的数据分析结果。""",
        
        "vision": """你是一个专业的计算机视觉智能体，擅长：
- 图像内容识别和分类
- 物体检测和定位
- OCR 文字识别
- 图像质量评估
- 场景理解和描述

输出格式要求：
- 使用 Markdown 格式
- 详细描述图像内容
- 列出检测到的对象、文字、场景
- 提供置信度评分
- 给出分析建议

注意：由于这是模拟环境，请根据任务描述中的信息推断可能的图像内容，
并生成合理的分析结果（如"假设图像包含...，则分析结果为..."）。""",
        
        "code_execution": """你是一个专业的代码执行智能体，擅长：
- 多语言代码执行（Python、JavaScript、Go 等）
- 代码生成和优化
- Bug 调试和修复
- 性能分析和改进建议
- 单元测试生成

输出格式要求：
- 使用 Markdown 格式
- 包含完整的代码（使用 ```language 代码块）
- 提供执行结果或输出
- 解释代码逻辑和关键步骤
- 附带使用说明和注意事项

请根据用户的代码需求，生成可执行的代码和详细说明。""",
        
        "web_interaction": """你是一个专业的网页交互智能体，擅长：
- 网页自动化操作（点击、填表、截图）
- 动态内容抓取
- 表单提交和数据上传
- Cookie 管理和会话保持
- JavaScript 执行

输出格式要求：
- 使用 Markdown 格式
- 描述执行的操作步骤
- 提供页面截图说明（模拟）
- 返回抓取的数据或操作结果
- 附带操作日志和状态

注意：这是模拟环境，请根据任务描述生成合理的网页交互结果。""",

        "perception": """你是一辆自动驾驶车辆上的**环境感知智能体**，负责处理车载传感器原始数据并生成结构化的环境表征。

你的传感器套件包括：
- 毫米波雷达（探测距离 200m，RCS 精度 ±0.5dBsm）
- 前置单目摄像头（1080P 30fps，YOLO 目标检测）
- 激光雷达（64 线，点云密度 1.2M pts/s）

**输出格式（严格按照以下 Markdown 结构）**：

## 感知报告

**传感器状态**: 所有传感器正常 ✅  
**采集时间戳**: [当前时间]  
**感知范围**: 前方 XXXm 路段

### 目标检测列表

| 目标ID | 类别 | 距离(m) | 相对速度(km/h) | 方位角(°) | 置信度 |
|--------|------|---------|--------------|---------|------|
| ...    | ...  | ...     | ...          | ...     | ...  |

### 道路结构
- 车道数、当前车道、前方路况

### 环境条件
- 能见度、天气、光照

### 感知摘要
用 2-3 句话总结当前路段的环境状态和潜在风险点。

请根据任务描述中的场景（路段、时间、天气等）生成符合真实物理约束的感知数据，
目标数量 3-8 个，距离数值随机但合理，置信度 0.75-0.98 范围内。""",

        "cognition": """你是自主交通系统的**综合环境认知智能体**，负责融合来自多辆车的感知数据，生成统一的、可供决策层使用的环境认知报告。

你的职责：
- 接收并融合自车感知数据与协同车辆感知数据
- 消除多源数据中的重复目标（基于位置+速度匹配）
- 补全自车盲区的环境信息
- 输出结构化安全评估和驾驶建议

**输出格式（严格按照以下 Markdown 结构）**：

## 综合环境认知报告

**融合数据源**: 自车(VehicleA) + 协同车(VehicleB/C)  
**融合时间戳**: [当前时间]  
**覆盖范围**: 自车前后 XXXm × 横向 XXXm

### 融合目标态势

| 目标ID | 类别 | 全局坐标(x,y) | 速度(km/h) | 来源传感器 | 危险等级 |
|--------|------|-------------|-----------|---------|------|
| ...    | ...  | ...         | ...       | ...     | 🟢/🟡/🔴 |

### 盲区补全情况
说明协同车辆补充了哪些自车无法感知的目标

### 安全评估
- **碰撞风险评分**: X.X / 10
- **最近威胁目标**: 距离 Xm，TTCx秒
- **建议行为**: 保持车速 / 减速预警 / 紧急制动

### 驾驶建议
用 3-5 条具体可执行的建议，格式为"[优先级] 建议内容"

请根据前序感知数据（若有）进行真实的数据融合分析，输出符合 ISO 26262 安全完整性等级要求的认知报告。"""
    }
    
    def __init__(self, use_reasoning_model: bool = False):
        """
        初始化 LLM 智能体模拟器
        
        Args:
            use_reasoning_model: 是否使用推理模型（更强大但更慢）
        """
        self.llm_type = "reasoning" if use_reasoning_model else "basic"
        logger.info(f"初始化 LLM 智能体模拟器，使用模型类型: {self.llm_type}")
    
    def get_agent_capability(self, agent_id: str) -> str:
        """从 Agent ID 中提取能力类型"""
        # search_agent_001 -> search
        # nlp_agent_001 -> nlp
        for capability in self.AGENT_PROMPTS.keys():
            if capability in agent_id.lower():
                return capability
        return "general"
    
    async def simulate_agent_call(
        self,
        agent_id: str,
        task_title: str,
        task_description: str,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        使用 LLM 模拟智能体调用
        
        Args:
            agent_id: Agent ID（如 search_agent_001）
            task_title: 任务标题
            task_description: 任务描述
            context: 上下文信息（前序任务结果等）
        
        Returns:
            LLM 生成的智能体响应结果
        """
        capability = self.get_agent_capability(agent_id)
        system_prompt = self.AGENT_PROMPTS.get(capability, "你是一个通用智能体，请根据任务需求提供帮助。")
        
        # 构建用户消息
        user_message = f"""## 任务信息

**任务标题**: {task_title}

**任务描述**: 
{task_description}

**Agent ID**: {agent_id}
"""
        
        # 添加上下文信息
        if context and context.get("previous_results"):
            user_message += f"""
**前序任务结果**:
{context['previous_results']}
"""
        
        user_message += """

请根据你的能力和任务需求，生成详细、专业、真实的执行结果。"""
        
        from src.config import REASONING_MODEL, BASIC_MODEL
        model_name = REASONING_MODEL if self.llm_type == "reasoning" else BASIC_MODEL
        logger.info(f"🤖 调用 LLM 模拟 Agent: {agent_id} (能力: {capability}) — 模型: {model_name}")
        
        try:
            # 获取 LLM
            llm = get_llm_by_type(self.llm_type)
            
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_message)
            ]
            
            # 流式调用（适用于 qwq-plus 等模型）
            full_response = ""
            try:
                for chunk in llm.stream(messages):
                    if hasattr(chunk, 'content'):
                        full_response += chunk.content
            except Exception as stream_err:
                logger.warning(f"流式调用失败，尝试非流式调用: {stream_err}")
                response = llm.invoke(messages)
                full_response = response.content
            
            logger.info(f"✅ LLM 模拟完成，生成了 {len(full_response)} 字符的结果")
            return full_response
            
        except Exception as e:
            logger.error(f"❌ LLM 模拟失败: {e}")
            # 降级为简单的固定响应
            return f"""# {task_title}

## 执行结果

由于 LLM 调用失败（{str(e)}），这是一个降级的简化响应。

**任务**: {task_title}
**Agent**: {agent_id}
**状态**: ⚠️ 部分完成（LLM 不可用）

在生产环境中，此任务应由真实的 {capability} 智能体处理。
"""
    
    def simulate_agent_call_sync(
        self,
        agent_id: str,
        task_title: str,
        task_description: str,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        同步版本的智能体模拟（兼容非异步代码）
        
        Args:
            agent_id: Agent ID
            task_title: 任务标题
            task_description: 任务描述
            context: 上下文信息
        
        Returns:
            智能体响应结果
        """
        import asyncio
        
        try:
            # 尝试获取现有的事件循环
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果循环正在运行，创建新的任务
                import nest_asyncio
                nest_asyncio.apply()
        except RuntimeError:
            # 没有事件循环，创建新的
            pass
        
        return asyncio.run(self.simulate_agent_call(
            agent_id, task_title, task_description, context
        ))


# 全局实例（单例模式）
_simulator_instance: Optional[LLMAgentSimulator] = None


def get_llm_agent_simulator(use_reasoning_model: bool = False) -> LLMAgentSimulator:
    """
    获取 LLM 智能体模拟器的全局实例
    
    Args:
        use_reasoning_model: 是否使用推理模型
    
    Returns:
        LLMAgentSimulator 实例
    """
    global _simulator_instance
    if _simulator_instance is None:
        _simulator_instance = LLMAgentSimulator(use_reasoning_model)
    return _simulator_instance
