"""
资源访问接口抽象层 (INTF - Interface for Resources)

运行层组件，为 Agent 实例提供对底层资源的统一访问接口：
- 计算资源接口（ComputeResourceInterface）
- 通信资源接口（CommunicationInterface）
- 传感器接口（SensorInterface）
- 执行器接口（ActuatorInterface）

当前版本：骨架/抽象层定义，具体实现占位。
生产环境：在子类中对接真实硬件驱动、云服务 SDK、ROS 等。

对应接口文档：智能体运行层接口流程 §1 传感器/执行器访问接口（INTF）
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


# ======================================================================
# 抽象基类
# ======================================================================

class ResourceInterface(ABC):
    """资源访问接口抽象基类"""

    @property
    @abstractmethod
    def interface_type(self) -> str:
        """接口类型标识（如 "compute", "communication", "sensor", "actuator"）"""
        ...

    @abstractmethod
    async def access(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        访问资源

        Args:
            request: 访问请求，结构由具体接口定义

        Returns:
            资源访问结果
        """
        ...

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """获取接口当前状态（可用/不可用/资源量等）"""
        ...


# ======================================================================
# 计算资源接口
# ======================================================================

class ComputeResourceInterface(ResourceInterface):
    """
    计算资源接口

    供业务 Agent 访问 CPU/GPU 计算资源。
    当前为 Mock 实现，返回模拟结果。
    """

    @property
    def interface_type(self) -> str:
        return "compute"

    async def access(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            request: {"operation": "execute_code"|"run_model", "payload": {...}}

        Returns:
            {"status": "ok", "result": {...}}
        """
        operation = request.get("operation", "unknown")
        logger.debug(f"[INTF/compute] 访问计算资源: operation={operation}")
        # ⚠️ Mock 实现
        return {"status": "ok", "result": f"计算完成: {operation}", "resource_type": "compute"}

    def get_status(self) -> Dict[str, Any]:
        return {"interface_type": "compute", "available": True, "description": "Mock 计算资源"}


# ======================================================================
# 通信资源接口
# ======================================================================

class CommunicationInterface(ResourceInterface):
    """
    通信/网络资源接口

    供资源 Agent 进行网络调度、带宽管理等。
    当前为 Mock 实现。
    """

    @property
    def interface_type(self) -> str:
        return "communication"

    async def access(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            request: {"operation": "get_bandwidth"|"schedule_task", "target": str}
        """
        operation = request.get("operation", "unknown")
        target = request.get("target", "unknown")
        logger.debug(f"[INTF/communication] 访问通信资源: op={operation}, target={target}")
        # ⚠️ Mock 实现
        return {
            "status": "ok",
            "result": {"bandwidth_mbps": 100, "latency_ms": 10},
            "resource_type": "communication",
        }

    def get_status(self) -> Dict[str, Any]:
        return {"interface_type": "communication", "available": True, "description": "Mock 通信资源"}


# ======================================================================
# 传感器接口
# ======================================================================

class SensorInterface(ResourceInterface):
    """
    传感器访问接口

    供业务 Agent 读取传感器数据（摄像头、激光雷达、GPS 等）。
    当前为 Mock 实现。
    """

    @property
    def interface_type(self) -> str:
        return "sensor"

    async def access(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            request: {"sensor_id": str, "data_type": "image"|"lidar"|"gps"|...}
        """
        sensor_id = request.get("sensor_id", "unknown")
        data_type = request.get("data_type", "unknown")
        logger.debug(f"[INTF/sensor] 读取传感器: sensor_id={sensor_id}, type={data_type}")
        # ⚠️ Mock 实现
        return {
            "status": "ok",
            "sensor_id": sensor_id,
            "data_type": data_type,
            "data": {"value": 0.0, "unit": "mock", "timestamp": "2026-03-06T00:00:00Z"},
            "resource_type": "sensor",
        }

    def get_status(self) -> Dict[str, Any]:
        return {"interface_type": "sensor", "available": True, "description": "Mock 传感器"}


# ======================================================================
# 执行器接口
# ======================================================================

class ActuatorInterface(ResourceInterface):
    """
    执行器访问接口

    供业务 Agent 发送执行命令（电机控制、信号灯、机械臂等）。
    当前为 Mock 实现。
    """

    @property
    def interface_type(self) -> str:
        return "actuator"

    async def access(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            request: {"actuator_id": str, "command": str, "params": {...}}
        """
        actuator_id = request.get("actuator_id", "unknown")
        command = request.get("command", "unknown")
        logger.debug(f"[INTF/actuator] 执行器命令: actuator_id={actuator_id}, cmd={command}")
        # ⚠️ Mock 实现
        return {
            "status": "ok",
            "actuator_id": actuator_id,
            "command": command,
            "execution_result": "confirmed",
        }

    def get_status(self) -> Dict[str, Any]:
        return {"interface_type": "actuator", "available": True, "description": "Mock 执行器"}


# ======================================================================
# 资源接口注册表
# ======================================================================

class ResourceInterfaceRegistry:
    """
    资源接口注册表

    统一管理所有 ResourceInterface 实例，
    提供按类型注册和获取接口的能力。
    """

    def __init__(self):
        self._interfaces: Dict[str, ResourceInterface] = {}
        # 注册默认 Mock 接口
        self._register_defaults()
        logger.info(
            f"ResourceInterfaceRegistry 初始化完成，"
            f"已注册接口类型: {list(self._interfaces.keys())}"
        )

    def _register_defaults(self):
        defaults: List[ResourceInterface] = [
            ComputeResourceInterface(),
            CommunicationInterface(),
            SensorInterface(),
            ActuatorInterface(),
        ]
        for intf in defaults:
            self.register(intf)

    def register(self, interface: ResourceInterface):
        """注册资源接口"""
        self._interfaces[interface.interface_type] = interface
        logger.debug(f"[INTF Registry] 注册接口: {interface.interface_type}")

    def get(self, interface_type: str) -> Optional[ResourceInterface]:
        """按类型获取接口实例"""
        return self._interfaces.get(interface_type)

    async def access(
        self,
        interface_type: str,
        request: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        通过类型访问资源

        Args:
            interface_type: 接口类型（"compute"/"communication"/"sensor"/"actuator"）
            request:         访问请求

        Returns:
            访问结果，若类型不存在返回错误字典
        """
        intf = self._interfaces.get(interface_type)
        if not intf:
            logger.warning(f"[INTF Registry] 未知接口类型: {interface_type}")
            return {"status": "error", "message": f"未知接口类型: {interface_type}"}
        return await intf.access(request)

    def list_interfaces(self) -> List[Dict[str, Any]]:
        """列出所有接口的状态"""
        return [intf.get_status() for intf in self._interfaces.values()]


# ======================================================================
# 单例访问
# ======================================================================
_registry_instance: Optional[ResourceInterfaceRegistry] = None


def get_resource_interface_registry() -> ResourceInterfaceRegistry:
    """获取全局 ResourceInterfaceRegistry 单例"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ResourceInterfaceRegistry()
    return _registry_instance
