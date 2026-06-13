# TASKS.md — Sprint 任务看板

> 最后更新：2026-05-07

---

## Sprint 当前目标

跨节点编排增强 + 子工作流即服务 + 周期性调度

---

## 进行中

- [ ] 验证集群侧 NVIDIA device plugin 是否已加载 time-slicing 配置（ConfigMap 挂载与参数生效）
- [ ] 周期调度记录聚合的前端交互优化（调度会话详情筛选、历史分页）
- [ ] 应用详情页可视化联动稳定性验证：`/ui/apps/{app_id}` 三可视化 tab 在无 `workflow_handle`、运行中、调度会话三场景下的数据映射与回退展示


---

## 待办

- [ ] **P0** Focus List 结构化工作记忆：`distributed_types.py` 加字段 + `progress_ledger.py` 加第 6 维 + `magentic_nodes.py` 维护 focus_list
- [ ] **P1** Monitor 主动超时检测（Watchdog）：`distributed_nodes.py` Monitor 节点 + `agent_server.py` 新增进度查询端点
- [ ] **P1** 跨主体异步 Dispatch：Executor 非阻塞调用 + Monitor 轮询/回调 + TaskAssignment 增加 dispatched 状态
- [ ] **P2** 事件触发层：新增 `src/service/event_trigger.py` 模块 + `server.py` webhook 端点
- [ ] **P2** 自适应控制参数：Progress Ledger 增加 strategy_adjustment 维度
- [ ] COMM 消息路由中间件（`src/service/message_router.py`）：按 capability 路由，解耦 Agent 直连
- [ ] RRDC 跨节点资源分配（当前为内存 mock）
- [ ] ASD 接入 Docker SDK（当前为 subprocess 替代）
- [ ] 跨主体编排集成测试：两进程模拟 T_AOE + E_AOE 全链路

---

## 已完成（本 Sprint）

- [x] **跨主体编排路由迁移到 app.py（2026-06-01）**：在 `src/api/app.py` 新增 `POST /orchestration/register_subworkflow`（编排期注册）与 `POST /orchestration/execute/{sub_workflow_id}`（运行期执行），并将 `POST /orchestration/dispatch` 改为兼容层（先注册再执行），避免实际系统依赖 `agent_server.py` 的临时测试路由。
- [x] **ARDC Gossip 推送/接收地址与来源标记对齐（2026-06-01）**：`src/service/agent_registry.py` 的 `push_to_peer()` 发送前会将本地 agents payload 的 `ip/port` 覆盖为 `local_url` 解析结果并标记 `is_local=false`；`sync_from_peer()` 接收侧也会将入站 agents 归一化为 `is_local=false` 后写入 peer 缓存，确保跨节点视图一致且不误判本地来源。
- [x] **工作流标识透传对齐（2026-05-25）**：`run_distributed_workflow()` 新增 `workflow_id` 参数并在 VizBus 注册阶段优先使用；`AppLogicEngine._run_workflow()` 调用时已传入 `workflow_handle`，统一应用层句柄与可视化工作流 ID。
- [x] **应用详情页 Tab 重构（2026-05-25）**：`src/api/templates/app_details.html` 已移除“运行状态”tab，并接入 `visualization.html` 的“编排过程/拓扑结果/执行监控”三 tab，最终顺序为“逻辑文件 → 编排过程 → 拓扑结果 → 执行监控 → 智能体视图”；`src/api/static/js/app_details.js` 已改为通过 `/api/viz/*` 与 `/ws/viz/*` 渲染实时可视化数据。
- [x] **Agent 本地/远端判定统一**（2026-05-22）：`agent_registry.json` 新增 `is_local` 字段，`src/api/visualization.py`、`src/graph/distributed_nodes.py`、`src/service/agent_startup.py` 改为只读注册表字段判断 local/remote，移除前端/调度层硬编码本地集合。
- [x] **周期调度可视化聚合**（2026-05-22）：`WorkflowScheduler` 启动时创建 `schedule_workflow_handle` 主记录并写入 `viz_bus`，周期子工作流关闭逐条可视化注册，前端 `/viz` 改为按调度会话记录展示；`ScheduleExecutionRecord` 新增 `schedule_workflow_handle` 便于追踪。
- [x] **周期调度前端刷新抑制**（2026-05-22）：`static/visualization.html` 对 Pane1 增加“按工作流编排签名缓存 + 调度空快照忽略”逻辑，周期触发不再把 Skills/Pipeline 回退为空，同时保留“第 N 次调度 · m/n”下拉展示。
- [x] **APPM 调度句柄对齐**（2026-05-26）：`AppManager.start_schedule()` 启动成功后回填 `app.workflow_handle=schedule_workflow_handle`，`stop_schedule()` 停止后清空 `app.workflow_handle`，保证与普通 `start()/stop()` 的句柄语义一致。


## 已完成（历史）

