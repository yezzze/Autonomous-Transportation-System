"""
Server script for running the LangManus API.
"""

import logging
import os
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    os.environ.setdefault("AGENT_DEPLOY_BACKEND", "kubernetes")
    logger.info("Starting LangManus API server")
    from src.config.env import REASONING_MODEL, REASONING_BASE_URL, BASIC_MODEL, BASIC_BASE_URL, VL_MODEL
    logger.info(f"[LLM] reasoning : {REASONING_MODEL}  ({REASONING_BASE_URL})")
    logger.info(f"[LLM] basic     : {BASIC_MODEL}  ({BASIC_BASE_URL})")
    logger.info(f"[LLM] vision    : {VL_MODEL}")
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
        log_level="info",
    )
