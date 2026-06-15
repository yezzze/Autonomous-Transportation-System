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

        agent_config = AgentStartupConfig.from_env()
        nats_service = os.getenv("NATS_SERVICE_NAME", "nats")
        nats_port = os.getenv("NATS_CLIENT_PORT", "4222")
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
            "[NATS] service=%s servers=%s agent_servers=%s domain=%s subjects=%s",
            nats_service,
            os.getenv("NATS_SERVERS", f"nats://127.0.0.1:{nats_port}"),
            os.getenv("AGENT_NATS_SERVERS", f"nats://{nats_service}:{nats_port}"),
            os.getenv("NATS_JETSTREAM_DOMAIN", "hub"),
            os.getenv("NATS_STREAM_SUBJECTS", "workflow.>"),
        )
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
        log_level="info",
    )
