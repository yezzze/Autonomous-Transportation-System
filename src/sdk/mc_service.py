"""
记忆中心 (MC) 服务端 — 编排层控制面组件

负责：
1. 记忆域创建 (memory_scope) — AOE 启动时调用，检索长期记忆供 Planner 使用
2. 记忆包生成与物化 (create_memory_bundle) — 为每个 Agent 实例生成专属临时目录
3. 记忆回收 (collect_outbox) — Agent 执行后回收 output，按请求方主归属分流
4. 记忆写回 (commit) — 将候选记忆写回长期存储
5. 跨设备委派接收 — 合入委派上下文生成远端沙箱目录
6. 生命周期管理 — 关闭 workflow 时清理所有临时目录

严格对应 接口流程v2.md:
  §2 单主体工作流 — memory_scope → create_bundle → collect_outbox → commit
  §3 跨主体工作流 — delegated_bundle → 接收合入 → caller-owned writeback
  §5 工作流停止 — collect_all_outboxes → close_workflow_session
  §7 记忆包管理流程
"""

import json
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .memory_models import (
    MemoryBundle,
    MemoryBundlePolicy,
    MemoryScope,
    MemoryScopeContext,
    Manifest,
    CallerInfo,
    DelegatedMemoryBundle,
    OutboxData,
    WritebackEntry,
    CreateBundleRequest,
    CreateBundleResponse,
    MemoryWriteRequest,
)

logger = logging.getLogger(__name__)


