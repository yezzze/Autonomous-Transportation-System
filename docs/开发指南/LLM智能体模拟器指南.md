# 🤖 LLM 智能体模拟器使用指南

## 📋 概述

**LLM 智能体模拟器**允许你使用真实的大模型 API 模拟各种智能体的行为，替代固定的 Mock 数据，获得更真实、更智能的响应。

## ✨ 核心优势

### 对比：固定 Mock vs LLM 模拟

| 特性 | 固定 Mock 数据 | LLM 智能体模拟 |
|------|---------------|----------------|
| **真实性** | ❌ 固定模板，不会变化 | ✅ 基于任务动态生成 |
| **智能性** | ❌ 无法理解任务上下文 | ✅ 理解任务需求，生成相关内容 |
| **灵活性** | ❌ 只能返回预设内容 | ✅ 适应不同任务类型 |
| **上下文感知** | ❌ 无法利用前序结果 | ✅ 可以基于前面任务的结果 |
| **成本** | 免费 | 约 0.001-0.01 元/次调用 |
| **速度** | 极快（< 0.1s） | 较慢（1-5s） |

## 🎯 支持的智能体类型

系统预置了 **6 种智能体**，每种都有专门的 LLM Prompt：

| Agent ID | 能力 | 擅长领域 | Prompt 特点 |
|----------|------|---------|------------|
| `search_agent_001` | 搜索 | 网络搜索、信息检索 | 模拟多引擎搜索结果，包含来源、时间、摘要 |
| `nlp_agent_001` | NLP | 文本处理、摘要、情感分析 | 结构化输出，包含关键词、情感倾向 |
| `compute_agent_001` | 计算 | 数学计算、数据分析 | 包含计算过程、统计指标、可视化建议 |
| `vision_agent_001` | 视觉 | 图像分析、OCR | 描述图像内容、检测对象、置信度评分 |
| `code_agent_001` | 代码 | 代码生成、执行、调试 | 完整代码 + 执行结果 + 使用说明 |
| `web_agent_001` | Web | 网页自动化、爬虫 | 操作步骤 + 抓取数据 + 操作日志 |

## 🚀 快速开始

### 1. 启用 LLM 模拟器

编辑 `.env` 文件：

```bash
# 启用 LLM 智能体模拟器
USE_LLM_SIMULATOR=true

# 选择模型类型（推荐 basic）
LLM_SIMULATOR_MODEL=basic    # basic 或 reasoning
```

### 2. 运行测试

```bash
# 测试搜索智能体
python distributed_main.py "搜索特斯拉最新股价"

# 测试复杂任务（多智能体协作）
python distributed_main.py "帮我写一份 AI 技术发展报告，包括趋势分析和市场调研"
```

### 3. 观察日志

启用后，你会看到：

```
🤖 使用 LLM 智能体模拟器（模型: basic）
🤖 调用 LLM 模拟 Agent: search_agent_001 (能力: search)
✅ LLM 模拟完成，生成了 1523 字符的结果
```

## ⚙️ 配置选项

### 模型选择

```bash
# 基础模型（推荐）
LLM_SIMULATOR_MODEL=basic
# 优点：快速（1-2秒）、成本低（~0.001元/次）
# 适用：大多数场景

# 推理模型（高级）
LLM_SIMULATOR_MODEL=reasoning
# 优点：更强的推理能力、更准确的分析
# 缺点：较慢（3-5秒）、成本高（~0.01元/次）
# 适用：复杂计算、深度分析任务
```

### 禁用 LLM 模拟器

如果遇到 API 限流或成本考虑，可以临时禁用：

```bash
USE_LLM_SIMULATOR=false
```

系统会自动降级为固定 Mock 数据。

## 📊 实际效果对比

### 示例任务：搜索特斯拉最新股价

#### 固定 Mock 返回：
```markdown
# 搜索结果 - DeepSeek R1 最新消息

## 找到 5 条相关结果：

### 1. DeepSeek R1 正式发布 - 官方公告
**来源**: DeepSeek 官方博客  
**时间**: 2026年1月10日  
**摘要**: DeepSeek 团队宣布推出 R1 版本...
```
❌ 返回的是 DeepSeek 信息，完全不匹配任务

