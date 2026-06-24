"""
Agent Template 协议定义包

本包包含基于 NATS JetStream 的底层数据传输封装。
"""

from .nats_comm import (
    NatsComm, NatsMessage
)

__all__ = [
    "NatsComm",
    "NatsMessage"
]
