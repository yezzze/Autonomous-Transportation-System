# 项目上下文快照

> 最后更新：2026-05-06

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
| ARDC HTTP Gossip 智能体发现 | ✅ 已完成 | POST /registry/sync，PEER_AOE_URLS 环境变量 |
| ASD subprocess 进程管理 | ✅ 已完成 | agent_scheduler.py，自动端口分配 |
| QoS 监控框架 | ✅ 已完成 | record_call / check_threshold / get_alert_agents |
| QoS → ASD 反馈闭环 | ✅ 已完成 | register_alert_callback 注册回调，cooldown 60s 防 storm，redeploy 后 reset 指标 |

> 详细实现状态见 [`接口/当前进度.md`](接口/当前进度.md)

---

## 当前正在解决的问题

**应用详情页 Agent 前端视图本地可访问性修复中**（2026-05-08）：`/ui/apps/{app_id}` 的 demo 视图原先回填 `http://192.168.49.2:30092`，在本机无法直连时会导致 iframe 打不开；现已改为在已知 demo 场景下优先改写到 `http://127.0.0.1:30092`，配合本地 `kubectl port-forward` 使用。

**应用详情页智能体前端视图已实现**（2026-05-07）：`/ui/apps/{app_id}` 现在会按运行中的 Agent 生成纵向堆叠 iframe 视窗，并通过新增的 `/api/apps/{app_id}/agent-views` 接口回填 `ip:port` 前端地址。

**Kubernetes 共享 GPU 联调验证完成**（2026-05-07）：已为 `nvidia-device-plugin-daemonset` 注入 `--config-file=/etc/nvidia-device-plugin/config.yaml` 并挂载 `nvidia-device-plugin-config`。节点 `nvidia.com/gpu` Capacity 已从 1 提升为 4，`perception2intermediatefeature-agent` 与 `cooperativefeaturefusiondetectionviz-agent` 已同时 Running，节点分配显示 `nvidia.com/gpu Requests/Limits = 2/2`。

**Kubernetes 共享 GPU 配置落地**（2026-05-07）：为 `k8s/cooperativefeaturefusiondetectionviz-agent.yaml` 与 `k8s/perception2intermediatefeature-agent.yaml` 增加共享 GPU 友好资源配置（显式 `nvidia.com/gpu` requests/limits + CPU/内存 requests/limits），避免 BestEffort 并匹配 time-slicing 场景。

**统一接口与代码映射文档已完成**（2026-03-10）

**已确认 Kubernetes Manifest 加载策略**（2026-05-06）：`_deploy_kubernetes()` 现在优先读取仓库根目录 `k8s/*.yaml`，读取失败或文件缺失时回退到字典构造；YAML manifest 会被补齐运行时必需的 env 与资源字段。
**前端应用列表自动刷新去重**（2026-05-07）：`src/api/static/js/ui.js` 的 `loadApps()` 现在会对 `apps` 结果做签名比对，相同结果直接跳过重绘，避免无意义刷新。

## 当前进度更新（UI 增强）

- **新增：应用列表刷新去重** — 应用列表自动刷新时会比较本次 `apps` 数据签名，若与上次一致则不重绘表格，仅更新提示信息。
- **新增：应用详情页智能体前端视图** — 详情页的“智能体前端视图”标签页会按 Agent 纵向堆叠 iframe，并提供“打开页面”快捷入口。

## 当前进度更新（Kubernetes 资源配置）

- **新增：共享 GPU 友好 Deployment 模板** — 两个示例 Deployment 已补齐 CPU/内存 requests/limits，并显式声明 `nvidia.com/gpu` request/limit，便于与 NVIDIA device plugin time-slicing 配合使用。


已新增 `接口/系统接口与代码映射文档.md`，将系统架构图、代码架构图、核心类关系图、三层接口流程序列图、每步对应的文件路径/类/函数映射表、HTTP API 一览、端到端数据流、DistributedState 全字段说明、设计模式与扩展点、骨架区域说明整合为一站式参考文档。现有 `类图.md`、`类图设计.md`、三个接口流程 v1.md 保留作为历史版本。

---

## 关键约束与决策

- `qwq-plus` 只支持 streaming=True，在 `src/agents/llm.py` 中特殊处理
- 跨主体 Rule 0 优先级高于 Rule 1：`apply_failure_rules` 先检查远端 failover，再检查本地重试
- `current_task_index` 必须 reset 到失败任务位置才能重新执行
- `find_alternative_remote_aoe` 跳过本地 IP（127/0.0.0/localhost）和已失败的远端 URL；仅匹配相同 capability
- `cross_host_sessions` 字段记录 task_id → remote_url 的映射，executor 路由用
- `failed_remote_aoe_urls: Dict[str, List[str]]` 按 task 维度记录已失败节点
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
| `src/service/agent_registry.py` | AgentRegistryClient，ARDC 查询/注册 |
| `src/service/agent_scheduler.py` | ASD subprocess 进程管理 |
| `agent_server.py` | FastAPI 节点服务，含 /orchestration/dispatch 等端点 |
| `src/app/app_logic_engine.py` | stop_app() / _cancel_remote_session() |
| `接口/系统接口与代码映射文档.md` | 统一接口文档：架构图+流程序列图+代码映射表+HTTP API+状态字段 |
| `接口/当前进度.md` | 实现状态档案（人工维护） |
