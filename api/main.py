"""
FastAPI Application for Agent Executor Service.

This module implements the main FastAPI application that serves as the entry point
for the Agent Executor service. It handles incoming CloudEvents from NATS JetStream,
orchestrates agent execution, and emits result CloudEvents.

The application provides:
- CloudEvent ingestion endpoint (POST /)
- Health check endpoints (GET /health, GET /ready)
- HTTP API endpoints for IDE Orchestrator integration
- WebSocket streaming endpoints
- Prometheus metrics endpoint
- Dependency injection for all service components
- Structured logging and OpenTelemetry tracing

Architecture:
    NATS JetStream → NATS Consumer → Parse CloudEvent → Build Graph → Execute → Emit Result
    IDE Orchestrator → HTTP/WebSocket API → Agent Execution → Real-time Streaming

References:
    - Requirements: Req. 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 3.1, 5.1, 5.3, 5.5, NFR-3.1, NFR-4.1
    - Design: Section 2.11 (Internal Component Architecture), Section 3.1 (API Layer)
    - Tasks: Task 8 (FastAPI Application and Endpoint)
"""

import asyncio
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import structlog
from dotenv import load_dotenv
from fastapi import Depends, FastAPI

# Import OpenTelemetry instrumentation
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    import warnings

    warnings.warn(
        "OpenTelemetry FastAPI instrumentation not available. "
        "Install opentelemetry-instrumentation-fastapi for tracing support.",
        ImportWarning,
    )

# Import service components
from core.executor import ExecutionManager
from services.cloudevents import CloudEventEmitter
from services.redis import RedisClient

# Import routers
from api.routers import cloudevents, deepagents, health, metrics

# Import dependencies
from api.dependencies import (
    get_cloudevent_emitter,
    get_execution_manager,
    get_graph_builder,
    get_nats_consumer,
    get_redis_client,
    set_cloudevent_emitter,
    set_execution_manager,
    set_nats_consumer,
    set_redis_client,
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)

logger = structlog.get_logger(__name__)

# Configure OpenTelemetry SDK with OTLP exporter
if OTEL_AVAILABLE:
    # Read service name from environment variable or use default
    service_name = os.getenv("OTEL_SERVICE_NAME", "agent-executor-service")

    # Create resource with service name
    resource = Resource(attributes={SERVICE_NAME: service_name})

    # Create TracerProvider with resource
    tracer_provider = TracerProvider(resource=resource)

    # Configure OTLP exporter (reads from OTEL_EXPORTER_OTLP_ENDPOINT env var)
    # Default endpoint: http://localhost:4317
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)

    # Add BatchSpanProcessor for efficient span export
    span_processor = BatchSpanProcessor(otlp_exporter)
    tracer_provider.add_span_processor(span_processor)

    # Set the global tracer provider
    trace.set_tracer_provider(tracer_provider)

    # Get tracer for this module
    tracer = trace.get_tracer(__name__)

    logger.info(
        "opentelemetry_sdk_configured", service_name=service_name, otlp_endpoint=otlp_endpoint
    )
