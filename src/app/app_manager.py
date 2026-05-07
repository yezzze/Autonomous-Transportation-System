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
                gf_dict = app_dict.pop("guidance_file", None)
                guidance_file = None
                if gf_dict:
                    guidance_file = GuidanceFile(**gf_dict)
                app = AppInfo(**app_dict, guidance_file=guidance_file)
                # 重启后将运行中/启动中状态重置为 stopped
                if app.status in ("running", "starting", "stopping"):
                    app.status = "stopped"
                    app.workflow_handle = None
                self._apps[app_id] = app
                # 同步恢复 ALRE 的指导文件内存字典
                if guidance_file:
                    engine = self._get_engine()
                    engine.install_app_logic(guidance_file)
            logger.info(f"[APPM] 从磁盘恢复 {len(self._apps)} 个应用")
        except Exception as e:
            logger.warning(f"[APPM] 恢复应用列表失败: {e}")

    def _save_to_disk(self):
        """将应用列表持久化到磁盘"""
        try:
            data = {app_id: app.to_dict() for app_id, app in self._apps.items()}
            with open(self._store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[APPM] 保存应用列表失败: {e}")

    def _ensure_builtin_apps(self):
        """确保 gRPC/B/C 三个内置应用始终存在于应用列表中。"""
        for app in self._apps.values():
            app.image_ids = ["agent-grpc:v1" if image_id == "agent-a-grpc:v2" else image_id for image_id in app.image_ids]
            if app.guidance_file:
                app.guidance_file.agents_required = [
                    "agent-grpc" if capability == "agent-a" else capability
                    for capability in app.guidance_file.agents_required
                ]
                app.guidance_file.task_description = app.guidance_file.task_description.replace(
                    "agent-a/agent-b/agent-c", "agent_gRPC/agent-b/agent-c"
                ).replace("Agent A", "agent_gRPC")

        old_agent_a = self._apps.pop("app_builtin_agent_a", None)
        if old_agent_a is not None and "app_builtin_agent_grpc" not in self._apps:
            old_agent_a.app_id = "app_builtin_agent_grpc"
            self._apps[old_agent_a.app_id] = old_agent_a

        builtin_specs = [
            {
                "app_id": "app_builtin_agent_grpc",
                "name": "agent_gRPC",
                "capability": "agent-grpc",
                "task_description": "启动 agent_gRPC，作为 gRPC 入口并通过 NATS 转发任务。",
            },
            {
                "app_id": "app_builtin_agent_b",
                "name": "Agent B",
                "capability": "agent-b",
                "task_description": "启动 Agent B，作为 NATS worker 转发任务给 Agent C 并回传结果。",
            },
            {
                "app_id": "app_builtin_agent_c",
                "name": "Agent C",
                "capability": "agent-c",
                "task_description": "启动 Agent C，作为 NATS worker 处理消息并返回结果。",
            },
            {
                "app_id": "app_builtin_perception2intermediatefeature",
                "name": "Perception2IntermediateFeature",
                "capability": "perception2intermediatefeature",
                "task_description": (
                    "启动 Perception2IntermediateFeature，"
                    "将自动驾驶感知输入转换为中间特征。"
                ),
            },
            {
                "app_id": "app_builtin_cooperativefeaturefusiondetectionviz",
                "name": "CooperativeFeatureFusionDetectionViz",
                "capability": "cooperativefeaturefusiondetectionviz",
                "task_description": (
                    "启动 CooperativeFeatureFusionDetectionViz，"
                    "执行协同特征融合、目标检测与可视化。"
                ),
            },
        ]

        changed = False
        warehouse = self._get_warehouse()
        engine = self._get_engine()

        for spec in builtin_specs:
            image = next(iter(warehouse.find_by_capability(spec["capability"])), None)
            if image is None:
                continue

            app = self._apps.get(spec["app_id"])
            guidance = GuidanceFile(
                app_id=spec["app_id"],
                task_description=spec["task_description"],
                agents_required=[spec["capability"]],
                orchestration_mode="adaptive",
                constraints={"timeout_seconds": 120},
                metadata={"deploy_only": True},
            )

            if app is None:
                app = AppInfo.create(name=spec["name"], guidance_file=guidance)
                app.app_id = spec["app_id"]
                app.name = spec["name"]
                app.image_ids = [image.image_id] if image else []
                self._apps[app.app_id] = app
                changed = True
            else:
                app.name = spec["name"]
                app.guidance_file = guidance
                app.image_ids = [image.image_id] if image else app.image_ids
                changed = True

            engine.install_app_logic(guidance)

        if changed:
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

        if app.status == "running":
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

        if app.status == "running":
            logger.warning(f"[APPM] start: app_id={app_id} 已在运行中")
            return app.workflow_handle

        app.update_status("starting")

        try:
            engine = self._get_engine()
            handle = await engine.start_app(app_id, resource_config=resource_config)

            if handle:
                app.workflow_handle = handle
                app.app_interface_url = f"/api/apps/{app_id}/interface"
                app.update_status("running")
                logger.info(f"[APPM] ✅ 启动成功: app_id={app_id}, handle={handle}")
            else:
                app.update_status("error", "ALRE 未能启动工作流")
                logger.error(f"[APPM] 启动失败: app_id={app_id}")

            self._save_to_disk()
            return handle

        except Exception as e:
            app.update_status("error", str(e))
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

        if app.status not in ("running", "starting"):
            logger.warning(
                f"[APPM] stop: app_id={app_id} 当前状态 {app.status}，无需停止"
            )
            return False

        app.update_status("stopping")

        try:
            engine = self._get_engine()
            success = await engine.stop_app(app_id)

            if success:
                app.workflow_handle = None
                app.app_interface_url = None
                app.update_status("stopped")
                logger.info(f"[APPM] ✅ 停止成功: app_id={app_id}")
            else:
                app.update_status("stopped")  # 无论如何标记为 stopped
                logger.warning(f"[APPM] stop: ALRE 未找到运行工作流，标记为 stopped")

            self._save_to_disk()
            return True

        except Exception as e:
            app.update_status("error", str(e))
            logger.error(f"[APPM] 停止异常: app_id={app_id}, error={e}")
            return False

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
        return [a for a in self._apps.values() if a.status == "running"]

    def update(
        self,
        app_id: str,
        name: Optional[str] = None,
        task_description: Optional[str] = None,
        skills_content: Optional[str] = None,
        orchestration_mode: Optional[str] = None,
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
            if constraints is not None:
                app.guidance_file.constraints.update(constraints)

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
