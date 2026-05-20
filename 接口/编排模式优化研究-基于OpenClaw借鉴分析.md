# 编排模式优化研究：基于 OpenClaw 的借鉴分析

---

## 一、系统概述

### 1.1 系统定位

一个分布式多智能体编排系统，基于 Microsoft Research 提出的 Magentic-One 模式，实现了自适应复杂度评估 + 多模式编排引擎。核心能力是接收用户的自然语言任务请求，自动分解为子任务，分配给合适的 Agent 执行，并处理执行过程中的失败恢复。

### 1.2 三层架构

```
┌────────────────────────────────────────────────┐
│  L1 应用层														          │
├────────────────────────────────────────────────┤
│  L2 编排层（核心）                               │
│  adaptive_orchestrator → graph nodes → builder │
│  agent_registry / agent_scheduler / QoS        │
├────────────────────────────────────────────────┤
│  L3 运行层                              
└────────────────────────────────────────────────┘
```

### 1.3 现有编排模式：自适应三档

LangManus 的编排入口 `adaptive_orchestrator` 会对每次用户请求自动评估复杂度，路由到三种编排模式之一：

| 模式 | 复杂度 | 图结构 | LLM 消耗 | 适用场景 |
|------|--------|--------|----------|---------|
| **Sequential** | SIMPLE | Planner → Executor → Monitor → Reporter（线性串行） | 2 次调用 | 单步查询、简单检索 |
| **Concurrent** | MEDIUM | 同上，但 Executor 内部按 `parallel_group` 并行执行 | 中等 | 多步独立任务 |
| **Magentic-One** | COMPLEX | Orchestrator ⇄ Executor 反馈循环，Progress Ledger 驱动 | 高 | 深度分析、多轮推理 |

**Magentic-One 模式核心 — Progress Ledger 5 维评估**：

每轮循环中，Orchestrator 使用推理模型对当前状态做 5 个维度的结构化评估：

| 维度 | 含义 |
|------|------|
| `is_request_satisfied` | 原始任务是否已完成 |
| `is_in_loop` | 是否陷入重复循环 |
| `is_progress_being_made` | 是否有实质进展 |
| `next_speaker` | 下一步由哪个 Agent 执行 |
| `instruction_or_question` | 给该 Agent 的具体指令 |

**失败恢复机制（4 条规则 + LLM 重规划）**：

```
Rule 0: 跨主体故障切换 — 远端节点失败，切换到同 capability 的替代节点
Rule 1: 同节点重试 — 最多 3 次
Rule 2: 切换备用 Agent — 查找同能力的其他 Agent
Rule 3: 插入前置任务 — 在失败任务前插入数据清洗步骤
兜底:   LLM 重规划 — 推理模型分析失败原因，生成替代方案（最多 2 次）
```

---

## 二、OpenClaw/Clawith Aware 编排模式概述

### 2.1 系统定位

OpenClaw 是当前多智能体领域的热门开源生态。Clawith（"OpenClaw for Teams"）是其企业级多智能体协作平台，核心创新是 **Aware（自主意识）系统**：每个 Agent 是一个持久存在的"数字员工"，具备自主感知、长期记忆和社交协作能力。



### 2.2 Aware 编排的三个核心机制

#### Trigger（触发器）— 定义"何时唤醒"

后台守护进程每 15 秒扫描一次所有启用的触发器，条件满足时将 Agent 加入唤醒队列。支持 6 种触发类型：

| 触发类型 | 机制 | 典型场景 |
|---------|------|---------|
| `cron` | Unix Cron 表达式 | 每天 9:00 生成日报 |
| `once` | 一次性延时触发 | Agent 为自己设闹钟 |
| `interval` | 固定间隔循环 | 每 30 分钟巡检 |
| `poll` | HTTP 探测 + JSONPath 提取 | 监控竞品价格 API |
| `on_message` | 等待特定 Agent/人的消息 | A2A 任务接力 |
| `webhook` | 接收外部 HTTP POST | GitHub PR 事件触发 |

关键约束：**每个触发器必须关联一个 Focus 项**，不允许无目的唤醒。

