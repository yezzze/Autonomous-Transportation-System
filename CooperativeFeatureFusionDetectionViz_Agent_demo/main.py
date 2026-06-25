import uvicorn

from utils.logger_utils import get_logger

logger = get_logger(__name__)

def main():
    logger.info("Starting CooperativeFeatureFusionDetectionViz Agent demo...")

    try:
        uvicorn.run("fast_api.app:app", host="0.0.0.0", port=9032)
    except Exception:
        logger.exception("CooperativeFeatureFusionDetectionViz Agent demo exited unexpectedly")
        raise


if __name__ == '__main__':
    main()
