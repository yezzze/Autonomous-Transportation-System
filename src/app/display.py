"""
交互与呈现 (DISP - Display & Interaction)

应用管理层组件，负责：
- 提供运行中应用列表（供 UI 层展示）
- 返回应用接口信息（工作流输入 schema、当前状态等）

对应接口文档：§4 交互呈现
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def get_running_app_list() -> List[Dict]:
    """
    获取当前运行中的应用列表

    供 UI 层调用，过滤出 status=running 的应用。

    Returns:
        List[Dict]，每项包含 app_id, name, status, app_interface_url, workflow_handle
    """
    from src.app.app_manager import get_app_manager

    manager = get_app_manager()
    running_apps = manager.list_running_apps()

    result = []
    for app in running_apps:
        result.append(
            {
                "app_id": app.app_id,
                "name": app.name,
                "status": app.status,
                "app_interface_url": app.app_interface_url,
                "workflow_handle": app.workflow_handle,
            }
        )

    logger.debug(f"[DISP] 运行中应用列表: {len(result)} 个")
    return result


def get_all_app_list() -> List[Dict]:
    """
    获取所有应用的列表（含所有状态）

    Returns:
        List[Dict]
    """
    from src.app.app_manager import get_app_manager

    manager = get_app_manager()
    all_apps = manager.list_apps()
    return [app.to_dict() for app in all_apps]


def get_app_interface(app_id: str) -> Optional[Dict]:
    """
    获取应用的对外接口信息

    供 UI 层调用，返回应用当前状态和交互入口。

    Args:
        app_id: 应用 ID

    Returns:
        {
            "app_id": str,
            "name": str,
            "status": str,
            "app_interface_url": str | None,
            "workflow_handle": str | None,
            "input_schema": {...},    # 当前仅占位
        }
        或 None（应用不存在）
    """
    from src.app.app_manager import get_app_manager

    manager = get_app_manager()
    app = manager.get_app(app_id)
    if not app:
        logger.warning(f"[DISP] get_app_interface: app_id={app_id} 未找到")
        return None

    return {
        "app_id": app.app_id,
        "name": app.name,
        "status": app.status,
        "app_interface_url": app.app_interface_url,
        "workflow_handle": app.workflow_handle,
        # 输入 Schema 占位（生产环境从指导文件中解析）
        "input_schema": {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户输入的任务描述"}
            },
            "required": ["user_input"],
        },
    }
