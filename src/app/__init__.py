"""
应用管理层 (APP Layer)

对应系统架构图中的"应用管理层"，包含四个组件：
- APPM (AppManager):      应用全生命周期管理
- AW   (AgentWarehouse):  智能体镜像仓库
- ALRE (AppLogicEngine):  应用逻辑执行引擎
- DISP (display):         交互与呈现
"""

from src.app.models import AgentImage, AppInfo, GuidanceFile
from src.app.agent_warehouse import AgentWarehouse, get_agent_warehouse
from src.app.app_logic_engine import AppLogicEngine, get_app_logic_engine
from src.app.app_manager import AppManager, get_app_manager
from src.app import display

__all__ = [
    # 数据模型
    "AgentImage",
    "AppInfo",
    "GuidanceFile",
    # AW
    "AgentWarehouse",
    "get_agent_warehouse",
    # ALRE
    "AppLogicEngine",
    "get_app_logic_engine",
    # APPM
    "AppManager",
    "get_app_manager",
    # DISP
    "display",
]
