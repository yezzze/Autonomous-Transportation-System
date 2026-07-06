"""
Agent 沙箱记忆 SDK — SandboxMemory

Agent 在沙箱内通过此 SDK 操作 /sandbox/memory/ 目录。

核心原则（对应 智能体编排层接口流程v2.md §0.2）：
- Agent 沙箱只能访问 /sandbox/memory/
- Agent 不直接访问 MC 全量存储
- input/ + local/ = MC 物化好的只读输入
- output/ = Agent 写入，由 sidecar/MC 回收后入库

双模式：
- 文件系统模式（默认）：直接读写 /sandbox/memory/
- HTTP API 模式（可选）：通过 MC 服务端点查询路径 / 通知状态
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from .memory_models import (
    Manifest,
    MemoryBundlePolicy as MemoryPolicy,
    CallerInfo,
    DelegatedContext,
    WritebackEntry,
)

logger = logging.getLogger(__name__)


class SandboxMemory:
    """
    Agent 沙箱记忆 SDK

    Agent 在沙箱内通过此对象操作 /sandbox/memory/。
    所有记忆数据已经由 MC 物化到文件系统，SDK 只负责读写文件。

    用法:
        mem = SandboxMemory()                          # 文件系统模式
        mem = SandboxMemory(mc_api_url="http://...")    # 双模式
        mem = SandboxMemory(memory_root="/custom/path") # 自定义路径
    """

    def __init__(
        self,
        memory_root: str = "/sandbox/memory",
        mc_api_url: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_instance_id: Optional[str] = None,
        auto_discover: bool = True,
    ):
        """
        Args:
            memory_root: 沙箱 memory 挂载点，默认 /sandbox/memory
            mc_api_url:  MC 服务 HTTP 地址（可选），设置后支持 notify/query API
            agent_id:  当前 Agent ID（可选，auto_discover 时从 manifest 自动读取）
            agent_instance_id: 当前实例 ID（可选，auto_discover 时自动读取）
            auto_discover: 是否自动从 manifest.json 发现身份
        """
        self._memory_root = Path(memory_root)
        self._mc_api_url = mc_api_url.rstrip("/") if mc_api_url else None
        self._agent_id = agent_id
        self._agent_instance_id = agent_instance_id
        self._manifest: Optional[Manifest] = None
        self._http_client: Optional[httpx.AsyncClient] = None

        # 自动发现
        if auto_discover:
            self._try_auto_discover()

    # ──────────────────────────────────────────────────────────
    # 属性
    # ──────────────────────────────────────────────────────────

    @property
    def memory_root(self) -> Path:
        """记忆根目录"""
        return self._memory_root

    @property
    def manifest(self) -> Optional[Manifest]:
        """缓存的 manifest"""
        return self._manifest

    @property
    def agent_id(self) -> Optional[str]:
        return self._agent_id

    @property
    def agent_instance_id(self) -> Optional[str]:
        return self._agent_instance_id

    @property
    def has_api(self) -> bool:
        """是否配置了 MC API 地址"""
        return self._mc_api_url is not None

    # ──────────────────────────────────────────────────────────
    # 自动发现
    # ──────────────────────────────────────────────────────────

    def _try_auto_discover(self):
        """尝试从 manifest.json 自动发现身份信息"""
        manifest_path = self._memory_root / "manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                self._manifest = Manifest(**data)
                if not self._agent_id:
                    self._agent_id = self._manifest.agent_id
                if not self._agent_instance_id:
                    self._agent_instance_id = self._manifest.agent_instance_id
                logger.info(
                    "📋 自动发现: agent=%s instance=%s bundle=%s",
                    self._agent_id,
                    self._agent_instance_id,
                    self._manifest.bundle_id,
                )
            except Exception as e:
                logger.warning("⚠️  manifest.json 读取失败: %s", e)
        else:
            logger.debug("ℹ️  manifest.json 不存在 (memory_root=%s)", self._memory_root)

    # ──────────────────────────────────────────────────────────
    # HTTP 客户端（懒加载）
    # ──────────────────────────────────────────────────────────

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # ──────────────────────────────────────────────────────────
    # 文件系统辅助
    # ──────────────────────────────────────────────────────────

    def _ensure_output_dir(self):
        """确保 output/ 子目录存在"""
        (self._memory_root / "output" / "artifacts").mkdir(parents=True, exist_ok=True)

    def _read_json(self, rel_path: str) -> Optional[dict]:
        """读取 JSON 文件，返回 None 如果不存在"""
        path = self._memory_root / rel_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("⚠️  读取 %s 失败: %s", rel_path, e)
            return None

    def _write_json(self, rel_path: str, data: dict):
        """写入 JSON 文件"""
        path = self._memory_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug("📝 写入 %s", rel_path)

    def _read_text(self, rel_path: str) -> Optional[str]:
        """读取文本文件"""
        path = self._memory_root / rel_path
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("⚠️  读取 %s 失败: %s", rel_path, e)
            return None

    def _write_text(self, rel_path: str, content: str):
        """写入文本文件"""
        path = self._memory_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.debug("📝 写入 %s", rel_path)

    def _append_jsonl(self, rel_path: str, entry: dict):
        """追加一行 JSONL"""
        path = self._memory_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("📝 追加 %s", rel_path)

    def file_exists(self, rel_path: str) -> bool:
        """检查文件是否存在"""
        return (self._memory_root / rel_path).exists()

    def list_dir(self, rel_path: str) -> list[Path]:
        """列出目录下的所有文件"""
        target = self._memory_root / rel_path
        if target.exists() and target.is_dir():
            return sorted(target.iterdir())
        return []

    # ══════════════════════════════════════════════════════════
    # 读 input/ 目录（由 MC 物化，Agent 只读）
    # ══════════════════════════════════════════════════════════

    async def read_manifest(self) -> Optional[Manifest]:
        """读取 manifest.json"""
        data = self._read_json("manifest.json")
        if data:
            self._manifest = Manifest(**data)
            return self._manifest
        return self._manifest

    async def read_task_description(self) -> Optional[str]:
        """读取 input/task.md — 当前任务描述"""
        return self._read_text("input/task.md")

    async def read_caller_info(self) -> Optional[CallerInfo]:
        """读取 input/caller_info.json — 调用方信息"""
        data = self._read_json("input/caller_info.json")
        if data:
            return CallerInfo(**data)
        return None

    async def read_policy(self) -> Optional[MemoryPolicy]:
        """读取 input/policy.json — 记忆读写策略"""
        data = self._read_json("input/policy.json")
        if data:
            return MemoryPolicy(**data)
        # fallback: 从 manifest 读取
        if self._manifest:
            return self._manifest.policy
        return None

    async def read_delegated_context(self) -> Optional[DelegatedContext]:
        """读取 input/delegated_context.json — 跨设备委派上下文"""
        data = self._read_json("input/delegated_context.json")
        if data:
            return DelegatedContext(**data)
        return None

    async def read_constraints(self) -> Optional[dict]:
        """读取 input/constraints.json — 任务约束"""
        return self._read_json("input/constraints.json")

    # ══════════════════════════════════════════════════════════
    # 读 local/ 目录（由被调用方本地 MC 生成，Agent 只读）
    # ══════════════════════════════════════════════════════════

    async def read_agent_profile(self) -> Optional[dict]:
        """读取 local/agent_profile.json — Agent 身份、能力、约束"""
        return self._read_json("local/agent_profile.json")

    async def read_agent_memory_excerpt(self) -> Optional[dict]:
        """读取 local/agent_memory_excerpt.json — Agent 本地经验摘录"""
        return self._read_json("local/agent_memory_excerpt.json")

    async def read_device_context(self) -> Optional[dict]:
        """读取 local/device_public_context.json — 设备公开上下文"""
        return self._read_json("local/device_public_context.json")

    # ══════════════════════════════════════════════════════════
    # 写 output/ 目录（Agent 写入，Sidecar/MC 回收）
    # ══════════════════════════════════════════════════════════

    async def write_result(self, result: dict):
        """写入 output/result.json — 任务执行结果"""
        self._ensure_output_dir()
        self._write_json("output/result.json", result)

    async def write_execution_notes(self, notes: str):
        """写入 output/execution_notes.md — 执行笔记"""
        self._ensure_output_dir()
        self._write_text("output/execution_notes.md", notes)

    async def write_to_caller(self, entry: WritebackEntry):
        """
        追加写入 output/writeback_to_caller.jsonl
        返回给调用方 MC 的候选记忆（跨设备场景）。
        跨主体合作记忆以请求方为主归属，应优先写入该通道。
        """
        if not entry.target_owner:
            entry.target_owner = "caller"
        self._ensure_output_dir()
        self._append_jsonl("output/writeback_to_caller.jsonl", entry.model_dump())

    async def write_to_local(self, entry: WritebackEntry):
        """
        追加写入 output/writeback_to_local.jsonl
        写回本地 Agent Memory 的候选记忆。
        跨主体场景仅用于本地审计、资源表现、Agent 自身运行经验。
        """
        if not entry.target_owner:
            entry.target_owner = "local_agent"
        self._ensure_output_dir()
        self._append_jsonl("output/writeback_to_local.jsonl", entry.model_dump())

    async def write_artifact(self, filename: str, data: bytes):
        """
        写入 output/artifacts/{filename} — 工件文件

        Args:
            filename: 文件名（如 "chart.png", "report.pdf"）
            data: 二进制内容
        """
        artifacts_dir = self._memory_root / "output" / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / filename).write_bytes(data)
        logger.debug("📝 写入工件 output/artifacts/%s (%d bytes)", filename, len(data))

    async def append_to_file(self, sub_path: str, content: str):
        """
        通用追加写入 output/ 下的自定义文件

        Args:
            sub_path: output/ 下的相对路径，如 "custom_log.txt"
            content: 文本内容（会自动追加换行）
        """
        self._ensure_output_dir()
        path = self._memory_root / "output" / sub_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content + "\n")
        logger.debug("📝 追加 output/%s", sub_path)

    async def write_done(self, content: str = "done"):
        """
        写入 output/.done — K8s sidecar 回收 output 的完成信号

        Args:
            content: .done 文件内容，默认 "done"
        """
        self._ensure_output_dir()
        self._write_text("output/.done", content)

    # ══════════════════════════════════════════════════════════
    # 组合便捷方法
    # ══════════════════════════════════════════════════════════

    #: 沙箱标准输入文件清单
    INPUT_FILES = [
        "manifest.json",
        "input/task.md",
        "input/caller_info.json",
        "input/policy.json",
        "input/delegated_context.json",
        "input/constraints.json",
        "local/agent_profile.json",
        "local/agent_memory_excerpt.json",
        "local/device_public_context.json",
    ]

    async def load_all_inputs(self) -> dict:
        """
        一次性加载所有输入文件

        Returns:
            {
                "manifest": Manifest | None,
                "task": str | None,
                "policy": MemoryPolicy | None,
                "caller_info": CallerInfo | None,
                "delegated_context": DelegatedContext | None,
                "constraints": dict | None,
                "agent_profile": dict | None,
                "agent_memory_excerpt": dict | None,
                "device_context": dict | None,
            }
        """
        return {
            "manifest": await self.read_manifest(),
            "task": await self.read_task_description(),
            "policy": await self.read_policy(),
            "caller_info": await self.read_caller_info(),
            "delegated_context": await self.read_delegated_context(),
            "constraints": await self.read_constraints(),
            "agent_profile": await self.read_agent_profile(),
            "agent_memory_excerpt": await self.read_agent_memory_excerpt(),
            "device_context": await self.read_device_context(),
        }

    async def finalize(self, result: dict, notes: str = "", write_done: bool = True):
        """
        快捷收尾：写 result + 写 notes + 通知 MC + 写 .done

        Args:
            result: 执行结果
            notes: 执行笔记（可选）
            write_done: 是否写 output/.done 作为 sidecar 完成信号，默认 True
        """
        await self.write_result(result)
        if notes:
            await self.write_execution_notes(notes)
        if self._mc_api_url:
            await self.notify_task_completed()
        if write_done:
            await self.write_done()
        logger.info("✅ 任务收尾完成")

    # ══════════════════════════════════════════════════════════
    # API 模式 — 通过 HTTP 调用 MC 服务端
    # ══════════════════════════════════════════════════════════

    async def get_memory_path_remote(self) -> Optional[dict]:
        """
        向 MC 查询当前实例的 memory 路径信息

        GET /memory/session/{instance_id}

        Returns:
            {"instance_id": str, "bundle_id": str, "mount_path": str, ...}
            或 None（查询失败）
        """
        if not self._mc_api_url or not self._agent_instance_id:
            logger.warning("⚠️  缺少 mc_api_url 或 agent_instance_id，无法远程查询")
            return None
        client = await self._get_http_client()
        try:
            resp = await client.get(
                f"{self._mc_api_url}/memory/session/{self._agent_instance_id}"
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("⚠️  远程查询 memory path 失败: %s", e)
            return None

    async def notify_task_started(self) -> bool:
        """
        通知 MC 任务已开始

        POST /memory/session/{instance_id}/notify_start

        Returns:
            True 通知成功
        """
        if not self._mc_api_url or not self._agent_instance_id:
            return False
        client = await self._get_http_client()
        try:
            resp = await client.post(
                f"{self._mc_api_url}/memory/session/{self._agent_instance_id}/notify_start",
                json={"agent_id": self._agent_id or ""},
            )
            resp.raise_for_status()
            logger.info("📤 通知 MC: 任务已开始")
            return True
        except Exception as e:
            logger.warning("⚠️  通知任务开始失败: %s", e)
            return False

    async def notify_task_completed(self) -> bool:
        """
        通知 MC 任务已完成

        POST /memory/session/{instance_id}/notify_complete

        Returns:
            True 通知成功
        """
        if not self._mc_api_url or not self._agent_instance_id:
            return False
        client = await self._get_http_client()
        try:
            resp = await client.post(
                f"{self._mc_api_url}/memory/session/{self._agent_instance_id}/notify_complete",
                json={"agent_id": self._agent_id or ""},
            )
            resp.raise_for_status()
            logger.info("📤 通知 MC: 任务已完成")
            return True
        except Exception as e:
            logger.warning("⚠️  通知任务完成失败: %s", e)
            return False
