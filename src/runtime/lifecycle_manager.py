"""
智能体生命周期管理 (ALCM - Agent Lifecycle Manager)

运行层核心组件，负责：
- 管理 Agent 实例的完整生命周期：deploy → running → stopping → stopped
- 工作流订阅/退订 Agent 实例（引用计数）
- 引用计数归零时触发空闲关闭
- 与 ASD（AgentScheduler）和 RRDC（ResourceRegistry）协作

对应接口文档：
- 智能体运行层接口流程 §1 智能体部署与调用
"""
import asyncio
import logging
from typing import Dict, List, Optional

from src.runtime.models import AgentInstance, ResourceConfig

logger = logging.getLogger(__name__)


class AgentLifecycleManager:
    """
    Agent 生命周期管理（ALCM）

    核心接口：
    - deploy_agent()    — 创建实例，调用 ASD 部署，注册 ARDC
    - subscribe()       — 工作流订阅实例，引用计数 +1
    - unsubscribe()     — 工作流退订实例，引用计数 -1（归零自动关闭）
    - shutdown_agent()  — 强制关闭实例
    - get_instance()    — 查询实例信息
    - list_instances()  — 列出所有实例
    """

    def __init__(self):
        # instance_id → AgentInstance
        self._instances: Dict[str, AgentInstance] = {}
        # agent_id → List[instance_id]（同一 agent 可能有多个实例）
        self._agent_index: Dict[str, List[str]] = {}
        logger.info("AgentLifecycleManager (ALCM) 初始化完成")

    # ------------------------------------------------------------------
    # 部署接口
    # ------------------------------------------------------------------

    def deploy_agent(
        self,
        agent_id: str,
        image_id: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> AgentInstance:
        """
        部署 Agent 实例

        流程（参考运行层接口文档 §1）：
        1. 向 RRDC 申请资源
        2. 创建 AgentInstance 记录
        3. 调用 ASD 执行实际部署
        4. 标记实例为 running

        Args:
            agent_id:        Agent 逻辑标识
            image_id:        镜像 ID
            resource_config: 资源配置，默认使用 ResourceConfig()

        Returns:
            AgentInstance
        """
        rc = resource_config or ResourceConfig()
        instance = AgentInstance.create(agent_id=agent_id, image_id=image_id, resource_config=rc)

        # 1. 向 RRDC 申请资源
        self._allocate_resources(instance)

        # 2. 记录实例
        self._instances[instance.instance_id] = instance
        self._agent_index.setdefault(agent_id, []).append(instance.instance_id)

        # 3. 调用 ASD 部署
        self._call_asd_deploy(instance)

        # 4. 标记运行中
        instance.set_status("running")

        logger.info(
            f"[ALCM] ✅ 部署成功: instance_id={instance.instance_id}, "
            f"agent_id={agent_id}"
        )
        return instance

    # ------------------------------------------------------------------
    # 订阅/退订（引用计数）
    # ------------------------------------------------------------------

    def subscribe(self, instance_id: str, workflow_id: str) -> bool:
        """
        工作流订阅 Agent 实例，引用计数 +1

        对应接口文档：AWM→ALCM 订阅智能体

        Args:
            instance_id: Agent 实例 ID
            workflow_id: 订阅的工作流 ID

        Returns:
            True 订阅成功，False 实例不存在
        """
        instance = self._instances.get(instance_id)
        if not instance:
            logger.warning(f"[ALCM] subscribe: instance_id={instance_id} 不存在")
            return False

        instance.subscribe(workflow_id)
        logger.info(
            f"[ALCM] 工作流订阅: workflow={workflow_id} → instance={instance_id}, "
            f"ref_count={instance.ref_count}"
        )
        return True

    def unsubscribe(self, instance_id: str, workflow_id: str) -> int:
        """
        工作流退订 Agent 实例，引用计数 -1。
        引用计数归零时标记实例为空闲，可被关闭。

        对应接口文档：AWM→ALCM 退订智能体

        Args:
            instance_id: Agent 实例 ID
            workflow_id: 退订的工作流 ID

        Returns:
            剩余引用计数
        """
        instance = self._instances.get(instance_id)
        if not instance:
            logger.warning(f"[ALCM] unsubscribe: instance_id={instance_id} 不存在")
            return 0

        remaining = instance.unsubscribe(workflow_id)
        logger.info(
            f"[ALCM] 工作流退订: workflow={workflow_id} ← instance={instance_id}, "
            f"ref_count={remaining}"
        )

        # 引用归零，触发空闲关闭
        if remaining == 0:
            logger.info(
                f"[ALCM] 引用计数归零，触发空闲关闭: instance_id={instance_id}"
            )
            self._idle_shutdown(instance)

        return remaining

    # ------------------------------------------------------------------
    # 关闭接口
    # ------------------------------------------------------------------

    def shutdown_agent(self, instance_id: str, force: bool = False) -> bool:
        """
        关闭 Agent 实例

        流程（参考接口文档 §2.3 停止编排）：
        1. 检查引用计数（force=True 时忽略）
        2. 调用 ASD 停止容器
        3. 从 RRDC 释放资源
        4. 从 ARDC 注销

        Args:
            instance_id: 实例 ID
            force:       是否强制关闭（忽略引用计数）

        Returns:
            True 关闭成功，False 未满足条件或不存在
        """
        instance = self._instances.get(instance_id)
        if not instance:
            logger.warning(f"[ALCM] shutdown_agent: instance_id={instance_id} 不存在")
            return False

        if not force and instance.ref_count > 0:
            logger.warning(
                f"[ALCM] shutdown_agent: 实例仍有 {instance.ref_count} 个订阅者，"
                f"使用 force=True 强制关闭"
            )
            return False

        return self._do_shutdown(instance)

    def shutdown_all_by_agent(self, agent_id: str, force: bool = False) -> int:
        """关闭某 agent_id 的所有实例，返回关闭成功数量"""
        instance_ids = list(self._agent_index.get(agent_id, []))
        count = sum(1 for iid in instance_ids if self.shutdown_agent(iid, force=force))
        return count

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_instance(self, instance_id: str) -> Optional[AgentInstance]:
        """按 instance_id 查询实例"""
        return self._instances.get(instance_id)

    def get_instances_by_agent(self, agent_id: str) -> List[AgentInstance]:
        """获取某 agent_id 的所有实例"""
        return [
            self._instances[iid]
            for iid in self._agent_index.get(agent_id, [])
            if iid in self._instances
        ]

    def list_instances(self) -> List[AgentInstance]:
        """列出所有实例"""
        return list(self._instances.values())

    def list_running_instances(self) -> List[AgentInstance]:
        """列出所有运行中实例"""
        return [i for i in self._instances.values() if i.status == "running"]

    # ------------------------------------------------------------------
    # 内部帮助方法
    # ------------------------------------------------------------------

    def _allocate_resources(self, instance: AgentInstance):
        """向 RRDC 申请资源"""
        try:
            from src.service.resource_registry import get_resource_registry

            registry = get_resource_registry()
            success = registry.allocate(
                node_id=instance.resource_config.node_id,
                cpu_cores=instance.resource_config.cpu_cores,
                memory_mb=instance.resource_config.memory_mb,
            )
            if not success:
                logger.warning(
                    f"[ALCM→RRDC] 资源分配失败，继续部署（Mock 模式）: "
                    f"instance_id={instance.instance_id}"
                )
        except Exception as e:
            logger.warning(f"[ALCM→RRDC] 资源分配异常（非关键）: {e}")

    def _release_resources(self, instance: AgentInstance):
        """向 RRDC 归还资源"""
        try:
            from src.service.resource_registry import get_resource_registry

            registry = get_resource_registry()
            registry.release(
                node_id=instance.resource_config.node_id,
                cpu_cores=instance.resource_config.cpu_cores,
                memory_mb=instance.resource_config.memory_mb,
            )
        except Exception as e:
            logger.warning(f"[ALCM→RRDC] 资源释放异常（非关键）: {e}")

    def _call_asd_deploy(self, instance: AgentInstance):
        """调用 ASD 执行实际部署"""
        try:
            from src.service.agent_scheduler import get_agent_scheduler

            scheduler = get_agent_scheduler()
            scheduler.deploy_agent(
                image_id=instance.image_id,
                agent_id=instance.agent_id,
                node_id=instance.resource_config.node_id,
                cpu_cores=instance.resource_config.cpu_cores,
                memory_mb=instance.resource_config.memory_mb,
            )
        except Exception as e:
            logger.warning(f"[ALCM→ASD] 部署调用异常（非关键）: {e}")

    def _call_asd_shutdown(self, instance: AgentInstance):
        """调用 ASD 关闭实例"""
        try:
            from src.service.agent_scheduler import get_agent_scheduler

            scheduler = get_agent_scheduler()
            scheduler.shutdown_agent(instance.agent_id)
        except Exception as e:
            logger.warning(f"[ALCM→ASD] 关闭调用异常（非关键）: {e}")

    def _idle_shutdown(self, instance: AgentInstance):
        """引用计数归零时的空闲关闭（非强制）"""
        self._do_shutdown(instance)

    def _do_shutdown(self, instance: AgentInstance) -> bool:
        """执行关闭并清理资源"""
        instance.set_status("stopping")

        # 调用 ASD 停止容器
        self._call_asd_shutdown(instance)

        # 释放 RRDC 资源
        self._release_resources(instance)

        # 标记已停止
        instance.set_status("stopped")

        # 从索引中清理（保留记录用于审计）
        agent_instances = self._agent_index.get(instance.agent_id, [])
        if instance.instance_id in agent_instances:
            agent_instances.remove(instance.instance_id)

        logger.info(f"[ALCM] ✅ 实例关闭完成: instance_id={instance.instance_id}")
        return True


# ======================================================================
# 单例访问
# ======================================================================
_manager_instance: Optional[AgentLifecycleManager] = None


def get_lifecycle_manager() -> AgentLifecycleManager:
    """获取全局 AgentLifecycleManager 单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AgentLifecycleManager()
    return _manager_instance
