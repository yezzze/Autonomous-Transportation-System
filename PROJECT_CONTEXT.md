# 项目上下文快照

> 最后更新：2026-05-07

---

## 项目概述

**LangManus**：基于 LangGraph 的分布式 Agent 编排系统，实现 Microsoft Magentic-One 模式 + 自适应模式选择。

- 技术栈：Python 3.11 + LangGraph + FastAPI + httpx + asyncio
- 项目根：`/Users/zhangtianfu/Desktop/project/langmanus`
- 主服务：`server.py`（FastAPI，端口 8000）
- Agent 节点：`agent_server.py`（可多实例，端口可配置，默认 9000）

---

## 当前进度

| 模块 | 状态 | 说明 |
|------|------|------|
| §1 应用层（install/start/stop/display） | ✅ 已完成 | 含 Skills.md 在线编辑 |
| §2.1 单主体工作流编排 | ✅ 已完成 | ASD 用 subprocess，非 Docker |
| §2.2 跨主体工作流编排 | ✅ 已完成 | httpx POST /orchestration/dispatch |
| §2.3 跨主体重编排（Agent 失效 failover） | ✅ 已完成 | Rule 0 切换替代节点，Rule 1 跳过失败任务 |
| §2.4 跨主体工作流停止编排 | ✅ 已完成 | DELETE /orchestration/session/{id} 传播 |
| ARDC HTTP Gossip 智能体发现 | ✅ 已完成 | POST /registry/sync，PEER_AOE_URLS 环境变量，含子工作流同步 |
| ASD subprocess 进程管理 | ✅ 已完成 | agent_scheduler.py，自动端口分配 |
| QoS 监控框架 | ✅ 已完成 | record_call / check_threshold / get_alert_agents |
| QoS → ASD 反馈闭环 | ✅ 已完成 | register_alert_callback 注册回调，cooldown 60s 防 storm，redeploy 后 reset 指标 |
| 跨主体编排主动触发 | ✅ 已完成 | identify_cross_host_tasks() 在 Planner 出口调用，写入 cross_host_sessions |
| 子工作流即服务（Sub-Workflow as a Service） | ✅ 已完成 | config/sub_workflows.json 定义 + ARDC Gossip 发现 + Executor 三路径路由 |
| 周期性工作流调度 | ✅ 已完成 | WorkflowScheduler asyncio 循环 + 并行执行 + 历史持久化 + 自动恢复 |
| 项目交接文档 | ✅ 已完成 | `接口/项目交接文档.md` 11 章节全面交接 |

> 详细实现状态见 [`接口/当前进度.md`](接口/当前进度.md)

---

## 当前正在解决的问题

**应用详情页 Agent 前端视图本地可访问性修复中**（2026-05-08）：`/ui/apps/{app_id}` 的 demo 视图原先回填 `http://192.168.49.2:30092`，在本机无法直连时会导致 iframe 打不开；现已改为在已知 demo 场景下优先改写到 `http://127.0.0.1:30092`，配合本地 `kubectl port-forward` 使用。

**应用详情页智能体前端视图已实现**（2026-05-07）：`/ui/apps/{app_id}` 现在会按运行中的 Agent 生成纵向堆叠 iframe 视窗，并通过新增的 `/api/apps/{app_id}/agent-views` 接口回填 `ip:port` 前端地址。

**Kubernetes 共享 GPU 联调验证完成**（2026-05-07）：已为 `nvidia-device-plugin-daemonset` 注入 `--config-file=/etc/nvidia-device-plugin/config.yaml` 并挂载 `nvidia-device-plugin-config`。节点 `nvidia.com/gpu` Capacity 已从 1 提升为 4，`perception2intermediatefeature-agent` 与 `cooperativefeaturefusiondetectionviz-agent` 已同时 Running，节点分配显示 `nvidia.com/gpu Requests/Limits = 2/2`。

**Kubernetes 共享 GPU 配置落地**（2026-05-07）：为 `k8s/cooperativefeaturefusiondetectionviz-agent.yaml` 与 `k8s/perception2intermediatefeature-agent.yaml` 增加共享 GPU 友好资源配置（显式 `nvidia.com/gpu` requests/limits + CPU/内存 requests/limits），避免 BestEffort 并匹配 time-slicing 场景。

**周期性工作流调度**（2026-05-07 完成）

支持将应用配置为周期性执行，按固定时间间隔自动触发工作流。每次触发启动独立实例（允许并行），执行历史持久化。

**实现要点**：
- `src/service/workflow_scheduler.py` — WorkflowScheduler 单例，asyncio 调度循环
- `GuidanceFile.constraints` 扩展：`schedule_interval_seconds` / `schedule_max_parallel` / `schedule_auto_restart`
- `AppStatus` 新增 `"scheduled"` 状态
- `ScheduleExecutionRecord` 数据类跟踪每次执行
- 4 个 API 端点：`schedule/start` / `schedule/stop` / `schedule/status` / `schedule/history`
- FastAPI startup 自动恢复 `schedule_auto_restart=true` 的应用
- Web UI 安装表单新增调度字段 + 调度控制按钮 + 历史面板

