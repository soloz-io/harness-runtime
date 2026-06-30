import logging
import os
import subprocess
import sys
import time
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


def _setup_otel() -> None:
    """Initialize OpenTelemetry if the OTLP endpoint is configured.

    Configurable via standard OTEL environment variables:
      - OTEL_EXPORTER_OTLP_ENDPOINT  (default: http://localhost:4318)
      - OTEL_SERVICE_NAME            (default: harness-runtime)
      - OTEL_RESOURCE_ATTRIBUTES
    """
    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not otel_endpoint:
        logger.info("otel_disabled_no_endpoint")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.getenv("OTEL_SERVICE_NAME", "harness-runtime")

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.version": "0.1.13",
                }
            ),
        )
        exporter = OTLPSpanExporter(endpoint=otel_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Auto-instrumentation
        try:
            FastAPIInstrumentor().instrument()
        except Exception:
            logger.warning("otel_fastapi_instrument_failed", exc_info=True)

        try:
            HTTPXClientInstrumentor().instrument()
        except Exception:
            logger.warning("otel_httpx_instrument_failed", exc_info=True)

        logger.info("otel_initialized", endpoint=otel_endpoint, service=service_name)

    except ImportError:
        logger.warning(
            "opentelemetry packages not installed — run `pip install harness-runtime[otel]`",
        )
    except Exception:
        logger.warning("otel_init_failed", exc_info=True)


def _start_redis() -> None:
    redis_port = os.getenv("REDIS_PORT", "6379")
    try:
        subprocess.run(
            ["redis-server", "--daemonize", "yes", "--port", redis_port],
            check=True,
            capture_output=True,
        )
        time.sleep(0.5)
        logger.info("redis_server_started", port=redis_port)
    except Exception:
        logger.warning("redis_server_start_failed", exc_info=True)


def main() -> None:
    from deepagents.profiles.provider import ProviderProfile, register_provider_profile

    register_provider_profile(
        "openai",
        ProviderProfile(init_kwargs={"use_responses_api": False}),
    )

    _setup_otel()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(2)

    _start_redis()

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