class MemoryCenterService:
    """
    记忆中心 (MC) 服务端

    存储设计（初期轻量文件系统）:
      data/memory-store/
        user_memory/{user_id}/{memory_id}.json
        agent_memory/{agent_id}/{memory_id}.json
        workflow_summary/{workflow_id}.json

      data/memory-bundles/
        {workflow_id}/
          {agent_instance_id}/     ← 挂载到 Pod /sandbox/memory
            manifest.json
            input/
            local/
            output/
    """

    def __init__(
        self,
        store_root: str = "./data/memory-store",
        bundles_root: str = "./data/memory-bundles",
    ):
        self._store_root = Path(store_root)
        self._bundles_root = Path(bundles_root)
        self._store_root.mkdir(parents=True, exist_ok=True)
        self._bundles_root.mkdir(parents=True, exist_ok=True)

        # 索引
        self._bundle_index: dict[str, MemoryBundle] = {}       # bundle_id → Bundle
        self._scope_index: dict[str, MemoryScopeContext] = {}  # scope_key → ScopeContext

        logger.info(
            "🧠 MC 服务端初始化: store=%s bundles=%s",
            self._store_root, self._bundles_root,
        )

    # ──────────────────────────────────────────────────────────
    # 路径辅助
    # ──────────────────────────────────────────────────────────

    def _bundle_dir(self, workflow_id: str, instance_id: str) -> Path:
        return self._bundles_root / workflow_id / instance_id

    def _user_memory_dir(self, user_id: str) -> Path:
        return self._store_root / "user_memory" / user_id

    def _agent_memory_dir(self, agent_id: str) -> Path:
        return self._store_root / "agent_memory" / agent_id

    def _collaboration_memory_dir(self, device_id: str, workflow_id: str) -> Path:
        return self._store_root / "collaboration_memory" / device_id / workflow_id

    def _caller_writeback_outbox_dir(self, device_id: str, workflow_id: str) -> Path:
        return self._store_root / "caller_writeback_outbox" / device_id / workflow_id

    def _workflow_summary_path(self, workflow_id: str) -> Path:
        d = self._store_root / "workflow_summary"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{workflow_id}.json"

    # ══════════════════════════════════════════════════════════
    # §2.1 记忆域创建 (memory_scope)
    #
    #   流程（第149-155行）:
    #     AOE->>MC: 创建 memory_scope(device_id, user_id, app_id, workflow_id)
    #     MC->>STORE: 检索本体长期记忆
    #     STORE->>MC: 返回候选记忆
    #     MC->>AOE: 返回 planner_memory_context（供 AOE 生成任务图）
    # ══════════════════════════════════════════════════════════

    async def create_memory_scope(
        self,
        device_id: str,
        user_id: str,
        app_id: str,
        workflow_id: str,
    ) -> MemoryScopeContext:
        """
        创建 Workflow 记忆域

        1. 构建 MemoryScope
        2. 从存储中心检索用户记忆 + Agent 记忆
        3. 构建 planner_memory_context（摘要级）
        4. 缓存 scope 上下文

        Returns:
            MemoryScopeContext: 包含 scope + 记忆摘要 + planner_context
        """
        scope = MemoryScope(
            owner_device_id=device_id,
            owner_user_id=user_id,
            app_id=app_id,
            workflow_id=workflow_id,
        )

        # 从存储检索记忆
        user_memories = self._list_memories(self._user_memory_dir(user_id))
        all_agent_memories: list[dict] = []
        agent_dirs = list(self._store_root.glob(f"agent_memory/*"))
        for ad in agent_dirs:
            all_agent_memories.extend(self._list_memories(ad))

        # 构建 Planner 可读的摘要级记忆
        planner_context = []
        for m in (user_memories + all_agent_memories)[:20]:
            planner_context.append({
                "memory_type": m.get("memory_type", "unknown"),
                "agent_id": m.get("agent_id"),
                "summary": m.get("content", "")[:200],
                "confidence": m.get("confidence"),
            })

        ctx = MemoryScopeContext(
            scope=scope,
            user_memory_summary=user_memories[:10],
            agent_memory_summary=all_agent_memories[:10],
            planner_memory_context=planner_context,
        )

        # 缓存
        self._scope_index[scope.scope_key] = ctx

        logger.info(
            "📋 记忆域已创建: device=%s user=%s workflow=%s "
            "user_mem=%d agent_mem=%d planner_ctx=%d",
            device_id, user_id, workflow_id,
            len(user_memories), len(all_agent_memories), len(planner_context),
        )
        return ctx

    async def get_memory_scope_context(self, scope_key: str) -> Optional[MemoryScopeContext]:
        """获取缓存的 Scope 上下文"""
        return self._scope_index.get(scope_key)

    # ══════════════════════════════════════════════════════════
    # §2 + §7 记忆包创建与物化
    #
    #   流程（第158-165行）:
    #     AOE->>MC: create_memory_bundle(task_id, agent_id, memory_policy)
    #     MC->>STORE: 检索相关本体记忆与 Agent 记忆
    #     MC->>MC: 过滤、摘要、脱敏、排序
    #     MC->>MC: 生成 MemoryBundle
    #     MC->>MC: 物化专属目录 /memory-bundles/{workflow_id}/{agent_instance_id}/
    #     MC->>AOE: 返回 bundle_id 与 memory_mount_spec
    # ══════════════════════════════════════════════════════════

    async def create_bundle(self, req: CreateBundleRequest) -> CreateBundleResponse:
        """
        创建 MemoryBundle 并物化为沙箱目录

        步骤:
          1. 生成 bundle_id 和 agent_instance_id
          2. 创建临时目录 /memory-bundles/{workflow_id}/{agent_instance_id}/
          3. 从存储中心检索 User Memory + Agent Memory
          4. 脱敏、摘要、排序 → 写入 manifest.json / input/ / local/
          5. 返回 bundle_id + memory_mount_spec
        """
        agent_instance_id = req.agent_instance_id or f"inst_{uuid.uuid4().hex[:8]}"
        bundle_id = f"mb_{uuid.uuid4().hex[:12]}"
        policy = req.memory_policy or MemoryBundlePolicy()

        bundle = MemoryBundle(
            bundle_id=bundle_id,
            owner_device_id=req.device_id,
            owner_user_id=req.user_id,
            workflow_id=req.workflow_id,
            task_id=req.task_id,
            agent_id=req.agent_id,
            agent_instance_id=agent_instance_id,
            mount_path="/sandbox/memory",
            visibility="local",
            created_at=datetime.now().isoformat(),
            policy=policy,
        )
        self._bundle_index[bundle_id] = bundle

        # ── 创建目录结构 ──
        bundle_path = self._bundle_dir(req.workflow_id, agent_instance_id)
        input_dir = bundle_path / "input"
        local_dir = bundle_path / "local"
        output_dir = bundle_path / "output"
        artifacts_dir = output_dir / "artifacts"
        for d in [bundle_path, input_dir, local_dir, output_dir, artifacts_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # ── manifest.json ──
        manifest = Manifest(
            bundle_id=bundle_id, workflow_id=req.workflow_id,
            task_id=req.task_id, agent_id=req.agent_id,
            agent_instance_id=agent_instance_id,
            created_at=bundle.created_at, policy=policy,
        )
        self._write_json(bundle_path / "manifest.json", manifest.model_dump())

        # ── input/task.md — 从存储检索用户记忆 ──
        user_memories = self._list_memories(self._user_memory_dir(req.user_id))
        task_lines = [f"# Task: {req.task_id}", ""]
        if user_memories:
            task_lines.append("## 相关用户记忆")
            for m in user_memories[:10]:
                task_lines.append(
                    f"- [{m.get('memory_type','')}] {m.get('content','')[:300]}"
                )
            task_lines.append("")
        (input_dir / "task.md").write_text("\n".join(task_lines), encoding="utf-8")

        # ── input/caller_info.json ──
        self._write_json(
            input_dir / "caller_info.json",
            (req.caller_info or CallerInfo(
                caller_device_id=req.device_id,
                caller_workflow_id=req.workflow_id,
            )).model_dump(),
        )

        # ── input/policy.json ──
        self._write_json(input_dir / "policy.json", policy.model_dump())

        # ── input/delegated_context.json (单主体为空) ──
        self._write_json(input_dir / "delegated_context.json", {
            "task_summary": "", "allowed_memories": [], "constraints": {},
        })

        # ── input/constraints.json ──
        self._write_json(input_dir / "constraints.json", {})

        # ── local/agent_profile.json ──
        self._write_json(local_dir / "agent_profile.json", {
            "agent_id": req.agent_id, "version": "1.0.0", "status": "active",
        })

        # ── local/agent_memory_excerpt.json — 从存储检索 Agent 经验 ──
        agent_memories = self._list_memories(self._agent_memory_dir(req.agent_id))
        self._write_json(local_dir / "agent_memory_excerpt.json", {
            "agent_id": req.agent_id,
            "memory_count": len(agent_memories),
            "recent_memories": agent_memories[:5],
        })

        # ── local/device_public_context.json ──
        self._write_json(local_dir / "device_public_context.json", {
            "device_id": req.device_id,
        })

        mount_spec = {
            "bundle_id": bundle_id,
            "mount_path": "/sandbox/memory",
            "input_mode": "readonly",
            "output_mode": "collect",
            "collector": "sidecar",
            "source_path": str(bundle_path),
        }

        logger.info(
            "📦 记忆包已创建: bundle=%s agent=%s instance=%s",
            bundle_id, req.agent_id, agent_instance_id,
        )
        return CreateBundleResponse(bundle_id=bundle_id, memory_mount_spec=mount_spec)

    async def materialize_bundle(self, bundle_id: str) -> Optional[dict]:
        """确认 Bundle 目录已物化就绪"""
        bundle = self._bundle_index.get(bundle_id)
        if not bundle:
            return None
        bp = self._bundle_dir(bundle.workflow_id, bundle.agent_instance_id)
        if not bp.exists():
            return None
        return {
            "bundle_id": bundle_id, "mount_path": "/sandbox/memory",
            "input_mode": "readonly", "output_mode": "collect",
            "collector": "sidecar", "source_path": str(bp),
        }

    # ══════════════════════════════════════════════════════════
    # §3 跨设备委派 — 合入委派上下文生成远端沙箱目录
    #
    #   流程（第264-270行）:
    #     B_AOE->>B_MC: 接收远端调用请求
    #     B_MC->>B_STORE: 检索目标 Agent 本地经验记忆
    #     B_MC->>B_MC: 合并 delegated_bundle 与本地 Agent 经验
    #     B_MC->>B_MC: 生成远端 Agent 沙箱专属记忆目录
    # ══════════════════════════════════════════════════════════

    async def create_delegated_bundle(
        self,
        delegation: DelegatedMemoryBundle,
        workflow_id: str,
        agent_instance_id: str,
    ) -> CreateBundleResponse:
        """
        被调用方 (设备 B) 接收委派：合并委派上下文 + 本地 Agent 经验，生成沙箱目录

        Returns:
            CreateBundleResponse: bundle_id + mount_spec
        """
        bundle_id = f"mb_{uuid.uuid4().hex[:12]}"
        agent_instance_id = agent_instance_id or f"inst_{uuid.uuid4().hex[:8]}"

        bundle = MemoryBundle(
            bundle_id=bundle_id,
            owner_device_id=delegation.callee_device_id,
            owner_user_id="",
            workflow_id=workflow_id,
            task_id=delegation.caller_task_id,
            agent_id=delegation.target_agent_id,
            agent_instance_id=agent_instance_id,
            caller_device_id=delegation.caller_device_id,
            caller_workflow_id=delegation.caller_workflow_id,
            caller_task_id=delegation.caller_task_id,
            mount_path="/sandbox/memory",
            visibility="delegated",
            created_at=datetime.now().isoformat(),
        )
        self._bundle_index[bundle_id] = bundle

        bundle_path = self._bundle_dir(workflow_id, agent_instance_id)
        input_dir = bundle_path / "input"
        local_dir = bundle_path / "local"
        output_dir = bundle_path / "output"
        (output_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        for d in [bundle_path, input_dir, local_dir, output_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # manifest
        self._write_json(bundle_path / "manifest.json", Manifest(
            bundle_id=bundle_id, workflow_id=workflow_id,
            task_id=delegation.caller_task_id,
            agent_id=delegation.target_agent_id,
            agent_instance_id=agent_instance_id,
            created_at=bundle.created_at,
        ).model_dump())

        # input/task.md — 合并委派上下文
        task_lines = [
            f"# 远端委派任务: {delegation.caller_task_id}",
            f"## 来自设备 {delegation.caller_device_id} 的工作流 {delegation.caller_workflow_id}",
            "",
            delegation.delegated_context.task_summary,
        ]
        (input_dir / "task.md").write_text("\n".join(task_lines), encoding="utf-8")

        # input/delegated_context.json — 委派上下文
        self._write_json(input_dir / "delegated_context.json",
                         delegation.delegated_context.model_dump())

        # input/caller_info.json — 调用方信息
        self._write_json(input_dir / "caller_info.json", {
            "caller_device_id": delegation.caller_device_id,
            "caller_workflow_id": delegation.caller_workflow_id,
            "caller_task_id": delegation.caller_task_id,
        })

        # input/policy.json — 委派策略
        self._write_json(input_dir / "policy.json", delegation.policy.model_dump())
        self._write_json(input_dir / "constraints.json",
                         delegation.delegated_context.constraints)

        # local/agent_memory_excerpt.json — 合并本地 Agent 经验
        agent_memories = self._list_memories(
            self._agent_memory_dir(delegation.target_agent_id)
        )
        self._write_json(local_dir / "agent_memory_excerpt.json", {
            "agent_id": delegation.target_agent_id,
            "memory_count": len(agent_memories),
            "recent_memories": agent_memories[:5],
        })
        self._write_json(local_dir / "agent_profile.json", {
            "agent_id": delegation.target_agent_id, "version": "1.0.0",
        })
        self._write_json(local_dir / "device_public_context.json", {
            "device_id": delegation.callee_device_id,
        })

        mount_spec = {
            "bundle_id": bundle_id, "mount_path": "/sandbox/memory",
            "input_mode": "readonly", "output_mode": "collect",
            "collector": "sidecar", "source_path": str(bundle_path),
        }
        logger.info(
            "📦 委派记忆包已创建: bundle=%s agent=%s caller=%s",
            bundle_id, delegation.target_agent_id, delegation.caller_device_id,
        )
        return CreateBundleResponse(bundle_id=bundle_id, memory_mount_spec=mount_spec)

    # ══════════════════════════════════════════════════════════
    # §2 + §7 Output 回收 (单 bundle)
    #
    #   流程（第192-194行）:
    #     AWM->>MC: collect_outbox(bundle_id)
    #     MC->>MC: 写入候选记忆 (task_result, agent_experience)
    #     MC->>AWM: 写入成功
    # ══════════════════════════════════════════════════════════

    async def collect_outbox(self, bundle_id: str) -> OutboxData:
        """
        回收单个 Agent 实例的 output 目录

        读取 output/ 下所有文件，按 target_owner 分流。
        """
        bundle = self._bundle_index.get(bundle_id)
        if not bundle:
            logger.warning("⚠️  Bundle %s 不存在", bundle_id)
            return OutboxData(has_output=False)

        output_dir = self._bundle_dir(bundle.workflow_id, bundle.agent_instance_id) / "output"
        if not output_dir.exists():
            return OutboxData(has_output=False)

        result = self._read_json(output_dir / "result.json")
        notes = (output_dir / "execution_notes.md").read_text(encoding="utf-8") \
            if (output_dir / "execution_notes.md").exists() else None

        # JSONL 解析
        def read_jsonl(path: Path) -> list[WritebackEntry]:
            if not path.exists():
                return []
            entries = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(WritebackEntry(**json.loads(line)))
                        except Exception:
                            continue
            return entries

        writeback_to_caller = read_jsonl(output_dir / "writeback_to_caller.jsonl")
        writeback_to_local = read_jsonl(output_dir / "writeback_to_local.jsonl")

        artifacts: dict[str, str] = {}
        adir = output_dir / "artifacts"
        if adir.exists():
            for f in adir.iterdir():
                if f.is_file():
                    import base64
                    artifacts[f.name] = base64.b64encode(f.read_bytes()).decode()

        has = bool(result or notes or writeback_to_caller or writeback_to_local or artifacts)
        if has:
            logger.info(
                "📥 回收 output: bundle=%s result=%s caller=%d local=%d art=%d",
                bundle_id, bool(result), len(writeback_to_caller),
                len(writeback_to_local), len(artifacts),
            )
        return OutboxData(
            result=result, execution_notes=notes,
            writeback_to_caller=writeback_to_caller,
            writeback_to_local=writeback_to_local,
            artifacts=artifacts, has_output=has,
        )

    # ══════════════════════════════════════════════════════════
    # §5 collect_all_outboxes — 停止工作流时回收所有 Agent output
    #
    #   流程（第431-433行）:
    #     AWM->>MC: collect_all_outboxes(workflow_id)
    #     MC->>POD: 回收 Agent 输出目录
    #     POD->>MC: 返回 output 记忆候选
    # ══════════════════════════════════════════════════════════

    async def collect_all_outboxes(self, workflow_id: str) -> list[OutboxData]:
        """
        回收 workflow 下所有 Agent 实例的 output

        §5 单主体工作流停止编排时调用:
          回收所有 Agent 输出后，MC 写入候选记忆，再提交 workflow_summary

        Returns:
            每个 Agent 实例的 OutboxData 列表
        """
        results: list[OutboxData] = []
        to_collect = [
            bid for bid, b in self._bundle_index.items()
            if b.workflow_id == workflow_id
        ]
        for bid in to_collect:
            outbox = await self.collect_outbox(bid)
            results.append(outbox)
        logger.info(
            "📥 collect_all_outboxes: workflow=%s bundles=%d",
            workflow_id, len(to_collect),
        )
        return results

    # ══════════════════════════════════════════════════════════
    # §2 + §5 提交记忆包 (commit_bundle)
    #
    #   将候选记忆写回长期存储：
    #     writeback_to_local → agent_memory (本地审计 / Agent 自身经验)
    #     writeback_to_caller → 请求方合作记忆或待转发 outbox
    #     task_result → workflow_summary
    # ══════════════════════════════════════════════════════════

    async def commit_bundle(self, bundle_id: str) -> bool:
        """
        提交记忆包输出：
        1. 回收 output
        2. writeback_to_local → 写入本地 Agent 审计记忆（仅本地审计 / Agent 自身经验）
        3. writeback_to_caller → 写入请求方合作记忆；委派场景下先进入待转发 outbox
        4. task_result → 更新 workflow_summary
        """
        bundle = self._bundle_index.get(bundle_id)
        if not bundle:
            return False

        outbox = await self.collect_outbox(bundle_id)

        # writeback_to_local → 本地 Agent 记忆，仅保存本地审计 / Agent 自身运行经验
        for entry in outbox.writeback_to_local:
            mid = f"mem_{uuid.uuid4().hex[:12]}"
            self._write_json(
                self._agent_memory_dir(bundle.agent_id) / f"{mid}.json",
                {
                    "memory_id": mid,
                    "memory_type": entry.memory_type,
                    "agent_id": entry.agent_id or bundle.agent_id,
                    "content": entry.content,
                    "confidence": entry.confidence,
                    "caller_device_id": entry.caller_device_id,
                    "caller_workflow_id": entry.caller_workflow_id,
                    "metadata": entry.metadata,
                    "created_at": entry.timestamp,
                    "source_bundle": bundle_id,
                },
            )

        # writeback_to_caller → 请求方合作记忆。
        # 委派场景下，当前 MC 属于被调用方，不能把请求方合作记忆写进本地 Agent Memory；
        # 先写入 caller_writeback_outbox，等待 AOE/AWM 转发给请求方 MC。
        for entry in outbox.writeback_to_caller:
            mid = f"mem_{uuid.uuid4().hex[:12]}"
            caller_device_id = (
                entry.caller_device_id
                or bundle.caller_device_id
                or bundle.owner_device_id
                or "unknown_caller"
            )
            caller_workflow_id = (
                entry.caller_workflow_id
                or bundle.caller_workflow_id
                or bundle.workflow_id
            )
            target_dir = (
                self._caller_writeback_outbox_dir(caller_device_id, caller_workflow_id)
                if bundle.visibility == "delegated"
                else self._collaboration_memory_dir(caller_device_id, caller_workflow_id)
            )
            self._write_json(
                target_dir / f"{mid}.json",
                {
                    "memory_id": mid,
                    "memory_type": entry.memory_type,
                    "agent_id": entry.agent_id or bundle.agent_id,
                    "content": entry.content,
                    "confidence": entry.confidence,
                    "target_owner": "caller",
                    "caller_device_id": entry.caller_device_id,
                    "caller_workflow_id": entry.caller_workflow_id,
                    "metadata": entry.metadata,
                    "created_at": entry.timestamp,
                    "source_bundle": bundle_id,
                    "delivery_state": (
                        "pending_forward_to_caller"
                        if bundle.visibility == "delegated"
                        else "stored_in_caller_scope"
                    ),
                },
            )

        # task_result → workflow_summary
        if outbox.result:
            sp = self._workflow_summary_path(bundle.workflow_id)
            summary = self._read_json(sp) or {
                "workflow_id": bundle.workflow_id, "tasks": [],
            }
            summary["tasks"].append({
                "task_id": bundle.task_id, "agent_id": bundle.agent_id,
                "bundle_id": bundle_id, "result": outbox.result,
                "execution_notes": outbox.execution_notes,
                "completed_at": datetime.now().isoformat(),
            })
            self._write_json(sp, summary)
            logger.info(
                "📋 工作流摘要已更新: workflow=%s task=%s",
                bundle.workflow_id, bundle.task_id,
            )

        logger.info(
            "✅ Bundle 已提交: bundle=%s local=%d caller=%d",
            bundle_id, len(outbox.writeback_to_local), len(outbox.writeback_to_caller),
        )
        return True

    # ══════════════════════════════════════════════════════════
    # §5 关闭 workflow memory session
    #
    #   流程（第442行）:
    #     AWM->>MC: 关闭 workflow memory session
    #     MC->>AWM: 关闭成功
    # ══════════════════════════════════════════════════════════

    async def close_workflow_session(self, workflow_id: str) -> int:
        """
        关闭工作流记忆会话：
        1. 回收所有 Agent output
        2. 清理 Bundle 临时目录
        3. 删除索引

        Returns: 清理的 Bundle 数量
        """
        # 先回收所有 output
        await self.collect_all_outboxes(workflow_id)

        to_remove = [
            bid for bid, b in self._bundle_index.items()
            if b.workflow_id == workflow_id
        ]
        workflow_dir = self._bundles_root / workflow_id
        if workflow_dir.exists():
            shutil.rmtree(workflow_dir)

        for bid in to_remove:
            self._bundle_index.pop(bid, None)

        # 清理 scope 缓存
        scope_keys = [
            k for k, ctx in self._scope_index.items()
            if workflow_id in k
        ]
        for k in scope_keys:
            self._scope_index.pop(k, None)

        logger.info(
            "🗑️  工作流会话已关闭: workflow=%s bundles=%d",
            workflow_id, len(to_remove),
        )
        return len(to_remove)

    # ══════════════════════════════════════════════════════════
    # §12.3 记忆存储操作
    # ══════════════════════════════════════════════════════════

    async def search_memories(
        self,
        query: str,
        agent_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        user_id: Optional[str] = None,
        device_id: Optional[str] = None,
        top_k: int = 10,
        filters: Optional[dict] = None,
    ) -> list[dict]:
        """搜索长期存储中的记忆（关键词匹配，后续可接向量检索）"""
        results = []
        if agent_id:
            for m in self._list_memories(self._agent_memory_dir(agent_id)):
                if self._match(m, query, filters):
                    results.append(m)
        if user_id:
            for m in self._list_memories(self._user_memory_dir(user_id)):
                if self._match(m, query, filters):
                    results.append(m)
        results.sort(key=lambda m: m.get("confidence", 0) or 0, reverse=True)
        return results[:top_k]

    async def write_memory(self, req: MemoryWriteRequest) -> str:
        """写入一条记忆"""
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        rec = {
            "memory_id": mid, "memory_type": req.memory_type,
            "agent_id": req.agent_id, "workflow_id": req.workflow_id,
            "device_id": req.device_id, "user_id": req.user_id,
            "content": req.content, "metadata": req.metadata,
            "created_at": datetime.now().isoformat(),
        }
        if req.agent_id:
            self._write_json(self._agent_memory_dir(req.agent_id) / f"{mid}.json", rec)
        elif req.user_id:
            self._write_json(self._user_memory_dir(req.user_id) / f"{mid}.json", rec)
        return mid

    async def write_collaboration_memory(
        self,
        device_id: str,
        workflow_id: str,
        entry: WritebackEntry,
        source_bundle: Optional[str] = None,
    ) -> str:
        """写入请求方合作记忆。用于 A_MC 接收远端 writeback。"""
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        self._write_json(
            self._collaboration_memory_dir(device_id, workflow_id) / f"{mid}.json",
            {
                "memory_id": mid,
                "memory_type": entry.memory_type,
                "agent_id": entry.agent_id,
                "content": entry.content,
                "confidence": entry.confidence,
                "target_owner": "caller",
                "caller_device_id": device_id,
                "caller_workflow_id": workflow_id,
                "metadata": entry.metadata,
                "created_at": entry.timestamp,
                "source_bundle": source_bundle,
                "delivery_state": "stored_in_caller_scope",
            },
        )
        return mid

    async def batch_write_memories(self, entries: list[MemoryWriteRequest]) -> int:
        count = 0
        for e in entries:
            await self.write_memory(e)
            count += 1
        return count

    async def get_memory(self, memory_id: str) -> Optional[dict]:
        for base in [self._store_root / "user_memory", self._store_root / "agent_memory"]:
            if not base.exists():
                continue
            for d in base.iterdir():
                p = d / f"{memory_id}.json"
                if p.exists():
                    return self._read_json(p)
        return None

    async def delete_memory(self, memory_id: str) -> bool:
        for base in [self._store_root / "user_memory", self._store_root / "agent_memory"]:
            if not base.exists():
                continue
            for d in base.iterdir():
                p = d / f"{memory_id}.json"
                if p.exists():
                    p.unlink()
                    return True
        return False

    # ──────────────────────────────────────────────────────────
    # Bundle 查询 / 删除
    # ──────────────────────────────────────────────────────────

    async def get_bundle_info(self, bundle_id: str) -> Optional[MemoryBundle]:
        return self._bundle_index.get(bundle_id)

    async def get_bundle_path(self, bundle_id: str) -> Optional[str]:
        b = self._bundle_index.get(bundle_id)
        if not b:
            return None
        p = self._bundle_dir(b.workflow_id, b.agent_instance_id)
        return str(p) if p.exists() else None

    async def delete_bundle(self, bundle_id: str) -> bool:
        b = self._bundle_index.get(bundle_id)
        if not b:
            return False
        p = self._bundle_dir(b.workflow_id, b.agent_instance_id)
        if p.exists():
            shutil.rmtree(p)
        self._bundle_index.pop(bundle_id, None)
        return True

    # ──────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _write_json(path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _list_memories(self, directory: Path) -> list[dict]:
        if not directory.exists():
            return []
        mems = []
        for f in directory.iterdir():
            if f.suffix == ".json":
                d = self._read_json(f)
                if d:
                    mems.append(d)
        mems.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return mems

    @staticmethod
    def _match(memory: dict, query: str, filters: Optional[dict]) -> bool:
        if query:
            text = f"{memory.get('content','')} {memory.get('memory_type','')}".lower()
            if query.lower() not in text:
                return False
        if filters:
            for k, v in filters.items():
                if k in memory and memory[k] != v:
                    return False
        return True
