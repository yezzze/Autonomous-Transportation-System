"""
应用逻辑执行引擎 (ALRE - Application Logic Execution Engine)

应用管理层组件，负责：
- 存储应用对应的编排指导文件（GuidanceFile）
- 将指导文件传递给编排层，启动工作流
- 停止正在运行的工作流

对应接口文档：
- §1 安装/卸载应用（存储/删除指导文件部分）
- §2 启动应用（ALRE → ORCH 部分）
- §3 停止应用（ALRE → ORCH 部分）
"""
import asyncio
import copy
import logging
import uuid
from typing import Any, Dict, List, Optional

from src.app.models import GuidanceFile
from src.runtime.models import ResourceConfig

logger = logging.getLogger(__name__)


class AppLogicEngine:
    """
    应用逻辑执行引擎（ALRE）

    职责：
    1. install_app_logic()   — 存储编排指导文件
    2. uninstall_app_logic() — 删除编排指导文件
    3. start_app()           — 解析指导文件 → 启动编排层工作流
    4. stop_app()            — 通知编排层停止工作流
    5. get_guidance()        — 查询指导文件

    编排层接口：
    - 调用 src.distributed_workflow.run_distributed_workflow()
    - 以 asyncio.Task 的形式后台运行工作流
    """

    def __init__(self):
        # app_id → GuidanceFile
        self._guidance_files: Dict[str, GuidanceFile] = {}
        # app_id → asyncio.Task（正在运行的工作流任务）
        self._running_tasks: Dict[str, asyncio.Task] = {}
        # app_id → workflow_handle（标识符）
        self._workflow_handles: Dict[str, str] = {}
        # app_id → [instance_id, ...]（ALCM 部署的 Agent 实例）
        self._instance_ids: Dict[str, List[str]] = {}
        self._execution_plans: Dict[str, List[Dict[str, Any]]] = {}
        self._frozen_plan_signatures: Dict[str, List[List[str]]] = {}
        self._cross_host_sessions: Dict[str, Dict[str, Any]] = {}
        # QoS 告警回调是否已注册（每个进程只注册一次）
        self._qos_callback_registered = False
        logger.info("AppLogicEngine (ALRE) 初始化完成")

    # ------------------------------------------------------------------
    # 指导文件管理
    # ------------------------------------------------------------------

    def install_app_logic(self, guidance_file: GuidanceFile) -> bool:
        """
        安装应用执行逻辑（存储指导文件）

        Args:
            guidance_file: 编排指导文件

        Returns:
            True 表示首次安装，False 表示更新已有
        """
        is_new = guidance_file.app_id not in self._guidance_files
        self._guidance_files[guidance_file.app_id] = guidance_file
        action = "安装" if is_new else "更新"
        logger.info(
            f"[ALRE] {action}应用逻辑: app_id={guidance_file.app_id}, "
            f"mode={guidance_file.orchestration_mode}"
        )
        return is_new

    def uninstall_app_logic(self, app_id: str) -> bool:
        """
        卸载应用执行逻辑（删除指导文件）

        注意：若工作流正在运行，需先调用 stop_app()

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功，False 表示未找到
        """
        if app_id not in self._guidance_files:
            logger.warning(f"[ALRE] uninstall_app_logic: app_id={app_id} 未找到")
            return False
        del self._guidance_files[app_id]
        logger.info(f"[ALRE] 卸载应用逻辑: app_id={app_id}")
        return True

    def get_guidance(self, app_id: str) -> Optional[GuidanceFile]:
        """获取指导文件"""
        return self._guidance_files.get(app_id)

    # ------------------------------------------------------------------
    # 工作流生命周期
    # ------------------------------------------------------------------

    async def start_app(
        self,
        app_id: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> Optional[str]:
        """
        启动应用

        流程（参考接口文档 §2）：
        1. 读取指导文件
        2. 调用编排层 run_distributed_workflow()
        3. 以 asyncio.Task 形式在后台运行
        4. 返回 workflow_handle

        Args:
            app_id: 应用 ID
            resource_config: 启动时为每个 Agent 实例申请的资源

        Returns:
            workflow_handle（str），失败返回 None
        """
        guidance = self._guidance_files.get(app_id)
        if not guidance:
            logger.error(f"[ALRE] start_app: app_id={app_id} 没有指导文件")
            return None

        if app_id in self._running_tasks and not self._running_tasks[app_id].done():
            logger.warning(f"[ALRE] start_app: app_id={app_id} 已在运行中")
            return self._workflow_handles.get(app_id)

        # 注册 QoS → ASD 告警回调（每个进程只注册一次）
        self._register_qos_callback()

        workflow_handle = f"wf_{app_id}_{uuid.uuid4().hex[:6]}"
        self._workflow_handles[app_id] = workflow_handle

        logger.info(
            f"[ALRE] 启动应用: app_id={app_id}, "
            f"workflow_handle={workflow_handle}, "
            f"mode={guidance.orchestration_mode}"
        )

        if guidance.metadata.get("deploy_only"):
            self._workflow_handles.pop(app_id, None)
            raise RuntimeError("DEPLOY_ONLY_UNSUPPORTED: 应用必须先生成 execution_plan")

        pipeline_topology = []
        if guidance.skills_content:
            from src.app.pipeline_parser import parse_pipeline
            pipeline_topology = parse_pipeline(guidance.skills_content) or []

        from src.distributed_workflow import generate_execution_plan
        from src.graph.distributed_nodes import (
            _cleanup_registered_remote_workflows,
            bind_and_finalize_execution_plan,
            register_cross_host_workflows,
        )

        timeout = guidance.constraints.get("timeout_seconds", guidance.constraints.get("max_timeout", 120))
        instance_ids: List[str] = []
        remote_sessions: Dict[str, Dict[str, Any]] = {}
        try:
            planning = await generate_execution_plan(
                guidance.task_description,
                skills_content=guidance.skills_content or "",
                pipeline_topology=pipeline_topology,
                timeout_seconds=timeout,
            )
            local_bindings, instance_ids = self._deploy_plan_local_agents(
                planning["execution_plan"],
                planning["agent_registry_cache"],
                workflow_handle,
                resource_config,
            )
            self._instance_ids[app_id] = instance_ids
            if planning["cross_host"]:
                remote_sessions = await register_cross_host_workflows(
                    planning["execution_plan"], planning["cross_host"], timeout
                )
            bound_plan, frozen_signature = await bind_and_finalize_execution_plan(
                planning["execution_plan"],
                {
                    "route_instances": local_bindings,
                    "timeout_seconds": timeout,
                    "session_timeout_seconds": timeout,
                },
                remote_sessions,
            )
            self._execution_plans[app_id] = bound_plan
            self._frozen_plan_signatures[app_id] = frozen_signature
            self._cross_host_sessions[app_id] = remote_sessions
        except Exception:
            if remote_sessions:
                await _cleanup_registered_remote_workflows(remote_sessions, timeout)
            self._unsubscribe_instances(app_id, workflow_handle)
            self._workflow_handles.pop(app_id, None)
            raise

        # 启动后台工作流任务
        task = asyncio.create_task(
            self._run_workflow(app_id, guidance, workflow_handle),
            name=f"workflow_{app_id}",
        )
        self._running_tasks[app_id] = task
        task.add_done_callback(
            lambda finished_task, _app_id=app_id, _handle=workflow_handle: self._on_workflow_done(
                _app_id,
                _handle,
                finished_task,
            )
        )
        logger.info(f"[ALRE] ✅ 工作流已启动: {workflow_handle}")
        return workflow_handle

    async def stop_app(self, app_id: str) -> bool:
        """
        停止应用

        流程（参考接口文档 §3 / §2.4）：
        1. fire-and-forget 通知所有活跃远端 AOE 会话取消（跨主体 §2.4）
        2. 取消本地 asyncio.Task
        3. 清理记录

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功停止，False 表示未找到运行中任务
        """
        task = self._running_tasks.get(app_id)
        if not task and app_id not in self._execution_plans:
            logger.warning(f"[ALRE] stop_app: app_id={app_id} 无运行中工作流")
            return False

        # §2.4：通知所有活跃远端 AOE 会话提前终止（fire-and-forget，不阻塞）
        try:
            from src.graph.distributed_nodes import get_active_remote_sessions
            remote_sessions = get_active_remote_sessions()
            if remote_sessions:
                logger.info(
                    f"[ALRE] 通知 {len(remote_sessions)} 个远端会话取消: {list(remote_sessions.keys())}"
                )
                for session_id, remote_url in remote_sessions.items():
                    asyncio.create_task(
                        _cancel_remote_session(remote_url, session_id)
                    )
        except Exception as e:
            logger.debug(f"[ALRE] 远端会话通知失败（非关键）: {e}")

        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        remote_sessions = self._cross_host_sessions.get(app_id, {})
        if remote_sessions:
            try:
                from src.graph.distributed_nodes import _cleanup_registered_remote_workflows
                await _cleanup_registered_remote_workflows(remote_sessions, 10)
            except Exception as exc:
                logger.warning("[ALRE] 清理应用远端子工作流失败: app_id=%s, error=%s", app_id, exc)

        # 退订 Agent 实例（对应接口文档 §3 引用计数自减）
        workflow_handle = self._workflow_handles.get(app_id)
        self._unsubscribe_instances(app_id, workflow_handle)

        self._running_tasks.pop(app_id, None)
        if app_id in self._workflow_handles:
            del self._workflow_handles[app_id]
        self._execution_plans.pop(app_id, None)
        self._frozen_plan_signatures.pop(app_id, None)
        self._cross_host_sessions.pop(app_id, None)

        logger.info(f"[ALRE] ✅ 停止应用: app_id={app_id}")
        return True

    def is_running(self, app_id: str) -> bool:
        """检查应用是否正在运行"""
        task = self._running_tasks.get(app_id)
        return task is not None and not task.done()

    def get_workflow_handle(self, app_id: str) -> Optional[str]:
        """获取运行中工作流的 handle"""
        return self._workflow_handles.get(app_id)

    def get_guidance(self, app_id: str) -> Optional[GuidanceFile]:
        """获取应用的编排指导文件"""
        return self._guidance_files.get(app_id)

    async def run_query(self, app_id: str, user_input: str) -> dict:
        """
        向已安装的应用发送一次查询，立即执行并返回结果。

        与 start_app() 不同，run_query() 是同步请求-响应模式：
        - 使用应用配置的 orchestration_mode / constraints
        - 以 user_input 覆盖 task_description
        - 等待工作流完成后返回结果

        Args:
            app_id: 应用 ID
            user_input: 用户输入的查询内容

        Returns:
            {"result": ..., "workflow_handle": ..., "status": "done"|"error"}
        """
        guidance = self._guidance_files.get(app_id)
        if not guidance:
            raise ValueError(f"应用 {app_id} 未安装")

        from src.distributed_workflow import run_distributed_workflow

        workflow_handle = f"wf_{app_id}_{uuid.uuid4().hex[:6]}"
        timeout = guidance.constraints.get("timeout_seconds", 120)

        logger.info(
            f"[ALRE] run_query: app_id={app_id}, "
            f"mode={guidance.orchestration_mode}, query={user_input[:60]}"
        )
        if guidance.skills_content:
            logger.info(
                f"[ALRE] Skills 指引将注入 Planner system_prompt: "
                f"app_id={app_id}, skills_len={len(guidance.skills_content)}"
            )

        plan = self._execution_plans.get(app_id)
        signature = self._frozen_plan_signatures.get(app_id)
        if not plan or not signature:
            raise RuntimeError("FROZEN_PLAN_MISSING: 应用尚未启动或没有冻结执行计划")
        query_plan = self._inject_query_into_plan(plan, user_input)

        try:
            result = await run_distributed_workflow(
                user_input=user_input,
                adaptive_mode=False,
                replanning_enabled=False,
                max_retries=guidance.constraints.get("max_retries", 3),
                timeout_seconds=timeout,
                skills_content=guidance.skills_content or "",
                route_instances=self._runtime_route_instances(app_id),
                route_prevalidated=True,
                frozen_plan_signature=signature,
                execution_plan=query_plan,
                cross_host_sessions=self._cross_host_sessions.get(app_id, {}),
            )
            logger.info(f"[ALRE] run_query 完成: app_id={app_id}")
            return {
                "workflow_handle": workflow_handle,
                "status": "done",
                "result": result,
            }
        except Exception as e:
            logger.error(f"[ALRE] run_query 异常: app_id={app_id}, error={e}")
            return {
                "workflow_handle": workflow_handle,
                "status": "error",
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # 供外部调度器调用的单次执行入口
    # ------------------------------------------------------------------

    async def run_single_workflow(
        self,
        app_id: str,
        workflow_handle: str,
        state_callback=None,
    ) -> Any:
        """
        执行单次工作流（供 WorkflowScheduler 调用）。

        与 start_app() 不同：
        - 不检查是否已在运行
        - 不管理 _running_tasks 字典
        - 不部署/退订 ALCM 实例
        调用方自行通过 asyncio.Task 管理生命周期。

        Args:
            app_id:          应用 ID
            workflow_handle: 工作流句柄（调用方生成）

        Returns:
            工作流执行结果

        Raises:
            ValueError: 应用未安装（无指导文件）
        """
        guidance = self._guidance_files.get(app_id)
        if not guidance:
            raise ValueError(f"应用 {app_id} 未安装")
        # 注意：调度器调用 run_single_workflow 时会把 viz_enabled 设为 False，
        # 目的是避免为每次周期调度创建单独的可视化工作流（防止列表泛滥）。
        # 同时，通过 state_callback 回传子运行的逐节点状态，调度器可将
        # 这些状态合并到调度会话的聚合快照中，使调度页面仍能展示进度。
        return await self._run_workflow(
            app_id,
            guidance,
            workflow_handle,
            viz_enabled=False,
            state_callback=state_callback,
        )

    # ------------------------------------------------------------------
    # 内部工作流执行
    # ------------------------------------------------------------------

    async def _run_workflow(
        self,
        app_id: str,
        guidance: GuidanceFile,
        workflow_handle: str,
        viz_enabled: bool = True,
        state_callback=None,
    ):
        """
        实际调用编排层运行工作流

        通过 run_distributed_workflow() 与编排层集成，
        结果存储在 workflow_handle 对应的状态中。
        """
        try:
            from src.distributed_workflow import run_distributed_workflow

            # 从约束中读取超时配置
            timeout = guidance.constraints.get(
                "timeout_seconds",
                guidance.constraints.get("max_timeout", 120),
            )

            logger.info(
                f"[ALRE] 开始执行工作流: app_id={app_id}, "
                f"task={guidance.task_description[:80]}..."
            )

            plan = self._execution_plans.get(app_id)
            signature = self._frozen_plan_signatures.get(app_id)
            if not plan or not signature:
                raise RuntimeError("FROZEN_PLAN_MISSING: 应用没有冻结执行计划")

            result = await run_distributed_workflow(
                user_input=guidance.task_description,
                adaptive_mode=False,
                replanning_enabled=False,
                max_retries=guidance.constraints.get("max_retries", 3),
                timeout_seconds=timeout,
                skills_content=guidance.skills_content or "",
                execution_plan=[copy.deepcopy(task) for task in plan],
                cross_host_sessions=self._cross_host_sessions.get(app_id, {}),
                route_prevalidated=True,
                frozen_plan_signature=signature,
                # 把 viz_enabled 及 state_callback 透传给编排层：
                # - viz_enabled 控制是否在 VizBus 中注册/推送逐节点更新
                # - state_callback 是一个可选回调（由上层传入），用于把逐节点
                #   更新回传给调用方（例如 WorkflowScheduler），以便合并调度视图
                viz_enabled=viz_enabled,
                workflow_id=workflow_handle,
                state_callback=state_callback,
                route_instances=self._runtime_route_instances(app_id),
            )

            logger.info(
                f"[ALRE] ✅ 工作流完成: app_id={app_id}, "
                f"workflow_handle={workflow_handle}"
            )
            return result

        except asyncio.CancelledError:
            logger.info(f"[ALRE] 工作流被取消: app_id={app_id}")
            raise
        except Exception as e:
            logger.error(f"[ALRE] 工作流异常: app_id={app_id}, error={e}")
            raise

    def _on_workflow_done(self, app_id: str, workflow_handle: str, task: asyncio.Task) -> None:
        """后台工作流结束后同步应用状态，避免 UI 长时间显示 running。"""
        if task.cancelled():
            return

        exc = task.exception()
        if exc is None:
            return

        logger.warning(f"[ALRE] 工作流失败回调: app_id={app_id}, error={exc}")
        self._unsubscribe_instances(app_id, workflow_handle)
        self._running_tasks.pop(app_id, None)
        if self._workflow_handles.get(app_id) == workflow_handle:
            self._workflow_handles.pop(app_id, None)

        try:
            from src.app.app_manager import get_app_manager

            manager = get_app_manager()
            app = manager.get_app(app_id)
            if app:
                app.workflow_handle = None
                app.app_interface_url = None
                app.update_status("error", str(exc))
                manager._save_to_disk()
        except Exception as callback_exc:
            logger.warning(f"[ALRE] 回写应用错误状态失败: app_id={app_id}, error={callback_exc}")

    # ------------------------------------------------------------------
    # ALCM 集成（§2/§3 ORCH↔RUN 生命周期）
    # ------------------------------------------------------------------

    def _deploy_plan_local_agents(
        self,
        execution_plan: List[Dict[str, Any]],
        registry_snapshot: List[Dict[str, Any]],
        workflow_handle: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """Deploy only agents explicitly owned by this AOE in the frozen plan."""
        from src.app.agent_warehouse import get_agent_warehouse
        from src.runtime.lifecycle_manager import get_lifecycle_manager

        registry = {
            str(agent.get("id") or agent.get("agent_id") or ""): agent
            for agent in registry_snapshot
        }
        local_tasks: List[Dict[str, Any]] = []
        for task in execution_plan:
            agent_id = str(task.get("assigned_agent_id") or "")
            agent = registry.get(agent_id)
            if agent is None:
                raise RuntimeError(f"AGENT_OWNERSHIP_UNKNOWN: {agent_id}")
            if agent.get("is_local") is True:
                local_tasks.append(task)

        warehouse = get_agent_warehouse()
        alcm = get_lifecycle_manager()
        images = warehouse.list_images()
        deployed_by_agent: Dict[str, Any] = {}
        instance_ids: List[str] = []
        subscribed_ids: set[str] = set()
        bindings: List[Dict[str, Any]] = []
        try:
            for task in local_tasks:
                agent_id = str(task["assigned_agent_id"])
                instance = deployed_by_agent.get(agent_id)
                if instance is None:
                    matches = [image for image in images if image.name == agent_id]
                    if len(matches) != 1:
                        raise RuntimeError(
                            f"Agent {agent_id} 在本地 Agent Warehouse 中匹配到 {len(matches)} 个镜像"
                        )
                    image = matches[0]
                    capability = str(
                        task.get("capability_required")
                        or registry[agent_id].get("capability")
                        or agent_id
                    )
                    effective_config = (
                        copy.deepcopy(resource_config)
                        if resource_config is not None
                        else self._resource_config_from_image(image)
                        or self._auto_resource_config(capability)
                    )
                    instance = alcm.deploy_agent(agent_id, image.image_id, effective_config)
                    instance_ids.append(instance.instance_id)
                    deployed_by_agent[agent_id] = instance
                    if instance.status != "running":
                        raise RuntimeError(instance.error_message or f"Agent {agent_id} 部署失败")
                    if not alcm.subscribe(instance.instance_id, workflow_handle):
                        raise RuntimeError(f"Agent {agent_id} 实例订阅失败")
                    subscribed_ids.add(instance.instance_id)

                bindings.append({
                    "task_id": task["task_id"],
                    "agent_id": instance.agent_id,
                    "instance_id": instance.instance_id,
                    "cluster_id": instance.cluster_id,
                    "status": instance.status,
                })
            return bindings, instance_ids
        except Exception:
            for instance_id in reversed(instance_ids):
                try:
                    if instance_id in subscribed_ids:
                        alcm.unsubscribe(instance_id, workflow_handle)
                    elif alcm.get_instance(instance_id):
                        alcm.shutdown_agent(instance_id, force=True)
                except Exception as cleanup_exc:
                    logger.warning("本地计划部署回滚失败: instance=%s, error=%s", instance_id, cleanup_exc)
            raise

    @staticmethod
    def _inject_query_into_plan(
        execution_plan: List[Dict[str, Any]], user_input: str
    ) -> List[Dict[str, Any]]:
        """Refresh business descriptions without changing frozen routing fields."""
        updated = [copy.deepcopy(task) for task in execution_plan]
        for task in updated:
            description = str(task.get("task_description") or "")
            marker = "\n\n用户请求："
            base = description.split(marker, 1)[0]
            task["task_description"] = f"{base}{marker}{user_input}" if base else f"用户请求：{user_input}"
            task["status"] = "pending"
            task["result"] = ""
            task["retry_count"] = 0
        return updated

    def _runtime_route_instances(self, app_id: str) -> List[Dict[str, Any]]:
        """Return the app's subscribed runtime instances, never registry placeholders."""
        from src.runtime.lifecycle_manager import get_lifecycle_manager

        workflow_handle = self._workflow_handles.get(app_id)
        instance_ids = self._instance_ids.get(app_id, [])
        snapshots: List[Dict[str, Any]] = []
        for instance_id in instance_ids:
            instance = get_lifecycle_manager().get_instance(instance_id)
            if instance is None:
                raise RuntimeError(f"INSTANCE_MISSING: 运行实例 {instance_id} 不存在")
            if instance.status != "running":
                raise RuntimeError(
                    f"INSTANCE_NOT_RUNNING: 运行实例 {instance_id} 状态为 {instance.status}"
                )
            if not workflow_handle or workflow_handle not in instance.subscribed_workflows:
                raise RuntimeError(
                    f"INSTANCE_NOT_SUBSCRIBED: 运行实例 {instance_id} 未订阅当前应用工作流"
                )
            snapshots.append(instance.to_dict())
        return snapshots

    def _resource_config_from_image(self, image) -> Optional[ResourceConfig]:
        """从 AgentImage.metadata.k8s 读取默认资源配置。"""
        if image is None:
            return None
        k8s_config = image.metadata.get("k8s", {}) if image.metadata else {}
        if not k8s_config:
            return None
        return ResourceConfig(
            cpu_cores=float(k8s_config.get("cpu_cores", 1.0)),
            memory_mb=int(k8s_config.get("memory_mb", 512)),
            node_id=str(k8s_config.get("node_id", "localhost")),
            gpu_count=int(k8s_config.get("gpu_count", 0)),
        )

    def _auto_resource_config(self, capability: str) -> ResourceConfig:
        """
        自动为 capability 挑选资源配置：
        1. 优先选 tag 命中的在线节点
        2. 其次选任意有余量的在线节点
        3. 根据能力给一组保守默认资源
        """
        cpu = 0.5
        memory_mb = 512
        gpu = 0

        if capability in {"agent-grpc", "agent-a"}:
            cpu = 0.5
            memory_mb = 512
        elif capability == "agent-b":
            cpu = 0.5
            memory_mb = 512
        elif capability == "agent-c":
            cpu = 0.5
            memory_mb = 512
        elif capability in {"vision"}:
            cpu = 1.0
            memory_mb = 1024
            gpu = 1
        elif capability == "perception2intermediatefeature":
            cpu = 2.0
            memory_mb = 4096
            gpu = 1
        elif capability == "cooperativefeaturefusiondetectionviz":
            cpu = 2.0
            memory_mb = 8192
            gpu = 1
        elif capability in {"compute", "nlp", "code_execution"}:
            cpu = 1.0
            memory_mb = 1024

        try:
            from src.service.resource_registry import get_resource_registry

            registry = get_resource_registry()
            if not registry.refresh_from_kubernetes():
                raise RuntimeError("Kubernetes 资源状态刷新失败")
            candidates = registry.query_available_resources(
                min_cpu=cpu,
                min_mem_mb=memory_mb,
                tags=[capability],
            )
            if not candidates:
                candidates = registry.query_available_resources(
                    min_cpu=cpu,
                    min_mem_mb=memory_mb,
                )
            if candidates:
                chosen = candidates[0]
                if gpu > 0:
                    gpu_candidates = [
                        node for node in candidates if node.gpu_available >= gpu
                    ]
                    if gpu_candidates:
                        chosen = gpu_candidates[0]
                    else:
                        logger.warning(
                            f"[ALRE] GPU 资源不足: capability={capability}, "
                            f"required_gpu={gpu}"
                        )
                return ResourceConfig(
                    cpu_cores=cpu,
                    memory_mb=memory_mb,
                    node_id=chosen.node_id,
                    gpu_count=gpu,
                )
        except Exception as exc:
            logger.warning(f"[ALRE] 自动资源分配失败，回退默认配置: capability={capability}, error={exc}")

        return ResourceConfig(
            cpu_cores=cpu,
            memory_mb=memory_mb,
            node_id="localhost",
            gpu_count=gpu,
        )

    def _unsubscribe_instances(
        self, app_id: str, workflow_handle: Optional[str]
    ):
        """
        退订并（引用归零时）关闭 Agent 实例。

        流程（对应接口文档 §3）：
        ALCM.unsubscribe() → 引用为0时自动关闭实例
        """
        instance_ids = self._instance_ids.pop(app_id, [])
        if not instance_ids or not workflow_handle:
            return

        try:
            from src.runtime.lifecycle_manager import get_lifecycle_manager

            alcm = get_lifecycle_manager()
            for iid in instance_ids:
                remaining = alcm.unsubscribe(iid, workflow_handle)
                logger.info(
                    f"[ALRE→ALCM] 退订: instance={iid}, "
                    f"workflow={workflow_handle}, 剩余引用={remaining}"
                )
        except Exception as e:
            logger.warning(f"[ALRE→ALCM] 退订 Agent 实例失败（非关键）: {e}")

    def _register_qos_callback(self):
        """
        向 QoSMonitor 注册告警回调，闭环：QoS告警 → ASD.redeploy_agent()

        每个进程只注册一次（通过 _qos_callback_registered 标志保护）。
        """
        if self._qos_callback_registered:
            return

        try:
            from src.runtime.qos_monitor import get_qos_monitor
            from src.service.agent_scheduler import get_agent_scheduler

            scheduler = get_agent_scheduler()

            def _on_qos_alert(agent_id: str, metrics) -> None:
                """QoS 告警触发：重新部署失效 Agent"""
                logger.warning(
                    f"[ALRE] QoS 告警触发重部署: agent_id={agent_id}, "
                    f"avg_latency={metrics.avg_latency_ms:.1f}ms, "
                    f"success_rate={metrics.success_rate:.1%}"
                )
                try:
                    scheduler.redeploy_agent(agent_id)
                except Exception as e:
                    logger.error(f"[ALRE] redeploy_agent 失败: agent_id={agent_id}, error={e}")

            get_qos_monitor().register_alert_callback(_on_qos_alert)
            self._qos_callback_registered = True
            logger.info("[ALRE] QoS 告警回调已注册（ASD.redeploy_agent）")

        except Exception as e:
            logger.warning(f"[ALRE] QoS 告警回调注册失败（非关键）: {e}")


# ======================================================================
# 单例访问
# ======================================================================
_engine_instance: Optional[AppLogicEngine] = None


def get_app_logic_engine() -> AppLogicEngine:
    """获取全局 AppLogicEngine 单例"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AppLogicEngine()
    return _engine_instance


async def _cancel_remote_session(remote_url: str, session_id: str) -> None:
    """
    向远端 AOE 发送 DELETE /orchestration/session/{id}，触发对端 Task.cancel()。
    fire-and-forget，不传播异常。
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.delete(
                f"{remote_url}/orchestration/session/{session_id}"
            )
        logger.info(f"[ALRE] 远端会话取消通知已发送: session_id={session_id}, url={remote_url}")
    except Exception as e:
        logger.debug(f"[ALRE] 远端会话取消通知失败（非关键）: session_id={session_id}, err={e}")
