"""
记忆中心 (MC) FastAPI Router

严格对应 接口流程v2.md 的接口定义:
  §12.1 本地记忆包接口 — 6 个端点
  §12.2 跨设备委派接口 — 4 个端点
  §12.3 记忆查询接口 — 5 个端点

注意路由顺序：带路径参数的端点需注意先后顺序，
/{memory_id} 通配路由必须放在所有具体路由之后。
"""

import base64
import io
import json as json_lib
import logging
import tarfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .memory_models import (
    CreateBundleRequest,
    CreateScopeRequest,
    DelegatedMemoryBundle,
    MemoryWriteRequest,
    BatchMemoryWriteRequest,
    MemorySearchRequest,
    SessionPathResponse,
    WritebackEntry,
)
from .mc_service import MemoryCenterService

logger = logging.getLogger(__name__)

mc_router = APIRouter(prefix="/memory", tags=["记忆中心 (MC)"])

# ── 全局 MC 服务实例 ──
_mc_service: Optional[MemoryCenterService] = None


def get_mc() -> MemoryCenterService:
    global _mc_service
    if _mc_service is None:
        _mc_service = MemoryCenterService()
    return _mc_service


def set_mc_service(service: MemoryCenterService):
    global _mc_service
    _mc_service = service


# ════════════════════════════════════════════════════════════
# 记忆域 (MemoryScope) — §2 流程
# ════════════════════════════════════════════════════════════

@mc_router.post("/scope/create", summary="创建 Workflow 记忆域")
async def create_scope(req: CreateScopeRequest):
    """AOE 调用：创建 memory_scope"""
    ctx = await get_mc().create_memory_scope(
        device_id=req.device_id,
        user_id=req.user_id,
        app_id=req.app_id,
        workflow_id=req.workflow_id,
    )
    return {"code": 0, "message": "记忆域创建成功", "data": ctx.model_dump()}


# ════════════════════════════════════════════════════════════
# §12.1 本地记忆包接口 (6 个端点)
# ════════════════════════════════════════════════════════════

@mc_router.post("/bundles/create", summary="创建 MemoryBundle (AOE → MC)")
async def create_bundle(req: CreateBundleRequest):
    result = await get_mc().create_bundle(req)
    return {"code": 0, "message": "记忆包创建成功", "data": result.model_dump()}


@mc_router.post("/bundles/{bundle_id}/materialize", summary="物化记忆包为沙箱目录")
async def materialize_bundle(bundle_id: str):
    spec = await get_mc().materialize_bundle(bundle_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="Bundle 不存在或未物化")
    return {"code": 0, "message": "物化成功", "data": spec}


@mc_router.get("/bundles/{bundle_id}/download", summary="initContainer 下载记忆包")
async def download_bundle(bundle_id: str):
    path = await get_mc().get_bundle_path(bundle_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Bundle 不存在")
    return {"code": 0, "message": "下载成功", "data": {"bundle_id": bundle_id, "source_path": path}}


@mc_router.get("/bundles/{bundle_id}/archive", summary="initContainer 下载记忆包 tar.gz")
async def download_bundle_archive(bundle_id: str):
    """
    返回 MemoryBundle 的可下载归档。

    用于 K8s emptyDir 模式：
    - initContainer 从该接口下载 tar.gz
    - 解压到 /sandbox/memory
    - Agent 只读 input/local，写 output

    output/ 不进入归档，因为它由 Agent 在沙箱内执行后生成。
    """
    path = await get_mc().get_bundle_path(bundle_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Bundle 不存在")

    bundle_path = Path(path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for child in bundle_path.iterdir():
            if child.name == "output":
                continue
            tar.add(child, arcname=child.name)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle_id}.tar.gz"',
        },
    )


