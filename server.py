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
    os.environ.setdefault(
        "AGENT_DEPLOY_BACKEND",
        os.environ.get("SERVER_DEFAULT_AGENT_DEPLOY_BACKEND", "kubernetes"),
    )
    logger.info("Starting LangManus API server")
    from src.config.env import REASONING_MODEL, REASONING_BASE_URL, BASIC_MODEL, BASIC_BASE_URL, VL_MODEL
    logger.info(f"[LLM] reasoning : {REASONING_MODEL}  ({REASONING_BASE_URL})")
    logger.info(f"[LLM] basic     : {BASIC_MODEL}  ({BASIC_BASE_URL})")
    logger.info(f"[LLM] vision    : {VL_MODEL}")
    if os.environ.get("AGENT_DEPLOY_BACKEND", "").strip().lower() == "kubernetes":
        from src.service.agent_startup import AgentStartupConfig
        from src.service.nats_startup import NatsStartupConfig

        agent_config = AgentStartupConfig.from_env()
        nats_config = NatsStartupConfig.from_env()
        logger.info(
            "[AgentStartup] backend=%s namespace=%s http_port=%s grpc_port=%s "
            "image_pull_policy=%s health_probe=%s",
            agent_config.deploy_backend,
            agent_config.k8s_namespace,
            agent_config.agent_container_port,
            agent_config.grpc_container_port,
            agent_config.image_pull_policy,
            agent_config.enable_health_probe,
        )
        logger.info(
            "[NATS] deployment=%s service=%s image=%s servers=%s args=%s",
            nats_config.deployment_name,
            nats_config.service_name,
            nats_config.image,
            nats_config.servers,
            nats_config.server_args(),
        )
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
        log_level="info",
    )
