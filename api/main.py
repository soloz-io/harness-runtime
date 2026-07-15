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
from api.routers import health, sessions
from core.event_publisher import StdioPublisher
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
    logger.info("harness_runtime_http_started")

    yield

    logger.info("harness_runtime_http_shutting_down")
    await sessions.shutdown_execution_manager_async()


app = FastAPI(title="Harness Runtime HTTP", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