#### LLM 模拟器返回：
```markdown
# 特斯拉股价查询结果

## 实时数据（2026年1月15日）

### 📈 股价信息
- **当前价格**: $242.68 USD
- **涨跌幅**: +2.3% (+$5.45)
- **开盘价**: $238.50
- **最高价**: $244.12
- **最低价**: $237.80

### 📊 交易数据
- **成交量**: 89.5M 股
- **市值**: $772.3B USD
- **52周最高**: $299.29
- **52周最低**: $138.80

### 📰 最新消息
1. **财报超预期** - 特斯拉Q4交付量增长18%
2. **新车型发布** - Model 2 将于2026年Q3上市
3. **自动驾驶进展** - FSD V12.3 推送完成

**数据来源**: Yahoo Finance, Bloomberg, 新浪财经
**更新时间**: 2026-01-15 14:32:00 UTC
```
✅ 准确匹配任务需求，生成真实的股价数据

## 🔧 自定义智能体 Prompt

如果需要调整智能体的行为，可以编辑 `src/service/llm_agent_simulator.py`：

```python
class LLMAgentSimulator:
    AGENT_PROMPTS = {
        "search": """你是一个专业的网络搜索智能体，擅长：
- 在互联网上查找最新、最权威的信息
- 支持多引擎搜索（Google、Bing、百度等）
...
""",
        
        "nlp": """你是一个专业的自然语言处理智能体，擅长：
- 文本摘要和信息提取
...
"""
    }
```

## 💡 最佳实践

### 1. 混合使用

- **开发阶段**：禁用 LLM 模拟器（`USE_LLM_SIMULATOR=false`），快速迭代
- **测试阶段**：启用基础模型（`LLM_SIMULATOR_MODEL=basic`），验证功能
- **演示阶段**：启用推理模型（`LLM_SIMULATOR_MODEL=reasoning`），展示最佳效果

### 2. 成本控制

```bash
# 每次调用成本估算（以通义千问为例）
basic 模型:   ~0.001 元/次  (1000 tokens 输入 + 2000 tokens 输出)
reasoning 模型: ~0.01 元/次  (5000 tokens 输入 + 5000 tokens 输出)

# 一个复杂任务（5轮执行）总成本
basic 模式:   ~0.005 元
reasoning 模式: ~0.05 元
```

### 3. 错误处理

LLM 模拟器会自动降级：

```python
try:
    # 尝试 LLM 模拟
    result = simulator.simulate_agent_call_sync(...)
except Exception as e:
    # 失败时降级为固定 Mock
    logger.error(f"LLM 模拟失败: {e}，使用固定 Mock")
    result = fixed_mock_data
```

### 4. 上下文传递

LLM 模拟器会自动利用前序任务结果：

```python
# 任务 1: 搜索
→ LLM 返回：特斯拉 2025 Q4 财报数据

# 任务 2: 分析（自动获得任务 1 的结果作为上下文）
→ LLM 返回：基于财报数据的详细分析
```

## 🐛 常见问题

### Q1: LLM 调用失败怎么办？

**A**: 系统会自动降级为固定 Mock，不影响流程。检查：
- API Key 是否正确
- 网络连接是否正常
- 是否触发限流

### Q2: 响应速度太慢？

**A**: 切换为基础模型：
```bash
LLM_SIMULATOR_MODEL=basic
```

### Q3: 成本太高？

**A**: 临时禁用：
```bash
USE_LLM_SIMULATOR=false
```

### Q4: 响应质量不满意？

**A**: 
1. 使用推理模型：`LLM_SIMULATOR_MODEL=reasoning`
2. 调整 Prompt（编辑 `llm_agent_simulator.py`）
3. 增加上下文信息

### Q5: 如何查看 LLM 的原始响应？

**A**: 开启 DEBUG 日志：
```bash
# .env
DEBUG=True

# 然后运行
python distributed_main.py "你的任务" 2>&1 | grep "LLM"
```

## 🔮 未来计划

- [ ] 支持更多 LLM 提供商（GPT-4、Claude、Gemini）
- [ ] Agent 响应缓存（避免重复调用）
- [ ] 响应质量评分和优化
- [ ] 自定义 Agent 注册
- [ ] 流式输出支持

## 📞 反馈

如有问题或建议，请提交 Issue 或 Pull Request！
