"""FastAPI application entry point — bootstraps ``RuntimeServices``.

Replaces module-level globals (``_execution_manager``, ``_session_store``,
``_redis_client``) with a ``RuntimeServices`` container stored on
``app.state.services``.
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI

from api.publisher import set_redis_client
from api.routers import computer, health, sessions, workspace_files
from core.computer import computer_app
from core.computer import supervisor as computer_supervisor
from core.publishers.event_publisher import StdioPublisher
from core.services import RuntimeServices, init_services

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    load_dotenv()
    logger.info("harness_runtime_http_starting")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.Redis.from_url(redis_url, decode_responses=False)
    r.ping()
    set_redis_client(r)
    logger.info("redis_client_initialized")

    # Bootstrap RuntimeServices — execution_manager is populated below
    init_services(
        RuntimeServices(
            publisher=StdioPublisher(),  # placeholder, replaced per-turn
            execution_manager=None,  # type: ignore[arg-type]
            redis_client=r,
        )
    )

    await sessions.init_execution_manager_async()
    # The session's computer app, when its definition declared one and the
    # image carries it (waypoint ADR-050).
    computer_supervisor.start(computer_app())
    logger.info("harness_runtime_http_started")

    yield

    logger.info("harness_runtime_http_shutting_down")
    await computer_supervisor.stop()
    await sessions.shutdown_execution_manager_async()


app = FastAPI(title="Harness Runtime HTTP", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(workspace_files.router)
# The session's computer (waypoint ADR-050): /computer/* to the image's app.
app.include_router(computer.router)
