from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI

from api.routers import health, sessions

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    load_dotenv()
    logger.info("harness_runtime_http_starting")
    sessions.init_execution_manager()
    logger.info("harness_runtime_http_started")
    yield
    logger.info("harness_runtime_http_shutting_down")


app = FastAPI(title="Harness Runtime HTTP", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
