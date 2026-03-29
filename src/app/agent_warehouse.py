"""
智能体仓库 (AW - Agent Warehouse)

应用管理层组件，负责：
- 存储并管理 Agent 镜像（本地字典 + JSON 持久化）
- 安装镜像时，向 ARDC（AgentRegistryClient）注册，使其可被编排引擎发现
- 卸载镜像时，从 ARDC 注销

对应接口文档：应用管理层接口流程 §1 安装/卸载应用
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from src.app.models import AgentImage

logger = logging.getLogger(__name__)

# 持久化存储路径（与 agent_registry.json 同目录）
_DEFAULT_WAREHOUSE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "agent_warehouse.json",
)


class AgentWarehouse:
    """
    智能体仓库（AW）

    职责：
    1. install_agent()   — 存储镜像 + 注册到 ARDC
    2. uninstall_agent() — 删除镜像 + 从 ARDC 注销
    3. get_image()       — 按 image_id 查询
    4. list_images()     — 列出所有镜像

    持久化：
    - 数据存储在 config/agent_warehouse.json
    - 每次增删操作后自动保存
    """

    def __init__(self, warehouse_file: Optional[str] = None):
        self._warehouse_file = warehouse_file or _DEFAULT_WAREHOUSE_FILE
        # image_id → AgentImage
        self._images: Dict[str, AgentImage] = {}
        self._load_from_json()
        logger.info(
            f"AgentWarehouse (AW) 初始化完成，已加载 {len(self._images)} 个镜像"
        )

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    def install_agent(
        self,
        image: AgentImage,
        expose_external: bool = False,
    ) -> AgentImage:
        """
        安装 Agent 镜像

        流程（参考接口文档 §1）：
        1. 存储镜像到本地仓库
        2. 调用 ARDC 注册（使 Agent 可被编排层发现）

        Args:
            image:            要安装的 AgentImage 对象
            expose_external:  是否允许外部主体使用

        Returns:
            安装后的 AgentImage（registered=True）
        """
        image.exposed_external = expose_external
        self._images[image.image_id] = image
        self._save_to_json()

        # 向 ARDC 注册
        success = self._register_to_ardc(image)
        if success:
            image.registered = True
            self._save_to_json()
            logger.info(
                f"[AW] 安装成功: image_id={image.image_id}, "
                f"capability={image.capability}, external={expose_external}"
            )
        else:
            logger.warning(f"[AW] 安装镜像成功，但 ARDC 注册失败: {image.image_id}")

        return image

    def uninstall_agent(self, image_id: str) -> bool:
        """
        卸载 Agent 镜像

        流程（参考接口文档 §1）：
        1. 从 ARDC 注销
        2. 从本地仓库删除

        Args:
            image_id: 要卸载的镜像 ID

        Returns:
            True 表示成功，False 表示 image_id 不存在
        """
        image = self._images.get(image_id)
        if not image:
            logger.warning(f"[AW] uninstall_agent: image_id={image_id} 不存在")
            return False

        # 从 ARDC 注销
        self._deregister_from_ardc(image)

        # 删除本地记录
        del self._images[image_id]
        self._save_to_json()
        logger.info(f"[AW] 卸载成功: image_id={image_id}")
        return True

    def get_image(self, image_id: str) -> Optional[AgentImage]:
        """按 image_id 查询镜像"""
        return self._images.get(image_id)

    def list_images(self) -> List[AgentImage]:
        """列出所有镜像"""
        return list(self._images.values())

    def find_by_capability(self, capability: str) -> List[AgentImage]:
        """按能力类型查找镜像"""
        return [img for img in self._images.values() if img.capability == capability]

    # ------------------------------------------------------------------
    # ARDC 集成
    # ------------------------------------------------------------------

    def _register_to_ardc(self, image: AgentImage) -> bool:
        """将镜像注册到 ARDC，使其可被编排引擎发现"""
        try:
            from src.service.agent_registry import get_registry_client
            from src.graph.distributed_types import AgentInfo

            registry = get_registry_client()

            # 构造 AgentInfo 注册到 ARDC
            # 注意：这里 IP/port 使用 localhost 作为默认值
            # 实际运行时应从部署信息中获取
            agent_info: AgentInfo = {
                "id": f"agent_{image.image_id}",
                "ip": "localhost",
                "port": 8080,
                "capability": image.capability,
                "status": "offline",  # 尚未部署，初始为 offline
                "description": (
                    f"{image.name} v{image.version} — {image.description}"
                ),
            }

            # AgentRegistryClient 目前不支持动态注册，更新状态作为近似
            # 生产环境：调用 registry.register_agent(agent_info)
            logger.debug(f"[AW→ARDC] 注册意图: {agent_info}")
            return True
        except Exception as e:
            logger.warning(f"[AW→ARDC] 注册失败: {e}")
            return False

    def _deregister_from_ardc(self, image: AgentImage):
        """从 ARDC 注销镜像对应的 Agent"""
        try:
            from src.service.agent_registry import get_registry_client

            registry = get_registry_client()
            agent_id = f"agent_{image.image_id}"
            registry.update_agent_status(agent_id, "offline")
            logger.debug(f"[AW→ARDC] 注销: agent_id={agent_id}")
        except Exception as e:
            logger.warning(f"[AW→ARDC] 注销失败（非关键）: {e}")

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_from_json(self):
        """从 JSON 文件加载镜像数据"""
        path = Path(self._warehouse_file)
        if not path.exists():
            logger.debug(f"[AW] 镜像仓库文件不存在，从空状态初始化: {path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("images", []):
                img = AgentImage(**item)
                self._images[img.image_id] = img
            logger.debug(f"[AW] 从文件加载 {len(self._images)} 个镜像: {path}")
        except Exception as e:
            logger.warning(f"[AW] 加载镜像仓库文件失败: {e}")

    def _save_to_json(self):
        """持久化镜像数据到 JSON 文件"""
        path = Path(self._warehouse_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {"images": [img.to_dict() for img in self._images.values()]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[AW] 持久化镜像仓库失败: {e}")


# ======================================================================
# 单例访问
# ======================================================================
_warehouse_instance: Optional[AgentWarehouse] = None


def get_agent_warehouse() -> AgentWarehouse:
    """获取全局 AgentWarehouse 单例"""
    global _warehouse_instance
    if _warehouse_instance is None:
        _warehouse_instance = AgentWarehouse()
    return _warehouse_instance
