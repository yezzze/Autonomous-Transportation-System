"""车路云 A2A Agent 入口：启动 FastAPI，监听 AGENT_PORT（默认 9001）。"""

import os

import uvicorn

from utils.logger_utils import get_logger

logger = get_logger(__name__)


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "9001"))
    agent_id = os.environ.get("AGENT_ID", "agent")
    logger.info("Starting %s on %s:%s ...", agent_id, host, port)
    uvicorn.run("fast_api.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
