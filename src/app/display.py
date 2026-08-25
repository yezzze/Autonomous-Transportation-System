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


def _with_runtime_state(app, data: Dict) -> Dict:
    """Add non-persistent deployment and scheduling state for UI controls."""
    from src.app.app_logic_engine import get_app_logic_engine
    from src.service.workflow_scheduler import get_workflow_scheduler

    schedule = get_workflow_scheduler().get_schedule_status(app.app_id)
    return {
        **data,
        "deployed": get_app_logic_engine().is_deployed(app.app_id),
        "schedule_active": schedule is not None,
        "schedule_workflow_handle": (
            schedule.get("schedule_workflow_handle") if schedule else None
        ),
    }


def _dedupe_apps(apps: List[Dict]) -> List[Dict]:
    """按 name 去重，同名应用优先保留运行中的，否则保留更新时间最新的。"""
    best_by_name: Dict[str, Dict] = {}
    for app in apps:
        name = app.get("name") or app.get("app_id")
        current = best_by_name.get(name)
        if current is None:
            best_by_name[name] = app
            continue

        current_running = current.get("status") == "running"
        app_running = app.get("status") == "running"
        if app_running and not current_running:
            best_by_name[name] = app
            continue
        if current_running and not app_running:
            continue

        if (app.get("updated_at") or "") >= (current.get("updated_at") or ""):
            best_by_name[name] = app

    result = list(best_by_name.values())
    result.sort(key=lambda item: ((item.get("name") or ""), (item.get("app_id") or "")))
    return result


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

    result = _dedupe_apps(result)
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
    return _dedupe_apps([
        _with_runtime_state(app, app.to_dict()) for app in all_apps
    ])


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

    return _with_runtime_state(app, {
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
        "guidance_file": app.guidance_file.to_dict() if app.guidance_file else None,
    })
