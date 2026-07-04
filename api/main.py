import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI

from api.publisher import set_redis_client
from api.registry import SandboxRegistry
from api.routers import health, sessions

logger = structlog.get_logger(__name__)

_sandbox_registry: SandboxRegistry | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    load_dotenv()
    logger.info("harness_runtime_http_starting")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.Redis.from_url(redis_url, decode_responses=False)
    r.ping()
    set_redis_client(r)
    logger.info("redis_client_initialized")

    await sessions.init_execution_manager_async()
    logger.info("harness_runtime_http_started")

    global _sandbox_registry
    _sandbox_registry = SandboxRegistry.create_from_env()
    if _sandbox_registry is not None:
        _sandbox_registry.register()
        _sandbox_registry.start_heartbeat()

    yield

    logger.info("harness_runtime_http_shutting_down")
    if _sandbox_registry is not None:
        _sandbox_registry.close()
    await sessions.shutdown_execution_manager_async()


app = FastAPI(title="Harness Runtime HTTP", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
