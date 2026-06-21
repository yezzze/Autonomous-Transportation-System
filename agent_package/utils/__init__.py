"""日志工具模块"""
import logging
import sys
from datetime import datetime


def get_logger(name: str) -> logging.Logger:
    """获取统一的命名空间日志记录器"""
    logger = logging.getLogger(f"AutoAgent.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
