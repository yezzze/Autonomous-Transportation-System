"""
智能体运行层 (Runtime Layer)

对应系统架构图中的"智能体运行层"，包含：
- ALCM (AgentLifecycleManager): 生命周期管理
- INTF (ResourceInterfaceRegistry): 传感器/执行器访问接口
- QoS  (QoSMonitor):              QoS 监控与保障

注：SBOX（执行沙箱）和 COMM（跨智能体通信中间件）由现有代码覆盖：
- SBOX: src/tools/（bash_tool, python_repl 等 L1 工具）
- COMM: src/protocols/a2a_protocol.py + src/service/a2a_client.py
"""

from src.runtime.models import AgentInstance, ResourceConfig, QoSMetrics
from src.runtime.lifecycle_manager import AgentLifecycleManager, get_lifecycle_manager
from src.runtime.resource_interface import (
    ResourceInterface,
    ResourceInterfaceRegistry,
    ComputeResourceInterface,
    CommunicationInterface,
    SensorInterface,
    ActuatorInterface,
    get_resource_interface_registry,
)
from src.runtime.qos_monitor import QoSMonitor, get_qos_monitor

__all__ = [
    # 数据模型
    "AgentInstance",
    "ResourceConfig",
    "QoSMetrics",
    # ALCM
    "AgentLifecycleManager",
    "get_lifecycle_manager",
    # INTF
    "ResourceInterface",
    "ResourceInterfaceRegistry",
    "ComputeResourceInterface",
    "CommunicationInterface",
    "SensorInterface",
    "ActuatorInterface",
    "get_resource_interface_registry",
    # QoS
    "QoSMonitor",
    "get_qos_monitor",
]
