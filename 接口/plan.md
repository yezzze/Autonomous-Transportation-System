## Plan: LangManus 后续开发路线图

**TL;DR**：当前三层架构的**单主体流程已全部跑通**，Skills / Pipeline 模式已实现。后续分三大阶段推进：多节点跨主体能力 → 执行层真实化 → 生产化基础设施。

---

## 现状摘要

| 层             | 已实现                                               | Mock/骨架                                    | 未实现                                     |
| -------------- | ---------------------------------------------------- | -------------------------------------------- | ------------------------------------------ |
| **应用管理层** | 全流程（安装/启动/停止/查询）、Skills、Pipeline      | AW镜像拉取、ARDC注册                         | —                                          |
| **编排层**     | 单主体AOE+AWM+ARDC、自适应模式、Magentic-One、重规划 | ASD容器部署                                  | 跨主体编排（§2.2/2.3/2.4）、ARDC广播（§1） |
| **运行层**     | ALCM生命周期+引用计数、QoS采集、RRDC内存实现         | SBOX（无沙箱隔离）、INTF（全mock）、镜像拉取 | COMM中间件、QoS→调度反馈                   |

---

## Phase 1 — 跨主体编排（高优先级）

> 目标：让两台机器（终端↔边缘）能协同执行同一个工作流

**步骤**
1. **ARDC 跨节点广播**（对应接口文档编排层§1）
   - 在 `agent_registry.py` 基础上增加 `broadcast_to_peers()` HTTP 推送接口
   - 新增 app.py 端点 `POST /internal/ardc/sync`，接收对端广播的 Agent 列表
   - 合并本地+远端 Agent 列表，标注来源 `source: "local"|"remote:192.168.x.x"`

2. **跨主体任务分发**（编排层§2.2）
   - 补全 `dispatch_subtask_to_remote_aoe()` 的真实 HTTP 实现（替换 mock）
   - 丰富 `identify_cross_host_tasks()` 逻辑：根据 Agent 的 `source` 字段判断是否跨主体
   - 边缘侧启动独立子 LangGraph 工作流，返回 `remote_workflow_handle`

3. **跨主体工作流停止**（编排层§2.4）
   - `stop_app()` 额外调用 `DELETE /orchestration/workflow/{handle}` 通知对端
   - 对端会话超时自动清理（`session_timeout` 字段已在接口文档定义）

4. **重编排（Agent 失效）**（编排层§2.3）
   - 在 `distributed_monitor_node` 失败处理分支中：检测失效 Agent 原属哪个主体
   - 若远端 Agent 失效，重新分发到新边缘；本地失效走现有 LLM 重规划路径

**相关文件**: distributed_nodes.py、agent_registry.py、app.py

---

## Phase 2 — 执行层真实化（中优先级）

> 目标：从 LLM mock 进化到真实 Agent 进程

**步骤**
1. **SBOX 容器隔离**（运行层§1）
   - 在 agent_scheduler.py 的 `_do_deploy()` 中接入 Docker SDK：`docker run --rm -d img_agent`
   - lifecycle_manager.py 记录容器 ID，`shutdown_agent()` 调用 `docker stop`
   - 开发模式保留 LLM Simulator 兜底，生产模式通过 .env 切换

2. **真实 Agent HTTP 服务**
   - 为每种 `capability` 提供标准化的 FastAPI agent_server（已有 agent_server.py 骨架）
   - 规范 A2A 协议接口：`POST /a2a/execute` 收 `A2ATaskRequest`，返回 `A2ATaskResult`
   - 替换 `execute_task_on_agent()` 中的 LLM mock 为真实 HTTP 调用

3. **COMM 跨 Agent 通信中间件**（运行层§1/§2）
   - 在 a2a_protocol.py 基础上，封装 `AgentBus` 类
   - 支持 pub/sub 模式：Agent A 发布结果 → Agent B 订阅消费（异步协同）
   - 与 QoS Monitor 集成：每次通信自动 `record_call()` 上报延迟

4. **INTF 真实硬件对接**（可选，领域相关）
   - `SensorInterface.read()` 接入真实数据源（摄像头/GPS/IoT 设备）
   - `ActuatorInterface.send_command()` 对接执行设备

**相关文件**: agent_scheduler.py、lifecycle_manager.py、a2a_protocol.py、agent_server.py

---

## Phase 3 — 生产化基础设施（低优先级）

> 目标：让系统具备生产部署能力

**步骤**
1. **QoS → 资源调度反馈**（运行层§2）
   - 资源 Agent 读取 `QoSMonitor.get_all_metrics()`，동态决策是否扩/缩实例
   - 延迟超阈值 → 触发 ASD 部署额外实例；成功率过低 → 触发重规划

2. **ARDC 生产级实现**
   - 替换 `agent_registry.json` 文件 mock → etcd / Consul / Zookeeper
   - `AgentRegistryClient` 支持 Watch 机制，Agent 上下线实时感知

3. **认证与权限**
   - API 层添加 JWT 认证（`/api/apps/install` 等管理接口需鉴权）
   - Agent 跨主体调用添加 mTLS 或 Token 验证

4. **前端 UI 升级**
   - 当前 UI 是内联 HTML，拆分为独立 React/Vue 项目
   - 新增：实时工作流可视化（任务 DAG、进度跟踪）、QoS 仪表盘

5. **测试覆盖**
   - 补全 integration 端到端测试
   - CI 流水线：`pytest` + 代码覆盖率 + 静态分析

---

## 优先级建议

```
Phase 1 Step 1+2（跨主体基础）  →  Phase 2 Step 1+2（真实容器+Agent）
         ↓                                    ↓
Phase 1 Step 3+4（停止+重编排）       Phase 2 Step 3（COMM中间件）
         ↓
Phase 3（按需推进）
```

Phase 1 和 Phase 2 Step 1+2 可**并行推进**（互不依赖），建议优先完成这两条线，构成可部署的多节点真实运行系统。