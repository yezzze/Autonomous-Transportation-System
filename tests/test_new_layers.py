"""验证所有新增模块"""
# 编排层
from src.service.agent_scheduler import get_agent_scheduler
from src.service.resource_registry import get_resource_registry
from src.graph.distributed_types import DistributedState
from src.graph.distributed_nodes import dispatch_subtask_to_remote_aoe, identify_cross_host_tasks

s = get_agent_scheduler()
r = get_resource_registry()
assert s.get_running_agents() == []
assert r.get_summary()["total_nodes"] == 3
assert "cross_host_sessions" in DistributedState.__annotations__
print("✅ 编排层 (ASD/RRDC/跨主体) OK")

# 应用层
from src.app.app_manager import get_app_manager
from src.app.models import GuidanceFile
from src.app.display import get_all_app_list

mgr = get_app_manager()
gf = GuidanceFile(
    app_id="app_test001",
    task_description="搜索最新的人工智能进展",
    orchestration_mode="adaptive",
    agents_required=["search"],
    constraints={"max_rounds": 5, "timeout_seconds": 60},
)
app = mgr.install(name="测试应用", guidance_file=gf)
assert app.status == "idle"
assert len(get_all_app_list()) == 1
mgr.uninstall(app.app_id)
assert mgr.list_apps() == []
print("✅ 应用层 (APPM/AW/ALRE/DISP) OK")

# 运行层
from src.runtime.lifecycle_manager import get_lifecycle_manager
from src.runtime.models import ResourceConfig
from src.runtime.qos_monitor import get_qos_monitor
from src.runtime.resource_interface import get_resource_interface_registry

lcm = get_lifecycle_manager()
inst = lcm.deploy_agent("search_agent_001", "search:v1", ResourceConfig(cpu_cores=1.0, memory_mb=256, node_id="node_localhost"))
assert inst.status == "running"
lcm.subscribe(inst.instance_id, "wf_001")
lcm.subscribe(inst.instance_id, "wf_002")
assert lcm.get_instance(inst.instance_id).ref_count == 2
lcm.unsubscribe(inst.instance_id, "wf_001")
lcm.unsubscribe(inst.instance_id, "wf_002")  # 引用归零 → 自动关闭
assert lcm.get_instance(inst.instance_id).status == "stopped"
print("✅ 运行层 ALCM 引用计数 OK")

qos = get_qos_monitor()
qos.record_call("search_agent_001", 120.5, True)
qos.record_call("search_agent_001", 6500.0, False)
m = qos.get_metrics("search_agent_001")
assert m.total_calls == 2
assert not qos.check_threshold("search_agent_001")  # avg=3310ms < 5000ms，调用<5次 → 无告警
# 触发平均延迟告警
qos.record_call("slow_agent", 5001.0, True)
assert qos.check_threshold("slow_agent")  # avg=5001ms > 5000ms → 告警
# 触发成功率告警（5次以上，成功率 < 80%）
for _ in range(5):
    qos.record_call("bad_agent", 100.0, False)
assert qos.check_threshold("bad_agent")  # success_rate=0% < 80% → 告警
print("✅ 运行层 QoS OK")

reg = get_resource_interface_registry()
assert len(reg.list_interfaces()) == 4
print("✅ 运行层 INTF OK")

print("\n🎉 所有模块验证通过！")
