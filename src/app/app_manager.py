"""
应用管理器 (APPM - Application Manager)

应用管理层的核心聚合组件，负责：
- 聚合 AW（智能体仓库）和 ALRE（应用逻辑执行引擎）
- 提供应用全生命周期管理接口：安装/启动/停止/卸载
- 维护 AppInfo 状态（idle/starting/running/stopping/stopped）

对应接口文档：
- §1 安装/卸载应用（APPM 部分）
- §2 启动应用（APPM 部分）
- §3 停止应用（APPM 部分）
"""
import json
import logging
import os
import uuid
from typing import Dict, List, Optional

from src.app.models import AgentImage, AppInfo, GuidanceFile
from src.runtime.models import ResourceConfig

APPS_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "apps_store.json")

logger = logging.getLogger(__name__)


class AppManager:
    """
    应用管理器（APPM）

    职责：
    1. install()    — 安装镜像（调 AW）+ 存储指导文件（调 ALRE）
    2. uninstall()  — 删除指导文件（调 ALRE）+ 卸载镜像（调 AW）
    3. start()      — 启动编排工作流（调 ALRE.start_app）
    4. stop()       — 停止工作流（调 ALRE.stop_app）
    5. list_apps()  — 返回所有 AppInfo
    6. get_app()    — 查询单个 AppInfo
    """

    def __init__(self):
        # app_id → AppInfo
        self._apps: Dict[str, AppInfo] = {}
        self._store_path = os.path.abspath(APPS_STORE_PATH)
        os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
        self._load_from_disk()
        self._ensure_builtin_apps()
        logger.info("AppManager (APPM) 初始化完成")

    def _load_from_disk(self):
        """从磁盘恢复应用列表"""
        if not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for app_id, app_dict in data.items():
                if "status" in app_dict:
                    raise ValueError(
                        f"应用 {app_id} 包含已废弃字段 status={app_dict['status']!r}"
                    )
                for field_name in ("deployment_status", "run_status"):
                    if field_name not in app_dict:
                        raise ValueError(f"应用 {app_id} 缺少必需字段 {field_name}")
                gf_dict = app_dict.pop("guidance_file", None)
                guidance_file = None
                if gf_dict:
                    guidance_file = GuidanceFile(**gf_dict)
                try:
                    app = AppInfo(**app_dict, guidance_file=guidance_file)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"应用 {app_id} 配置无效: {exc}") from exc
                # 部署资源和工作流任务均为进程内状态，重启后不能沿用磁盘快照。
                app.deployment_status = "undeployed"
                if app.run_status in {"starting", "running", "stopping"}:
                    app.run_status = "stopped"
                app.workflow_handle = None
                self._apps[app_id] = app
                # 同步恢复 ALRE 的指导文件内存字典
                if guidance_file:
                    engine = self._get_engine()
                    engine.install_app_logic(guidance_file)
            logger.info(f"[APPM] 从磁盘恢复 {len(self._apps)} 个应用")
        except Exception as e:
            logger.error(f"[APPM] 恢复应用列表失败: {e}")
            raise

    def _save_to_disk(self):
        """将应用列表持久化到磁盘"""
        try:
            data = {app_id: app.to_dict() for app_id, app in self._apps.items()}
            with open(self._store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[APPM] 保存应用列表失败: {e}")

    def _ensure_builtin_apps(self):
        """仅执行历史兼容迁移，不再自动创建内置应用或探测本地镜像。"""
        changed = False

        old_agent_a = self._apps.pop("app_builtin_agent_a", None)
        if old_agent_a is not None:
            changed = True
            if "app_builtin_agent_grpc" not in self._apps:
                old_agent_a.app_id = "app_builtin_agent_grpc"
                if old_agent_a.guidance_file:
                    old_agent_a.guidance_file.app_id = old_agent_a.app_id
                self._apps[old_agent_a.app_id] = old_agent_a

        for app in self._apps.values():
            migrated_image_ids = [
                "agent-grpc:v1" if image_id == "agent-a-grpc:v2" else image_id
                for image_id in app.image_ids
            ]
            if migrated_image_ids != app.image_ids:
                app.image_ids = migrated_image_ids
                changed = True

            guidance = app.guidance_file
            if guidance is None:
                continue

            migrated_agents_required = [
                "agent-grpc" if capability == "agent-a" else capability
                for capability in guidance.agents_required
            ]
            if migrated_agents_required != guidance.agents_required:
                guidance.agents_required = migrated_agents_required
                changed = True

            migrated_task_description = guidance.task_description.replace(
                "agent-a/agent-b/agent-c", "agent_gRPC/agent-b/agent-c"
            ).replace("Agent A", "agent_gRPC")
            if migrated_task_description != guidance.task_description:
                guidance.task_description = migrated_task_description
                changed = True

            if guidance.app_id != app.app_id:
                guidance.app_id = app.app_id
                changed = True

        if changed:
            engine = self._get_engine()
            for app in self._apps.values():
                if app.guidance_file:
                    engine.install_app_logic(app.guidance_file)
            self._save_to_disk()

    # ------------------------------------------------------------------
    # 安装 / 卸载
    # ------------------------------------------------------------------

    def install(
        self,
        name: str,
        guidance_file: GuidanceFile,
        images: Optional[List[AgentImage]] = None,
        expose_external: bool = False,
    ) -> AppInfo:
        """
        安装应用

        流程（参考接口文档 §1）：
        1. 可选：调用 AW 安装 Agent 镜像并注册到 ARDC
        2. 调用 ALRE 保存编排指导文件
        3. 创建并保存 AppInfo

        Args:
            name:             应用名称
            guidance_file:    编排指导文件（任务描述、约束等）
            images:           包含的 Agent 镜像列表（可选）
            expose_external:  是否外部可见

        Returns:
            AppInfo
        """
        # 确保 guidance_file.app_id 一致
        if not guidance_file.app_id:
            guidance_file.app_id = f"app_{uuid.uuid4().hex[:8]}"

        app = AppInfo.create(name=name, guidance_file=guidance_file)
        app.app_id = guidance_file.app_id  # 与 guidance_file 使用同一 app_id

        # 1. 安装镜像到 AW（可选）
        if images:
            warehouse = self._get_warehouse()
            for img in images:
                warehouse.install_agent(img, expose_external=expose_external)
                app.image_ids.append(img.image_id)

        # 2. 保存指导文件到 ALRE
        engine = self._get_engine()
        engine.install_app_logic(guidance_file)

        # 3. 保存应用信息
        self._apps[app.app_id] = app
        self._save_to_disk()

        logger.info(
            f"[APPM] ✅ 安装完成: app_id={app.app_id}, name={name}, "
            f"images={len(images or [])}"
        )
        return app

    def uninstall(self, app_id: str) -> bool:
        """
        卸载应用

        流程（参考接口文档 §1）：
        1. 若正在运行，先停止（同步版本：标记停止意图）
        2. 调用 ALRE 删除指导文件
        3. 调用 AW 卸载镜像
        4. 删除 AppInfo

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功，False 表示未找到
        """
        app = self._apps.get(app_id)
        if not app:
            logger.warning(f"[APPM] uninstall: app_id={app_id} 未找到")
            return False

        if app.run_status in {"starting", "running", "stopping"}:
            logger.warning(
                f"[APPM] uninstall: app_id={app_id} 正在运行，建议先调用 stop()"
            )

        # 删除 ALRE 指导文件
        engine = self._get_engine()
        engine.uninstall_app_logic(app_id)

        # 卸载 AW 中的镜像
        warehouse = self._get_warehouse()
        for image_id in app.image_ids:
            warehouse.uninstall_agent(image_id)

        # 删除应用记录
        del self._apps[app_id]
        self._save_to_disk()
        logger.info(f"[APPM] ✅ 卸载完成: app_id={app_id}")
        return True

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    async def start(
        self,
        app_id: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> Optional[str]:
        """
        启动应用

        流程（参考接口文档 §2）：
        1. 更新状态为 starting
        2. 调用 ALRE.start_app()
        3. 更新状态为 running，填充 workflow_handle

        Args:
            app_id: 应用 ID
            resource_config: 启动时传给编排器的容器资源配置

        Returns:
            workflow_handle（str），失败返回 None
        """
        app = self._apps.get(app_id)
        if not app:
            logger.error(f"[APPM] start: app_id={app_id} 未找到")
            return None

        engine = self._get_engine()
        deploy_only = bool(
            app.guidance_file
            and app.guidance_file.metadata.get("deploy_only")
        )
        if app.deployment_status == "deployed" and engine.is_deployed(app_id):
            logger.warning(f"[APPM] start: app_id={app_id} 已在运行中")
            return app.workflow_handle

        app.update_deployment_status("deploying")
        app.update_run_status("not_running" if deploy_only else "starting")

        try:
            handle = await engine.start_app(app_id, resource_config=resource_config)

            if handle:
                app.workflow_handle = handle
                app.app_interface_url = f"/api/apps/{app_id}/interface"
                app.update_deployment_status("deployed")
                app.update_run_status("not_running" if deploy_only else "running")
                logger.info(f"[APPM] ✅ 启动成功: app_id={app_id}, handle={handle}")
            else:
                app.update_deployment_status("deployment_error", "ALRE 未能启动工作流")
                app.update_run_status("not_running")
                logger.error(f"[APPM] 启动失败: app_id={app_id}")

            self._save_to_disk()
            return handle

        except Exception as e:
            app.update_deployment_status("deployment_error", str(e))
            app.update_run_status("not_running")
            logger.error(f"[APPM] 启动异常: app_id={app_id}, error={e}")
            return None

    async def stop(self, app_id: str) -> bool:
        """
        停止应用

        流程（参考接口文档 §3）：
        1. 更新状态为 stopping
        2. 调用 ALRE.stop_app()
        3. 更新状态为 stopped

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功停止，False 表示失败
        """
        app = self._apps.get(app_id)
        if not app:
            logger.warning(f"[APPM] stop: app_id={app_id} 未找到")
            return False

        if app.deployment_status not in {"deploying", "deployed", "deployment_error"}:
            logger.warning(
                f"[APPM] stop: app_id={app_id} 当前部署状态 "
                f"{app.deployment_status}，无需停止"
            )
            return False

        app.update_run_status("stopping")
        app.update_deployment_status("undeploying")
        app.schedule_enabled = False

        try:
            engine = self._get_engine()
            success = await engine.stop_app(app_id)

            if success:
                app.workflow_handle = None
                app.app_interface_url = None
                app.update_deployment_status("undeployed")
                app.update_run_status("stopped")
                logger.info(f"[APPM] ✅ 停止成功: app_id={app_id}")
            else:
                app.update_deployment_status("undeployed")
                app.update_run_status("stopped")
                logger.warning(f"[APPM] stop: ALRE 未找到运行工作流，标记为 stopped")

            self._save_to_disk()
            return True

        except Exception as e:
            app.update_deployment_status("deployment_error", str(e))
            app.update_run_status("stopped")
            logger.error(f"[APPM] 停止异常: app_id={app_id}, error={e}")
            return False

    # ------------------------------------------------------------------
    # 周期调度
    # ------------------------------------------------------------------

    async def start_schedule(
        self,
        app_id: str,
        resource_config: Optional[ResourceConfig] = None,
    ) -> bool:
        """
        启动应用的周期调度

        从 GuidanceFile.constraints 读取调度配置：
        - schedule_interval_seconds (可选，默认 0；0 表示串行连续执行)
        - schedule_max_parallel (默认 5)
        - schedule_max_history (默认 100)

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功启动
        """
        app = self._apps.get(app_id)
        if not app:
            logger.error(f"[APPM] start_schedule: app_id={app_id} 未找到")
            return False

        if app.schedule_enabled:
            logger.warning(f"[APPM] start_schedule: app_id={app_id} 已在调度中")
            return False

        if not app.guidance_file:
            logger.error(f"[APPM] start_schedule: app_id={app_id} 无指导文件")
            return False

        deploy_only = bool(app.guidance_file.metadata.get("deploy_only"))
        engine = self._get_engine()
        if not deploy_only and app.run_status in {"starting", "running", "stopping"}:
            logger.error(
                "[APPM] start_schedule: 普通应用 %s 当前状态 %s，不能周期启动",
                app_id,
                app.run_status,
            )
            return False
        if deploy_only and not engine.is_deployed(app_id):
            logger.error(
                "[APPM] start_schedule: deploy_only 应用 %s 尚未完成部署",
                app_id,
            )
            return False

        constraints = app.guidance_file.constraints
        try:
            interval = float(constraints.get("schedule_interval_seconds", 0) or 0)
        except (TypeError, ValueError):
            logger.error(
                f"[APPM] start_schedule: app_id={app_id} "
                f"schedule_interval_seconds 值无效"
            )
            return False

        if interval < 0:
            logger.error(
                f"[APPM] start_schedule: app_id={app_id} "
                f"schedule_interval_seconds 不能小于 0"
            )
            return False

        max_parallel = int(constraints.get("schedule_max_parallel", 5))
        if interval == 0:
            max_parallel = 1
        max_history = int(constraints.get("schedule_max_history", 100))

        # 普通应用的“周期启动”与普通启动共用完整编排部署流程，区别仅在于
        # 冻结计划后不立即执行，而是交给调度器按周期触发。
        deployed_for_schedule = False
        if not deploy_only and not engine.is_deployed(app_id):
            app.update_deployment_status("deploying")
            app.update_run_status("starting")
            try:
                handle = await engine.start_app(
                    app_id,
                    resource_config=resource_config,
                    auto_execute=False,
                )
            except Exception as exc:
                app.update_deployment_status("deployment_error", str(exc))
                app.update_run_status("not_running")
                self._save_to_disk()
                logger.error("[APPM] 周期启动部署失败: app_id=%s, error=%s", app_id, exc)
                return False
            if not handle:
                app.update_deployment_status("deployment_error", "ALRE 未能完成周期启动部署")
                app.update_run_status("not_running")
                self._save_to_disk()
                return False
            deployed_for_schedule = True
            app.workflow_handle = handle
            app.app_interface_url = f"/api/apps/{app_id}/interface"

        scheduler = self._get_scheduler()
        success = await scheduler.start_schedule(
            app_id, interval, max_parallel, max_history
        )
        if not success and deployed_for_schedule:
            await engine.stop_app(app_id)
            app.workflow_handle = None
            app.app_interface_url = None
            app.update_deployment_status(
                "deployment_error", "部署完成，但周期调度器启动失败，已回滚部署"
            )
            app.update_run_status("not_running")
            self._save_to_disk()
            return False
        if success:
            app.schedule_enabled = True
            app.update_deployment_status("deployed")
            app.update_run_status("running")
            self._save_to_disk()
            logger.info(
                f"[APPM] 周期调度已启动: app_id={app_id}, "
                f"interval={interval}s, "
                f"workflow_handle={app.workflow_handle}"
            )
        return success

    async def stop_schedule(self, app_id: str) -> bool:
        """
        停止应用的周期调度

        活跃的工作流实例继续运行直到完成。

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功停止
        """
        app = self._apps.get(app_id)
        if not app:
            logger.warning(f"[APPM] stop_schedule: app_id={app_id} 未找到")
            return False

        scheduler = self._get_scheduler()
        deploy_only = bool(
            app.guidance_file
            and app.guidance_file.metadata.get("deploy_only")
        )
        deploy_only_scheduled = bool(
            deploy_only and scheduler.get_schedule_status(app_id)
        )
        if not app.schedule_enabled and not deploy_only_scheduled:
            logger.warning(
                f"[APPM] stop_schedule: app_id={app_id} "
                "当前未启用周期调度"
            )
            return False

        success = await scheduler.stop_schedule(app_id, cancel_active=not deploy_only)
        if success:
            app.schedule_enabled = False
            if deploy_only and self._get_engine().is_deployed(app_id):
                app.update_deployment_status("deployed")
                app.update_run_status("stopped")
            else:
                # 普通“周期启动”的部署由调度生命周期持有；停止调度时同时
                # 释放本地实例、远端会话和冻结计划。
                await self._get_engine().stop_app(app_id)
                app.workflow_handle = None
                app.app_interface_url = None
                app.update_deployment_status("undeployed")
                app.update_run_status("stopped")
            self._save_to_disk()
            logger.info(f"[APPM] 周期调度已停止: app_id={app_id}")
        return success

    async def restore_schedules(self):
        """
        恢复之前处于 scheduled 状态的应用的周期调度。

        在 FastAPI startup 事件中调用。仅恢复配置了
        schedule_auto_restart: true 的应用。
        """
        restored = 0
        for app in self._apps.values():
            if not app.schedule_enabled or not app.guidance_file:
                continue
            constraints = app.guidance_file.constraints
            auto_restart = constraints.get("schedule_auto_restart", False)
            try:
                interval = float(constraints.get("schedule_interval_seconds", 0) or 0)
            except (TypeError, ValueError):
                continue
            if auto_restart and interval >= 0:
                if app.guidance_file.metadata.get("deploy_only"):
                    # Deployment state is process-local and must be recreated
                    # explicitly before scheduled execution can resume.
                    app.schedule_enabled = False
                    continue
                # 清除磁盘恢复出的意图标记，再通过正常启动路径重新建立调度。
                app.schedule_enabled = False
                success = await self.start_schedule(app.app_id)
                if success:
                    restored += 1
        if restored:
            logger.info(f"[APPM] 自动恢复了 {restored} 个周期调度")

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_app(self, app_id: str) -> Optional[AppInfo]:
        """按 app_id 查询应用信息"""
        return self._apps.get(app_id)

    def list_apps(self) -> List[AppInfo]:
        """列出所有应用"""
        return list(self._apps.values())

    def list_running_apps(self) -> List[AppInfo]:
        """列出所有运行中的应用"""
        return [a for a in self._apps.values() if a.run_status == "running"]

    def update(
        self,
        app_id: str,
        name: Optional[str] = None,
        task_description: Optional[str] = None,
        skills_content: Optional[str] = None,
        orchestration_mode: Optional[str] = None,
        agents_required: Optional[List[str]] = None,
        images: Optional[List[Dict]] = None,
        constraints: Optional[dict] = None,
    ) -> Optional[AppInfo]:
        """
        更新应用配置，无需重新安装

        不允许对运行中的应用修改 skills_content 或 orchestration_mode（因为
        这会改变工作流行为，需要重启）。task_description 和 name 可随时修改。

        Returns:
            更新后的 AppInfo，app_id 不存在时返回 None
        """
        app = self._apps.get(app_id)
        if not app:
            logger.warning(f"[APPM] update: app_id={app_id} 未找到")
            return None

        if name is not None:
            app.name = name

        if app.guidance_file:
            if task_description is not None:
                app.guidance_file.task_description = task_description
            if skills_content is not None:
                app.guidance_file.skills_content = skills_content
                # 同步更新 ALRE 的缓存
                engine = self._get_engine()
                engine.install_app_logic(app.guidance_file)
            if orchestration_mode is not None:
                app.guidance_file.orchestration_mode = orchestration_mode
            if agents_required is not None:
                app.guidance_file.agents_required = list(agents_required)
            if constraints is not None:
                app.guidance_file.constraints.update(constraints)

        if images is not None:
            image_ids = []
            for item in images:
                if not isinstance(item, dict):
                    continue
                image_id = item.get("image_id")
                if image_id:
                    image_ids.append(image_id)
            app.image_ids = image_ids

        app.updated_at = __import__("datetime").datetime.utcnow().isoformat()
        self._save_to_disk()
        logger.info(f"[APPM] ✅ 更新完成: app_id={app_id}")
        return app

    # ------------------------------------------------------------------
    # 内部帮助
    # ------------------------------------------------------------------

    def _get_warehouse(self):
        from src.app.agent_warehouse import get_agent_warehouse
        return get_agent_warehouse()

    def _get_engine(self):
        from src.app.app_logic_engine import get_app_logic_engine
        return get_app_logic_engine()

    def _get_scheduler(self):
        from src.service.workflow_scheduler import get_workflow_scheduler
        return get_workflow_scheduler()


# ======================================================================
# 单例访问
# ======================================================================
_manager_instance: Optional[AppManager] = None


def get_app_manager() -> AppManager:
    """获取全局 AppManager 单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AppManager()
    return _manager_instance
