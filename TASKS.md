# TASKS.md — Sprint 任务看板

> 最后更新：2026-03-10

---

## Sprint 当前目标

建立跨对话上下文管理体系（CLAUDE.md + PROJECT_CONTEXT.md + 初始化模板）

---

## 进行中

_（当前无进行中任务）_


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

---

## 已完成（历史）

- [x] §1 应用层：install / start / stop / display + Skills.md 在线编辑
- [x] §2.1 单主体工作流：planner → executor → monitor → reporter
- [x] ARDC HTTP Gossip 智能体发现（PEER_AOE_URLS + /registry/sync）
- [x] ASD subprocess 进程管理（自动端口分配 + 就绪检测）
- [x] §2.2 跨主体编排：httpx dispatch + SessionRegistry + 引用计数
- [x] §2.3 跨主体重编排（Rule 0 failover + find_alternative_remote_aoe）
- [x] §2.4 跨主体工作流停止（DELETE /orchestration/session/{id} 传播）