#### Focus（焦点）— 定义"关注什么"

Agent 被唤醒后，系统自动将 `focus.md` 注入对话上下文最前端。Focus 是一个结构化的任务清单：

```markdown
- [x] 收集 Q1 竞品报价数据
- [/] 生成竞品分析报告（进行中）
- [ ] 提交给策略总监审阅
```

Agent 在执行过程中自主维护 Focus（通过 `write_file` 工具更新），完成后主动取消关联的触发器。

#### Heartbeat（心跳）— 定义"主动探索"

独立于触发器，每 240 分钟自动执行 4 阶段自主探索：

```
Phase 1: 上下文回顾 — 读 soul.md + memory/reflections.md
Phase 2: 定向探索   — web_search 研究感兴趣的问题（最多 5 次）
Phase 3: 社交互动   — 浏览 Plaza，分享发现
Phase 4: 总结       — 记录发现 或 返回 HEARTBEAT_OK
```

### 2.3 Aware 模式的状态管理

| 状态载体 | 内容 | 持久性 |
|---------|------|--------|
| `soul.md` | Agent 的角色定义、性格、职责 | 永久，人工配置 |
| `memory.md` | 长期记忆（跨任务累积） | 永久，Agent 自主维护 |
| `focus.md` | 当前关注的任务清单 | 动态，Agent 自主更新 |
| `HEARTBEAT.md` | 心跳行为协议 | 可定制模板 |
| AgentTrigger 表 | 触发器配置 | DB 持久化 |

---

## 三、核心编排理念对比

### 3.1 设计哲学对比

| 维度 | LangManus | Clawith Aware |
|------|-----------|---------------|
| **控制方向** | 自上而下：中央 Orchestrator 统一规划分配 | 自下而上：每个 Agent 自主感知、决策、行动 |
| **Agent 角色** | 被动执行者：接到任务执行，执行完上报 | 主动工作者：自己决定做什么、何时做 |
| **任务生命周期** | 请求级：一次编排请求产生、结束后状态丢弃 | 持久级：Agent 跨任务存在，经验持续累积 |
| **对话记忆** | 仅当次对话有效，下次从零开始 | `memory.md` 永久保留 |
| **决策频率** | 高频集中：Orchestrator 每轮决策 | 低频分散：每个 Agent 独立按触发条件决策 |


---

## 四、可借鉴方向分析

### 4.1 结构化工作记忆（Focus List）

**Aware 的做法**：Agent 被唤醒后首先读取 `focus.md`，审阅"我正在关注什么"，确保所有行为围绕明确的子目标展开。

**LangManus 的痛点**：Magentic-One 的 Orchestrator 在长对话（10+ 轮）中，Progress Ledger 完全依赖对话历史生成。随着中间结果堆积，LLM 上下文窗口被淹没，容易丢失原始目标。

**借鉴方案**：

在 `DistributedState` 中增加 `focus_list` 字段，在 Magentic-One 的 Inner Loop 中维护结构化子目标清单：

```
改进前：                          改进后：
Orchestrator                    Orchestrator
  → 读对话历史                      → 读 focus_list（子目标清单）
  → LLM 生成 5 维 Ledger            → 读对话历史
  → 选 next_speaker                → LLM 生成 6 维 Ledger
                                      （新增 focus_update 维度）
                                   → 更新 focus_list
                                   → 选 next_speaker
```



## 六、总结

LangManus 与 OpenClaw/Clawith 代表了多智能体编排的两种互补范式：

- **LangManus**：中央计划 + 结构化状态 + 多级容错，擅长**一次性复杂任务的分解执行**
- **Clawith Aware**：自主感知 + 持久记忆 + 事件驱动，擅长**长期异步协作**

两者并非替代关系。通过有选择地借鉴 Aware 系统的编排理念，LangManus 可以在保持自身中央编排优势的同时，补足在工作记忆、主动监控、异步执行、事件驱动等方面的能力缺口。上述 5 个借鉴方向均可在现有架构上增量实现，无需推翻重建。

