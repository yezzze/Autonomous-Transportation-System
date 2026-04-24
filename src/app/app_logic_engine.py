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
from typing import Dict, List, Optional

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

        # 部署并订阅 Agent 实例（对应接口文档 §2 ORCH→RUN）
        instance_ids = self._deploy_and_subscribe(
            guidance,
            workflow_handle,
            resource_config=resource_config,
        )
        self._instance_ids[app_id] = instance_ids

        # deploy_only 应用只负责把 Agent 服务拉起，不执行编排工作流
        if guidance.metadata.get("deploy_only"):
            logger.info(f"[ALRE] deploy_only 应用已启动: app_id={app_id}, workflow_handle={workflow_handle}")
            return workflow_handle

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
        guidance = self._guidance_files.get(app_id)
        task = self._running_tasks.get(app_id)
        if guidance and guidance.metadata.get("deploy_only"):
            workflow_handle = self._workflow_handles.get(app_id)
            self._unsubscribe_instances(app_id, workflow_handle)
            self._running_tasks.pop(app_id, None)
            if app_id in self._workflow_handles:
                del self._workflow_handles[app_id]
            logger.info(f"[ALRE] ✅ deploy_only 应用已停止: app_id={app_id}")
            return True

        if not task or task.done():
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

        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        # 退订 Agent 实例（对应接口文档 §3 引用计数自减）
        workflow_handle = self._workflow_handles.get(app_id)
        self._unsubscribe_instances(app_id, workflow_handle)

        del self._running_tasks[app_id]
        if app_id in self._workflow_handles:
            del self._workflow_handles[app_id]

        logger.info(f"[ALRE] ✅ 停止应用: app_id={app_id}")
        return True

    def is_running(self, app_id: str) -> bool:
        """检查应用是否正在运行"""
        guidance = self._guidance_files.get(app_id)
        if guidance and guidance.metadata.get("deploy_only"):
            return bool(self._instance_ids.get(app_id))
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

        # 解析 Pipeline 拓扑（若 Skills.md 中包含 ## Pipeline 段落）
        pipeline_topology = []
        if guidance.skills_content:
            from src.app.pipeline_parser import parse_pipeline, topology_to_description
            pipeline_topology = parse_pipeline(guidance.skills_content) or []
            if pipeline_topology:
                from src.app.pipeline_parser import topology_to_description
                logger.info(
                    f"[ALRE] ⚡ 检测到 Pipeline 拓扑，跳过 LLM Planner: "
                    f"{topology_to_description(pipeline_topology)}"
                )

        try:
            result = await run_distributed_workflow(
                user_input=user_input,
                adaptive_mode=(guidance.orchestration_mode == "adaptive"),
                max_retries=guidance.constraints.get("max_retries", 3),
                timeout_seconds=timeout,
                skills_content=guidance.skills_content or "",
                pipeline_topology=pipeline_topology,
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
    # 内部工作流执行
    # ------------------------------------------------------------------

    async def _run_workflow(
        self,
        app_id: str,
        guidance: GuidanceFile,
        workflow_handle: str,
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

            # 解析 Pipeline 拓扑（与 run_query 保持一致）
            pipeline_topology = []
            if guidance.skills_content:
                from src.app.pipeline_parser import parse_pipeline, topology_to_description
                pipeline_topology = parse_pipeline(guidance.skills_content) or []
                if pipeline_topology:
                    logger.info(
                        f"[ALRE] ⚡ 检测到 Pipeline 拓扑，跳过 LLM Planner: "
                        f"{topology_to_description(pipeline_topology)}"
                    )

            result = await run_distributed_workflow(
                user_input=guidance.task_description,
                adaptive_mode=(guidance.orchestration_mode == "adaptive"),
                max_retries=guidance.constraints.get("max_retries", 3),
                timeout_seconds=timeout,
                skills_content=guidance.skills_content or "",
                pipeline_topology=pipeline_topology,
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

    def _deploy_and_subscribe(
        self,
        guidance: GuidanceFile,
        workflow_handle: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> List[str]:
        """
        为 agents_required 中的每个能力部署 Agent 实例，并订阅到本工作流。

        流程（对应接口文档 §2）：
        ORCH → RUN: 部署智能体 → ALCM.subscribe()

        Returns:
            已部署实例的 instance_id 列表
        """
        if not guidance.agents_required:
            return []

        try:
            from src.runtime.lifecycle_manager import get_lifecycle_manager
            from src.app.agent_warehouse import get_agent_warehouse

            alcm = get_lifecycle_manager()
            warehouse = get_agent_warehouse()
            instance_ids: List[str] = []

            for capability in guidance.agents_required:
                # 从 AW 查找对应能力的镜像
                images = warehouse.find_by_capability(capability)
                image_id = images[0].image_id if images else f"img_{capability}_default"
                agent_id = f"{capability}_agent"
                effective_resource_config = (
                    copy.deepcopy(resource_config)
                    if resource_config is not None
                    else self._auto_resource_config(capability)
                )

                # 部署 Agent 实例
                instance = alcm.deploy_agent(
                    agent_id=agent_id,
                    image_id=image_id,
                    resource_config=effective_resource_config,
                )
                # 工作流订阅（引用计数 +1）
                alcm.subscribe(instance.instance_id, workflow_handle)
                instance_ids.append(instance.instance_id)
                logger.info(
                    f"[ALRE→ALCM] 部署+订阅: capability={capability}, "
                    f"instance={instance.instance_id}, workflow={workflow_handle}"
                )

            return instance_ids

        except Exception as e:
            logger.warning(f"[ALRE→ALCM] 部署 Agent 实例失败（非关键）: {e}")
            return []

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
        elif capability in {"compute", "nlp", "code_execution"}:
            cpu = 1.0
            memory_mb = 1024

        try:
            from src.service.resource_registry import get_resource_registry

            registry = get_resource_registry()
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
                if gpu > chosen.gpu_available:
                    gpu = 0
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
