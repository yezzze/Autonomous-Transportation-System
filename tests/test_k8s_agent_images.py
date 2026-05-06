import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app.agent_warehouse import AgentWarehouse
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

    perception = warehouse.get_image("perception2intermediatefeature-agent:0.1.1")
    fusion = warehouse.get_image("cooperativefeaturefusiondetectionviz-agent:0.1.1")

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


def test_builtin_apps_bind_vehicle_capabilities_to_k8s_images(monkeypatch, tmp_path):
    import src.app.app_manager as app_manager_module

    monkeypatch.setattr(
        app_manager_module,
        "APPS_STORE_PATH",
        str(tmp_path / "apps_store.json"),
    )

    manager = AppManager()

    perception_app = manager.get_app("app_builtin_perception2intermediatefeature")
    fusion_app = manager.get_app("app_builtin_cooperativefeaturefusiondetectionviz")

    assert perception_app is not None
    assert perception_app.guidance_file.agents_required == [
        "perception2intermediatefeature"
    ]
    assert perception_app.image_ids == ["perception2intermediatefeature-agent:0.1.1"]

    assert fusion_app is not None
    assert fusion_app.guidance_file.agents_required == [
        "cooperativefeaturefusiondetectionviz"
    ]
    assert fusion_app.image_ids == ["cooperativefeaturefusiondetectionviz-agent:0.1.1"]

    stored = json.loads((tmp_path / "apps_store.json").read_text(encoding="utf-8"))
    assert "app_builtin_perception2intermediatefeature" in stored
    assert "app_builtin_cooperativefeaturefusiondetectionviz" in stored


def test_app_logic_uses_image_k8s_resource_defaults():
    warehouse = AgentWarehouse(warehouse_file=WAREHOUSE_FILE)
    engine = AppLogicEngine()

    image = warehouse.get_image("cooperativefeaturefusiondetectionviz-agent:0.1.1")
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
        image_id="cooperativefeaturefusiondetectionviz-agent:0.1.1",
    )

    assert instance.status == "error"