**子工作流即服务（Sub-Workflow as a Service）**（2026-05-07 完成）

支持节点定义子工作流（多 Agent 组成的命名 Pipeline），其他节点通过 ARDC Gossip 自动发现，直接调用子工作流整体而非单个 Agent。每次调用启动独立工作流实例。

**跨主体编排主动触发修复**（2026-05-07 完成）

修复 `identify_cross_host_tasks()` 从未被调用的问题 — 在 `distributed_planner_node()` 的 Pipeline 路径和 LLM 路径两个出口均注入调用，将识别结果写入 `cross_host_sessions`，使跨节点编排从被动（仅 Rule 0 故障切换）变为主动。

---

**编排可视化 H5 — 联动版**（2026-05-07 完成）

将可视化合并进主服务(端口 8000)。**真实分布式工作流自动出现在可视化页面,支持多工作流切换**。

**联动链路**:
```
POST /api/apps/install + /start  (或 任何调 run_distributed_workflow 的入口)
  → src/distributed_workflow.run_distributed_workflow()
    ├─ bus.register(title=user_input)              ← 注册新工作流
    ├─ bus.update_state(state, node="__init__")    ← 推首屏
    └─ async for chunk in graph.astream():
         → bus.update_state(state, node=node_name) ← 每节点推一帧
         → 触发 WS 推送给前端 (snapshot 含 3 场景全量数据)
```

**新增/改动文件**:
- `src/service/viz_bus.py` — VizBus 单例(多工作流注册/订阅/state 快照,asyncio-safe)
- `src/api/visualization_routes.py` — APIRouter(挂载到 src/api/app.py)
- `src/api/visualization.py` — 三场景数据提取层(纯只读,不侵入业务)
- `src/distributed_workflow.py` — `graph.ainvoke()` → `graph.astream()` + 注入 viz_bus
- `src/api/app.py` — `app.include_router(viz_router)`
- `static/visualization.html` — 顶部下拉选择器 + 双 WebSocket(全局列表 + 单工作流) + URL `?wf=<id>` 路由

**两种使用方式**:
- 真实工作流: `POST /api/apps/install` + `POST /api/apps/{id}/start` → 自动出现在 /viz
- Demo 演示: /viz 页面点 "▶ Demo 工作流" → 不依赖 LLM 的 7 任务流水线演示

**启动**: `python server.py` → 浏览器 http://127.0.0.1:8000/viz

**3 场景内容**:
- 场景 1 编排过程: Skills.md 原文 + 候选 Agent 按平台分组(本机/远端) + 已选 Agent 绿色高亮 + Pipeline 流图
- 场景 2 拓扑结果: Cytoscape + dagre 自动布局,平台 compound 父节点(🏠 本机 / ☁️ 远端),节点状态色 + 当前节点脉冲发光
- 场景 3 工作流执行: 总进度条 + 当前 Agent 卡(扫光动画) + MCP/A2A 工具调用气泡 + 时间线

> 旧的独立可视化服务 `visualization_server.py`(端口 8888)仍可用,作为离线演示模式保留。

---

**前期问题: 编排模式优化研究 — 基于 OpenClaw/Clawith Aware 系统的借鉴分析**（2026-03-30）

已完成对 OpenClaw/Clawith Aware 编排模式与 LangManus 现有三档编排的深度对比研究，产出技术方案报告，识别 5 个可借鉴方向：
1. P0: 结构化工作记忆（Focus List）— 解决 Magentic-One 长对话目标遗忘
2. P1: Monitor 主动超时检测 — 解决远端 hang 无法及时发现
3. P1: 跨主体异步 Dispatch — 解锁跨主体并行执行
4. P2: 事件触发层 — 支持 webhook/poll/cron 驱动编排
5. P2: 自适应控制参数 — 动态调整 max_round/max_stall

报告输出至 `接口/编排模式优化研究-基于OpenClaw借鉴分析.md`

**已确认 Kubernetes Manifest 加载策略**（2026-05-06）：`_deploy_kubernetes()` 现在优先读取仓库根目录 `k8s/*.yaml`，读取失败或文件缺失时回退到字典构造；YAML manifest 会被补齐运行时必需的 env 与资源字段。
**前端应用列表自动刷新去重**（2026-05-07）：`src/api/static/js/ui.js` 的 `loadApps()` 现在会对 `apps` 结果做签名比对，相同结果直接跳过重绘，避免无意义刷新。

## 当前进度更新（UI 增强）

- **新增：应用列表刷新去重** — 应用列表自动刷新时会比较本次 `apps` 数据签名，若与上次一致则不重绘表格，仅更新提示信息。
- **新增：应用详情页智能体前端视图** — 详情页的“智能体前端视图”标签页会按 Agent 纵向堆叠 iframe，并提供“打开页面”快捷入口。

