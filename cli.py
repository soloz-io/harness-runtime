import logging
import os
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

_log_level = os.getenv("HARNESS_LOG_LEVEL")
_log_file = os.getenv("HARNESS_LOG_FILE")

if _log_level and _log_file:
    _log_dir = Path(_log_file).parent
    _log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=_log_file,
        level=getattr(logging, _log_level.upper(), logging.DEBUG),
        force=True,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

structlog.configure(
    logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


def main() -> None:
    from deepagents.profiles.provider import ProviderProfile, register_provider_profile

    register_provider_profile(
        "openai",
        ProviderProfile(init_kwargs={"use_responses_api": False}),
    )

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(2)

    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    logger.info("starting_http_server", port=port)
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        lifespan="on",
    )


if __name__ == "__main__":
    main()
