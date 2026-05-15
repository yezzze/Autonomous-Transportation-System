import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app.agent_warehouse import AgentWarehouse
from src.app.models import AgentImage
from src.app.app_logic_engine import AppLogicEngine
from src.app.app_manager import AppManager
from src.runtime.lifecycle_manager import AgentLifecycleManager


WAREHOUSE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "agent_warehouse.json",
)


def test_vehicle_k8s_images_are_registered_in_warehouse():
    warehouse = AgentWarehouse(warehouse_file=WAREHOUSE_FILE)

    perception = warehouse.get_image("perception2intermediatefeature-agent:0.1.2")
    fusion = warehouse.get_image("cooperativefeaturefusiondetectionviz-agent:0.1.2")

    assert perception is not None
    assert perception.capability == "perception2intermediatefeature"
    assert perception.metadata["k8s"] == {
        "cpu_cores": 2.0,
        "memory_mb": 4096,
        "gpu_count": 1,
    }

    assert fusion is not None
    assert fusion.capability == "cooperativefeaturefusiondetectionviz"
    assert fusion.metadata["k8s"] == {
        "cpu_cores": 2.0,
        "memory_mb": 8192,
        "gpu_count": 1,
    }


def test_app_manager_does_not_create_builtin_apps_from_empty_store(monkeypatch, tmp_path):
    import src.app.app_manager as app_manager_module

    monkeypatch.setattr(
        app_manager_module,
        "APPS_STORE_PATH",
        str(tmp_path / "apps_store.json"),
    )

    manager = AppManager()

    assert manager.list_apps() == []
    assert not (tmp_path / "apps_store.json").exists()


