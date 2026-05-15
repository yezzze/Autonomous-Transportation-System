# TASKS.md — Sprint 任务看板

> 最后更新：2026-05-07

---

## Sprint 当前目标

建立跨对话上下文管理体系（CLAUDE.md + PROJECT_CONTEXT.md + 初始化模板）

---

## 进行中

- [ ] 验证集群侧 NVIDIA device plugin 是否已加载 time-slicing 配置（ConfigMap 挂载与参数生效）


---

## 待办

- [ ] COMM 消息路由中间件（`src/service/message_router.py`）：按 capability 路由，解耦 Agent 直连
- [ ] RRDC 跨节点资源分配（当前为内存 mock）
- [ ] ASD 接入 Docker SDK（当前为 subprocess 替代）
- [ ] 跨主体编排集成测试：两进程模拟 T_AOE + E_AOE 全链路

---

## 已完成（本 Sprint）

- [x] 创建 CLAUDE.md（项目规范入口，AI 自动读取）
- [x] 创建 PROJECT_CONTEXT.md（项目状态快照）
- [x] 创建 TASKS.md（Sprint 任务看板）
- [x] QoS → ASD 反馈闭环：register_alert_callback + cooldown + redeploy_agent + reset_metrics（2026-03-08）
- [x] 创建 `/接口/接口文档.md`：补齐应用层 / 编排层 / 运行层代码接口契约与对外接口说明（2026-03-10）
- [x] 创建 `接口/系统接口与代码映射文档.md`：统一接口文档 — 架构图+类关系图+流程序列图+代码映射表+HTTP API+DistributedState 全字段+设计模式+骨架区域说明（2026-03-10）
- [x] 前端：添加 `应用详情` 页面与路由（`app_details.html` / `app_details.js`），并在应用列表中加入“应用详情”按钮（2026-05-02）
- [x] 前端：应用详情页运行态编排区支持从 `Skills.md` 的 `## Pipeline` 解析工作流并渲染节点/箭头（2026-05-06）
- [x] AgentScheduler 支持优先读取 `k8s/` 根目录 YAML，失败回退到字典构造（2026-05-06）
- [x] 前端：应用列表自动刷新时对 `apps` 结果做签名比对，相同结果不重绘表格（2026-05-07）
- [x] 前端：应用详情页智能体视图改为纵向堆叠 iframe，并新增 `/api/apps/{app_id}/agent-views`（2026-05-07）
- [x] 前端：应用详情页 demo 智能体视图在本地自动改写为 `127.0.0.1:30092`，配合 `kubectl port-forward` 访问（2026-05-08）
- [x] Kubernetes：`k8s/cooperativefeaturefusiondetectionviz-agent.yaml` 与 `k8s/perception2intermediatefeature-agent.yaml` 更新为共享 GPU 友好资源配置（2026-05-07）

---

## 已完成（历史）

- [x] §1 应用层：install / start / stop / display + Skills.md 在线编辑
- [x] §2.1 单主体工作流：planner → executor → monitor → reporter
- [x] ARDC HTTP Gossip 智能体发现（PEER_AOE_URLS + /registry/sync）
- [x] ASD subprocess 进程管理（自动端口分配 + 就绪检测）
- [x] §2.2 跨主体编排：httpx dispatch + SessionRegistry + 引用计数
- [x] §2.3 跨主体重编排（Rule 0 failover + find_alternative_remote_aoe）
- [x] §2.4 跨主体工作流停止（DELETE /orchestration/session/{id} 传播）
