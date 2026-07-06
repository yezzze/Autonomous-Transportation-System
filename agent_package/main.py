"""
Auto-Agent 项目入口文件

职责:
  - 初始化应用日志
  - 使用 uvicorn 启动 FastAPI 服务，监听 0.0.0.0:9001
  - 捕获未预期的异常并记录日志
"""
import uvicorn

from utils.logger_utils import get_logger

logger = get_logger(__name__)


def main():
    """应用主入口函数。启动 uvicorn ASGI 服务器，运行 FastAPI 应用。"""
    logger.info("Starting Auto-Agent ...")

    try:
        uvicorn.run("fast_api.app:app", host="0.0.0.0", port=9001)
    except Exception:
        logger.exception("Auto-Agent exited unexpectedly")
        raise


if __name__ == "__main__":
    main()