def test_builtin_app_keeps_manual_image_binding_without_local_lookup(monkeypatch, tmp_path):
    import src.app.app_manager as app_manager_module

    store_path = tmp_path / "apps_store.json"
    monkeypatch.setattr(app_manager_module, "APPS_STORE_PATH", str(store_path))

    store_path.write_text(
        json.dumps(
            {
                "app_builtin_agent_b": {
                    "app_id": "app_builtin_agent_b",
                    "name": "Agent B",
                    "status": "idle",
                    "image_ids": ["manual-agent-b:v9"],
                    "workflow_handle": None,
                    "app_interface_url": None,
                    "error_message": None,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "guidance_file": {
                        "app_id": "app_builtin_agent_b",
                        "task_description": "启动 Agent B，作为 NATS worker 转发任务给 Agent C 并回传结果。",
                        "agents_required": ["agent-b"],
                        "orchestration_mode": "adaptive",
                        "constraints": {"timeout_seconds": 120},
                        "metadata": {"deploy_only": True},
                        "skills_content": None,
                        "created_at": "2026-01-01T00:00:00",
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    manager = AppManager()
    app = manager.get_app("app_builtin_agent_b")

    assert app is not None
    assert app.image_ids == ["manual-agent-b:v9"]


def test_builtin_agent_a_is_migrated_without_creating_new_builtin_apps(monkeypatch, tmp_path):
    import src.app.app_manager as app_manager_module

    store_path = tmp_path / "apps_store.json"
    monkeypatch.setattr(app_manager_module, "APPS_STORE_PATH", str(store_path))

    store_path.write_text(
        json.dumps(
            {
                "app_builtin_agent_a": {
                    "app_id": "app_builtin_agent_a",
                    "name": "Agent A",
                    "status": "idle",
                    "image_ids": ["agent-a-grpc:v2"],
                    "workflow_handle": None,
                    "app_interface_url": None,
                    "error_message": None,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "guidance_file": {
                        "app_id": "app_builtin_agent_a",
                        "task_description": "启动 Agent A，作为 agent-a/agent-b/agent-c 的入口。",
                        "agents_required": ["agent-a"],
                        "orchestration_mode": "adaptive",
                        "constraints": {"timeout_seconds": 120},
                        "metadata": {"deploy_only": True},
                        "skills_content": None,
                        "created_at": "2026-01-01T00:00:00",
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    manager = AppManager()

    assert manager.get_app("app_builtin_agent_a") is None
    migrated = manager.get_app("app_builtin_agent_grpc")
    assert migrated is not None
    assert migrated.image_ids == ["agent-grpc:v1"]
    assert migrated.guidance_file.agents_required == ["agent-grpc"]
    assert "agent_gRPC/agent-b/agent-c" in migrated.guidance_file.task_description


def test_app_logic_uses_image_k8s_resource_defaults():
    warehouse = AgentWarehouse(warehouse_file=WAREHOUSE_FILE)
    engine = AppLogicEngine()

    image = warehouse.get_image("cooperativefeaturefusiondetectionviz-agent:0.1.2")
    config = engine._resource_config_from_image(image)

    assert config.cpu_cores == 2.0
    assert config.memory_mb == 8192
    assert config.gpu_count == 1
    assert config.node_id == "localhost"


def test_lifecycle_manager_preserves_failed_k8s_deploy_status(monkeypatch):
    class FailedScheduler:
        def deploy_agent(self, **kwargs):
            class Record:
                status = "failed"

            return Record()

    import src.service.agent_scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "get_agent_scheduler",
        lambda: FailedScheduler(),
    )

    manager = AgentLifecycleManager()
    instance = manager.deploy_agent(
        agent_id="cooperativefeaturefusiondetectionviz_agent",
        image_id="cooperativefeaturefusiondetectionviz-agent:0.1.2",
    )

    assert instance.status == "error"


def test_warehouse_keeps_manually_configured_image_when_not_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DEPLOY_BACKEND", "kubernetes")
    monkeypatch.delenv("AGENT_WAREHOUSE_PRUNE_UNAVAILABLE", raising=False)
    monkeypatch.setattr(AgentWarehouse, "_local_image_ids", staticmethod(lambda: {"nats:2.10"}))

    warehouse_file = tmp_path / "warehouse.json"
    warehouse_file.write_text(
        json.dumps(
            {
                "images": [
                    AgentImage(
                        image_id="manual-only-agent:1.0",
                        name="Manual Only Agent",
                        version="1.0",
                        capability="manual-only",
                        description="manually configured",
                    ).to_dict()
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    warehouse = AgentWarehouse(warehouse_file=str(warehouse_file))

    assert warehouse.get_image("manual-only-agent:1.0") is not None


def test_warehouse_init_does_not_import_apps_store_or_autosync(monkeypatch, tmp_path):
    warehouse_file = tmp_path / "warehouse.json"
    warehouse_file.write_text(json.dumps({"images": []}, ensure_ascii=False), encoding="utf-8")

    apps_store = tmp_path / "apps_store.json"
    apps_store.write_text(
        json.dumps(
            {
                "app_manual": {
                    "app_id": "app_manual",
                    "name": "Manual App",
                    "image_ids": ["should-not-be-imported:1.0"],
                    "guidance_file": {"agents_required": ["manual-agent"]},
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("APPS_STORE_PATH", str(apps_store))

    def fail_refresh(self):
        raise AssertionError("refresh_from_kubernetes should not be called during init")

    monkeypatch.setattr(AgentWarehouse, "refresh_from_kubernetes", fail_refresh)

    warehouse = AgentWarehouse(warehouse_file=str(warehouse_file))

    assert warehouse.list_images(refresh=False) == []
    stored = json.loads(warehouse_file.read_text(encoding="utf-8"))
    assert stored == {"images": []}


def test_warehouse_prunes_unavailable_image_only_when_explicitly_called(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DEPLOY_BACKEND", "kubernetes")
    monkeypatch.setenv("AGENT_WAREHOUSE_PRUNE_UNAVAILABLE", "1")
    monkeypatch.setattr(AgentWarehouse, "_local_image_ids", staticmethod(lambda: {"nats:2.10"}))

    warehouse_file = tmp_path / "warehouse.json"
    warehouse_file.write_text(
        json.dumps(
            {
                "images": [
                    AgentImage(
                        image_id="manual-only-agent:1.0",
                        name="Manual Only Agent",
                        version="1.0",
                        capability="manual-only",
                        description="manually configured",
                    ).to_dict()
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    warehouse = AgentWarehouse(warehouse_file=str(warehouse_file))
    warehouse._prune_unavailable_images()

    assert warehouse.get_image("manual-only-agent:1.0") is None