else:
    tracer = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI lifespan context manager for startup and shutdown events.

    Startup:
    - Validates required environment variables
    - Reads credentials from environment variables (populated by Kubernetes Secrets)
    - Initializes RedisClient, ExecutionManager, CloudEventEmitter
    - Starts NATS consumer as background task
    - Sets up OpenTelemetry instrumentation

    Shutdown:
    - Stops NATS consumer
    - Closes all service connections
    - Cleans up resources

    Raises:
        RuntimeError: If required environment variables are missing
        Exception: If any service initialization fails

    References:
        - Requirements: Req. 1.1, 1.2, 2.1, 14.1, 14.2, NFR-3.1
        - Tasks: Task 1.1, 1.2, 1.3
    """
    # Load .env file if it exists (for local development/testing)
    # Use explicit path to ensure .env is found regardless of working directory
    env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=env_path)

    logger.info("deepagents_runtime_service_starting")

    # Service instances
    redis_client = None
    execution_manager = None
    cloudevent_emitter = None
    nats_consumer = None
    nats_consumer_task = None

    try:
        # Validate required environment variables
        required_env_vars = ["DATABASE_URL", "DRAGONFLY_HOST"]
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]

        if missing_vars:
            error_msg = f"Missing required environment variables: {', '.join(missing_vars)}"
            logger.error("startup_validation_failed", missing_variables=missing_vars, message=error_msg)
            raise RuntimeError(error_msg)

        # Use DATABASE_URL for application, but also set POSTGRES_URI for LangGraph checkpointer
        database_url = os.getenv("DATABASE_URL")
        
        # LangGraph CLI expects POSTGRES_URI environment variable for checkpointer
        # Set it from DATABASE_URL if not already set
        if not os.getenv("POSTGRES_URI"):
            os.environ["POSTGRES_URI"] = database_url

        # Build Dragonfly (Redis-compatible) configuration from environment variables
        dragonfly_config = {
            "host": os.getenv("DRAGONFLY_HOST"),
            "port": int(os.getenv("DRAGONFLY_PORT", "6379")),
            "password": os.getenv("DRAGONFLY_PASSWORD"),  # May be None if no auth
        }

        logger.info(
            "credentials_loaded_from_environment",
            database_url_set=bool(database_url),
            postgres_uri_set=bool(os.getenv("POSTGRES_URI")),
            dragonfly_host=dragonfly_config["host"],
            dragonfly_port=dragonfly_config["port"],
            nats_url=os.getenv("NATS_URL", "nats://nats.nats.svc:4222"),
        )

        # Use DATABASE_URL for ExecutionManager connection string
        postgres_connection_string = database_url
        logger.info("postgres_connection_string_loaded")

        # Initialize RedisClient (connects to Dragonfly)
        logger.info("initializing_redis_client")
        redis_kwargs = {"host": dragonfly_config["host"], "port": dragonfly_config["port"]}
        if dragonfly_config.get("password"):
            redis_kwargs["password"] = dragonfly_config["password"]

        redis_client = RedisClient(**redis_kwargs)
        set_redis_client(redis_client)
        logger.info("redis_client_initialized")

        # Initialize ExecutionManager
        logger.info("initializing_execution_manager")
        execution_manager = ExecutionManager(
            redis_client=redis_client, postgres_connection_string=postgres_connection_string
        )
        set_execution_manager(execution_manager)
        logger.info("execution_manager_initialized")

        # Initialize CloudEventEmitter
        logger.info("initializing_cloudevent_emitter")
        cloudevent_emitter = CloudEventEmitter()
        set_cloudevent_emitter(cloudevent_emitter)
        logger.info("cloudevent_emitter_initialized")

        # Validate LLM API keys are available as environment variables
        # LangChain/LangGraph expects API keys to be available as env vars
        # These are populated by Kubernetes Secrets managed by External Secrets Operator
        logger.info("validating_llm_api_keys")
        if os.getenv("OPENAI_API_KEY"):
            logger.info("openai_api_key_available")
        else:
            logger.warning("openai_api_key_not_set")

        if os.getenv("ANTHROPIC_API_KEY"):
            logger.info("anthropic_api_key_available")
        else:
            logger.warning("anthropic_api_key_not_set")

        # Initialize NATS consumer
        logger.info("initializing_nats_consumer")
        from services.nats_consumer import NATSConsumer

        nats_consumer = NATSConsumer(
            nats_url=os.getenv("NATS_URL", "nats://nats.nats.svc:4222"),
            stream_name="AGENT_EXECUTION",
            consumer_group="agent-executor-workers",
            execution_manager=execution_manager,
            cloudevent_emitter=cloudevent_emitter,
        )
        set_nats_consumer(nats_consumer)

        # Start NATS consumer as background task
        logger.info("starting_nats_consumer_background_task")
        nats_consumer_task = asyncio.create_task(nats_consumer.start())
        
        # Wait for NATS connection to be established
        # This ensures the connection is available for tests and health checks
        connection_ready = await nats_consumer.wait_for_connection(timeout=10.0)
        if connection_ready:
            logger.info("nats_consumer_started")
        else:
            logger.warning("nats_consumer_connection_timeout", message="NATS connection not established within timeout")
            logger.info("nats_consumer_started")

        logger.info("deepagents_runtime_service_started", message="All services initialized successfully")

        # Yield control to the application
        yield

    except Exception as e:
        logger.error(
            "startup_failed",
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc(),
        )
        raise

    finally:
        # Shutdown: Clean up resources
        logger.info("deepagents_runtime_service_shutting_down")

        # Stop NATS consumer
        if nats_consumer:
            logger.info("stopping_nats_consumer")
            await nats_consumer.stop()
            logger.info("nats_consumer_stopped")

        if nats_consumer_task and not nats_consumer_task.done():
            logger.info("cancelling_nats_consumer_task")
            nats_consumer_task.cancel()
            try:
                await nats_consumer_task
            except asyncio.CancelledError:
                logger.info("nats_consumer_task_cancelled")

        if execution_manager:
            execution_manager.close()
            logger.info("execution_manager_closed")

        if redis_client:
            redis_client.close()
            logger.info("redis_client_closed")

        logger.info("deepagents_runtime_service_stopped")


# Initialize FastAPI application
app = FastAPI(
    title="Agent Executor Service",
    description="Event-Driven LangGraph Agent Execution Service with KEDA Autoscaling",
    version="0.1.0",
    lifespan=lifespan,
)

# Set up OpenTelemetry instrumentation
if OTEL_AVAILABLE:
    FastAPIInstrumentor.instrument_app(app)
    logger.info("opentelemetry_instrumentation_enabled")

# Include routers
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(cloudevents.router)
app.include_router(deepagents.router)


# Application entry point for uvicorn
def main() -> None:
    """
    Entry point for running the application with uvicorn.

    Usage:
        python -m deepagents_runtime.api.main
        or
        uvicorn deepagents_runtime.api.main:app --host 0.0.0.0 --port 8080
    """
    import uvicorn

    # Allow port to be configured via environment variable
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("deepagents_runtime.api.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()