@mc_router.post("/bundles/{bundle_id}/upload_outbox", summary="Sidecar 上传 Agent output")
async def upload_outbox(bundle_id: str, payload: dict):
    mc = get_mc()
    bundle = await mc.get_bundle_info(bundle_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Bundle 不存在")

    bundle_path = Path(await mc.get_bundle_path(bundle_id) or "")
    output_dir = bundle_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    adir = output_dir / "artifacts"
    adir.mkdir(parents=True, exist_ok=True)

    if "result" in payload:
        (output_dir / "result.json").write_text(
            json_lib.dumps(payload["result"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if "execution_notes" in payload:
        (output_dir / "execution_notes.md").write_text(payload["execution_notes"], encoding="utf-8")
    if "writeback_to_caller" in payload:
        with open(output_dir / "writeback_to_caller.jsonl", "a", encoding="utf-8") as f:
            for entry in payload["writeback_to_caller"]:
                f.write(json_lib.dumps(entry, ensure_ascii=False) + "\n")
    if "writeback_to_local" in payload:
        with open(output_dir / "writeback_to_local.jsonl", "a", encoding="utf-8") as f:
            for entry in payload["writeback_to_local"]:
                f.write(json_lib.dumps(entry, ensure_ascii=False) + "\n")
    if "artifacts" in payload:
        for name, b64_data in payload["artifacts"].items():
            (adir / name).write_bytes(base64.b64decode(b64_data))

    return {"code": 0, "message": "Outbox 上传成功"}


@mc_router.post("/bundles/{bundle_id}/commit", summary="提交记忆包输出 (回收+写回)")
async def commit_bundle(bundle_id: str):
    ok = await get_mc().commit_bundle(bundle_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Bundle 不存在或提交失败")
    return {"code": 0, "message": "提交成功"}


@mc_router.delete("/bundles/{bundle_id}", summary="删除或归档记忆包")
async def delete_bundle(bundle_id: str):
    ok = await get_mc().delete_bundle(bundle_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Bundle 不存在")
    return {"code": 0, "message": "删除成功"}


# ════════════════════════════════════════════════════════════
# §12.2 跨设备委派接口 (4 个端点)
# ════════════════════════════════════════════════════════════

_delegation_index: dict[str, DelegatedMemoryBundle] = {}


@mc_router.post("/delegations/create", summary="调用方创建委派记忆包")
async def create_delegation(req: DelegatedMemoryBundle):
    _delegation_index[req.delegation_id] = req
    return {
        "code": 0, "message": "委派记忆包创建成功",
        "data": {"delegation_id": req.delegation_id},
    }


@mc_router.post("/delegations/accept", summary="被调用方接收委派记忆包")
async def accept_delegation(payload: dict):
    did = payload.get("delegation_id", "")
    delegation = _delegation_index.get(did)
    if not delegation:
        raise HTTPException(status_code=404, detail="委派记忆包不存在")
    workflow_id = payload.get("workflow_id", f"remote_{did}")
    instance_id = payload.get("agent_instance_id", "")
    result = await get_mc().create_delegated_bundle(delegation, workflow_id, instance_id)
    return {"code": 0, "message": "委派记忆包接收成功", "data": result.model_dump()}


@mc_router.post("/delegations/{delegation_id}/writeback", summary="被调用方向调用方写回候选记忆")
async def delegation_writeback(delegation_id: str, payload: dict):
    delegation = _delegation_index.get(delegation_id)
    if not delegation:
        raise HTTPException(status_code=404, detail="委派记忆包不存在")
    entries = payload.get("writeback_entries", [])
    written_ids = []
    for ed in entries:
        entry = WritebackEntry(
            content=ed.get("content", ""),
            memory_type=ed.get("memory_type", "remote_collaboration_experience"),
            target_owner="caller",
            agent_id=delegation.target_agent_id,
            caller_device_id=delegation.caller_device_id,
            caller_workflow_id=delegation.caller_workflow_id,
            confidence=ed.get("confidence"),
            metadata=ed.get("metadata", {}),
        )
        mid = await get_mc().write_collaboration_memory(
            delegation.caller_device_id,
            delegation.caller_workflow_id,
            entry,
            source_bundle=ed.get("source_bundle"),
        )
        written_ids.append(mid)
    return {
        "code": 0,
        "message": "委派写回成功",
        "data": {"memory_ids": written_ids},
    }


@mc_router.delete("/delegations/{delegation_id}", summary="删除或关闭委派会话")
async def delete_delegation(delegation_id: str):
    _delegation_index.pop(delegation_id, None)
    return {"code": 0, "message": "委派会话已关闭"}


# ════════════════════════════════════════════════════════════
# §5 辅助端点 — 先于 /{memory_id} 通配路由定义
# ════════════════════════════════════════════════════════════

class _NotifyRequest(BaseModel):
    agent_id: str = ""


@mc_router.post("/outboxes/collect_all", summary="回收 workflow 下所有 Agent output")
async def collect_all_outboxes(payload: dict):
    """§5 collect_all_outboxes(workflow_id)"""
    workflow_id = payload.get("workflow_id", "")
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id 必填")
    results = await get_mc().collect_all_outboxes(workflow_id)
    return {
        "code": 0,
        "message": f"已回收 {len(results)} 个 Agent output",
        "data": {"outboxes": [r.model_dump() for r in results]},
    }


@mc_router.post("/workflow/{workflow_id}/close", summary="关闭 workflow memory session")
async def close_workflow_session(workflow_id: str):
    """§5 关闭 workflow memory session"""
    count = await get_mc().close_workflow_session(workflow_id)
    return {"code": 0, "message": f"工作流会话已关闭，清理 {count} 个 Bundle"}


@mc_router.get("/session/{instance_id}", summary="Agent 查询记忆路径信息")
async def get_session_path(instance_id: str):
    """Agent 向 MC 查询当前实例的 memory 路径信息"""
    mc = get_mc()
    for bid, bundle in mc._bundle_index.items():
        if bundle.agent_instance_id == instance_id:
            bp = await mc.get_bundle_path(bid)
            return {
                "code": 0,
                "data": SessionPathResponse(
                    instance_id=instance_id,
                    agent_id=bundle.agent_id,
                    bundle_id=bid,
                    mount_path=bundle.mount_path,
                    manifest_path=str(Path(bp or "") / "manifest.json"),
                    exists=bp is not None,
                ).model_dump(),
            }
    raise HTTPException(status_code=404, detail="实例不存在")


@mc_router.post("/session/{instance_id}/notify_start", summary="Agent 通知任务开始")
async def notify_task_started(instance_id: str, req: _NotifyRequest):
    logger.info("▶️  任务开始: instance=%s agent=%s", instance_id, req.agent_id)
    return {"code": 0, "message": "已记录"}


@mc_router.post("/session/{instance_id}/notify_complete", summary="Agent 通知任务完成")
async def notify_task_completed(instance_id: str, req: _NotifyRequest):
    logger.info("✅  任务完成: instance=%s agent=%s", instance_id, req.agent_id)
    return {"code": 0, "message": "已记录，等待 sidecar 回收 output"}


# ════════════════════════════════════════════════════════════
# §12.3 记忆查询接口 (5 个端点)
# 注意: /{memory_id} 通配路由必须放在最后
# ════════════════════════════════════════════════════════════

@mc_router.post("/search", summary="查询本体记忆 / Agent 记忆")
async def search_memories(req: MemorySearchRequest):
    results = await get_mc().search_memories(
        query=req.query, agent_id=req.agent_id,
        workflow_id=req.workflow_id, user_id=req.user_id,
        top_k=req.top_k, filters=req.filters or {},
    )
    return {"code": 0, "message": "查询成功", "data": {"results": results, "total": len(results)}}


@mc_router.post("/write", summary="写入本地记忆")
async def write_memory(req: MemoryWriteRequest):
    mid = await get_mc().write_memory(req)
    return {"code": 0, "message": "写入成功", "data": {"memory_id": mid}}


@mc_router.post("/batch_write", summary="批量写入本地记忆")
async def batch_write(req: BatchMemoryWriteRequest):
    count = await get_mc().batch_write_memories(req.entries)
    return {"code": 0, "message": f"批量写入成功，共 {count} 条", "data": {"count": count}}


@mc_router.get("/{memory_id}", summary="查询单条记忆")
async def get_memory(memory_id: str):
    """§12.3 GET /memory/{memory_id}"""
    mem = await get_mc().get_memory(memory_id)
    if mem is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"code": 0, "message": "查询成功", "data": mem}


@mc_router.delete("/{memory_id}", summary="删除记忆")
async def delete_memory(memory_id: str):
    """§12.3 DELETE /memory/{memory_id}"""
    ok = await get_mc().delete_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"code": 0, "message": "删除成功"}
