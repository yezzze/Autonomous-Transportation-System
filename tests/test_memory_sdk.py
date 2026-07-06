import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.sdk import SandboxMemory
from src.sdk.mc_service import MemoryCenterService
from src.sdk.memory_models import (
    DelegatedContext,
    DelegatedMemoryBundle,
    WritebackEntry,
)


def test_finalize_writes_done_signal(tmp_path):
    mem = SandboxMemory(memory_root=str(tmp_path), auto_discover=False)

    asyncio.run(mem.finalize({"status": "ok"}, "finished"))

    result_path = tmp_path / "output" / "result.json"
    notes_path = tmp_path / "output" / "execution_notes.md"
    done_path = tmp_path / "output" / ".done"

    assert json.loads(result_path.read_text(encoding="utf-8")) == {"status": "ok"}
    assert notes_path.read_text(encoding="utf-8") == "finished"
    assert done_path.read_text(encoding="utf-8") == "done"


def test_finalize_can_skip_done_signal(tmp_path):
    mem = SandboxMemory(memory_root=str(tmp_path), auto_discover=False)

    asyncio.run(mem.finalize({"status": "ok"}, write_done=False))

    assert not (tmp_path / "output" / ".done").exists()


def test_delegated_commit_keeps_caller_memory_out_of_callee_agent_memory(tmp_path):
    service = MemoryCenterService(
        store_root=str(tmp_path / "store"),
        bundles_root=str(tmp_path / "bundles"),
    )
    delegation = DelegatedMemoryBundle(
        caller_device_id="device_A",
        caller_user_id="user_A",
        caller_workflow_id="workflow_A_001",
        caller_task_id="task_A_001",
        callee_device_id="device_B",
        target_agent_id="vision_agent_B",
        delegated_context=DelegatedContext(task_summary="run detection"),
    )

    response = asyncio.run(
        service.create_delegated_bundle(
            delegation,
            workflow_id="remote_workflow_B_001",
            agent_instance_id="inst_B_001",
        )
    )
    bundle_path = Path(asyncio.run(service.get_bundle_path(response.bundle_id)))
    output_dir = bundle_path / "output"

    caller_entry = WritebackEntry(
        memory_type="remote_collaboration_experience",
        target_owner="caller",
        content="B completed detection; reuse these constraints next time.",
        agent_id="vision_agent_B",
        caller_device_id="device_A",
        caller_workflow_id="workflow_A_001",
    )
    local_entry = WritebackEntry(
        memory_type="local_agent_audit",
        target_owner="local_agent",
        content="vision_agent_B ran successfully for a remote caller.",
        agent_id="vision_agent_B",
        caller_device_id="device_A",
        caller_workflow_id="workflow_A_001",
    )
    (output_dir / "writeback_to_caller.jsonl").write_text(
        json.dumps(caller_entry.model_dump(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "writeback_to_local.jsonl").write_text(
        json.dumps(local_entry.model_dump(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    assert asyncio.run(service.commit_bundle(response.bundle_id)) is True

    callee_memories = list(
        (tmp_path / "store" / "agent_memory" / "vision_agent_B").glob("*.json")
    )
    assert len(callee_memories) == 1
    assert json.loads(callee_memories[0].read_text(encoding="utf-8"))[
        "memory_type"
    ] == "local_agent_audit"

    caller_outbox = list(
        (
            tmp_path
            / "store"
            / "caller_writeback_outbox"
            / "device_A"
            / "workflow_A_001"
        ).glob("*.json")
    )
    assert len(caller_outbox) == 1
    caller_record = json.loads(caller_outbox[0].read_text(encoding="utf-8"))
    assert caller_record["memory_type"] == "remote_collaboration_experience"
    assert caller_record["delivery_state"] == "pending_forward_to_caller"
