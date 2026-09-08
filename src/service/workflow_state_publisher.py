"""当前工作流运行上下文的中间状态发布器。

图节点不应自行猜测 VizBus 中的 ``latest_running`` 工作流。运行入口会在
执行图之前绑定准确的发布函数，节点内的即时事件由此回传到当前运行。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Callable, Dict, Any, Optional


StatePublisher = Callable[[Dict[str, Any], str], None]
_current_publisher: ContextVar[Optional[StatePublisher]] = ContextVar(
    "workflow_state_publisher",
    default=None,
)


def bind_workflow_state_publisher(publisher: StatePublisher) -> Token:
    """为当前异步运行绑定状态发布器。"""
    return _current_publisher.set(publisher)


def reset_workflow_state_publisher(token: Token) -> None:
    """恢复进入当前运行前的发布上下文。"""
    _current_publisher.reset(token)


def publish_workflow_state(state: Dict[str, Any], node_name: str) -> None:
    """向当前运行发布中间状态；未绑定时安全忽略。"""
    publisher = _current_publisher.get()
    if publisher:
        publisher(state, node_name)