## 当前进度更新（Kubernetes 资源配置）

- **新增：共享 GPU 友好 Deployment 模板** — 两个示例 Deployment 已补齐 CPU/内存 requests/limits，并显式声明 `nvidia.com/gpu` request/limit，便于与 NVIDIA device plugin time-slicing 配合使用。

**统一接口与代码映射文档已完成**（2026-03-10）

已新增 `接口/系统接口与代码映射文档.md`，将系统架构图、代码架构图、核心类关系图、三层接口流程序列图、每步对应的文件路径/类/函数映射表、HTTP API 一览、端到端数据流、DistributedState 全字段说明、设计模式与扩展点、骨架区域说明整合为一站式参考文档。现有 `类图.md`、`类图设计.md`、三个接口流程 v1.md 保留作为历史版本。

---

## 关键约束与决策

- `qwq-plus` 只支持 streaming=True，在 `src/agents/llm.py` 中特殊处理；节点中使用 `llm.astream()` 异步流式调用
- 所有 LangGraph 节点函数（planner/executor/monitor/reporter）均为 `async def`，所有 LLM 调用均使用异步方法（`astream`/`ainvoke`），确保多工作流并发执行
- LLM 模拟器（`llm_agent_simulator.py`）：executor 降级路径必须用 `await simulate_agent_call()` 而非同步版本，否则会冻结事件循环
- 跨主体 Rule 0 优先级高于 Rule 1：`apply_failure_rules` 先检查远端 failover，再检查本地重试
- `current_task_index` 必须 reset 到失败任务位置才能重新执行
- `find_alternative_remote_aoe` 跳过本地 IP（127/0.0.0/localhost）和已失败的远端 URL；仅匹配相同 capability
- `cross_host_sessions` 字段记录 task_id → remote_url 的映射，executor 路由用
- `failed_remote_aoe_urls: Dict[str, List[str]]` 按 task 维度记录已失败节点
- 子工作流（SubWorkflow）= 可被远端发现和调用的命名 Pipeline，复用 PipelineTopology 格式
- Executor 路由优先级：远端跨主体分发 > 本地子工作流 > 本地单 Agent
- 子工作流内部 Agent 生命周期由提供方节点自行管理，调用方只关心整体结果
- 不引入 Kafka/MQ，跨主体通信用 httpx 直连
- COMM 消息路由中间件（`src/service/message_router.py`）暂未实现
- 接口文档以“代码接口契约”为主体，HTTP API 作为子集纳入；必须覆盖 DistributedState、核心节点、共享配置契约

---

## 关键文件路径

| 文件 | 作用 |
|------|------|
| `src/graph/adaptive_orchestrator.py` | 所有工作流入口，自动选择编排模式 |
| `src/graph/distributed_types.py` | `DistributedState` 定义，所有状态字段在此 |
| `src/graph/distributed_nodes.py` | 核心节点：executor / monitor / apply_failure_rules |
| `src/graph/distributed_builder.py` | 构建 Planner→Executor→Monitor→Reporter 图 |
| `src/graph/magentic_nodes.py` | Magentic-One 节点（Progress Ledger 驱动） |
| `src/graph/progress_ledger.py` | 5 维 JSON 评估：满足/循环/进展/下一说话人/指令 |
| `接口/接口文档.md` | 统一接口文档：类、函数、状态对象、HTTP 接口契约 |
| `config/sub_workflows.json` | 子工作流定义（SubWorkflow as a Service） |
| `src/service/agent_registry.py` | AgentRegistryClient，ARDC 查询/注册（精确匹配，已移除 HNSW）+ 子工作流加载/Gossip 同步 |
| `src/service/agent_scheduler.py` | ASD subprocess 进程管理 |
| `agent_server.py` | FastAPI 节点服务，含 /orchestration/dispatch 等端点 |
| `src/app/app_logic_engine.py` | stop_app() / _cancel_remote_session() |
| `接口/系统接口与代码映射文档.md` | 统一接口文档：架构图+流程序列图+代码映射表+HTTP API+状态字段 |
| `接口/当前进度.md` | 实现状态档案（人工维护） |
| `接口/编排模式优化研究-基于OpenClaw借鉴分析.md` | 编排模式优化技术方案报告 |
| `src/api/visualization.py` | 可视化数据提取层(三场景 extract_*) |
| `visualization_server.py` | 独立可视化 FastAPI 服务(端口 8888) |
| `static/visualization.html` | H5 单页(Cytoscape + 原生 JS) |
| `scripts/run_visualization.sh` | 可视化启动脚本 |
| `src/service/workflow_scheduler.py` | 周期性工作流调度器(asyncio 循环 + 历史持久化) |
| `接口/项目交接文档.md` | 项目交接文档(11 章节，快速上手指南) |
