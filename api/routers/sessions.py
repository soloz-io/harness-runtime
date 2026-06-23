import asyncio
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from api.publisher import _SENTINEL, SSEEventPublisher, _stream_key
from core.executor import ExecutionManager
from core.session import Session

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions"])

_db_url: str = os.environ.get("DATABASE_URL", "")


@dataclass
class SessionState:
    session: Session
    publisher: SSEEventPublisher


_session_store: dict[str, SessionState] = {}
_execution_manager: Optional[ExecutionManager] = None


async def init_execution_manager_async() -> None:
    global _execution_manager
    if _execution_manager is None:
        if not _db_url:
            logger.error("DATABASE_URL not set, starting without checkpointer")
            _execution_manager = await ExecutionManager.create_async(
                postgres_connection_string="",
                publisher=SSEEventPublisher("_init_"),
            )
        else:
            publisher = SSEEventPublisher("_init_")
            _execution_manager = await ExecutionManager.create_async(
                postgres_connection_string=_db_url,
                publisher=publisher,
            )
        logger.info("execution_manager_initialized")


def init_execution_manager() -> None:
    global _execution_manager
    if _execution_manager is None:
        if not _db_url:
            logger.error("DATABASE_URL not set, starting without checkpointer")
            _execution_manager = ExecutionManager(
                postgres_connection_string="",
                publisher=SSEEventPublisher("_init_"),
            )
        else:
            publisher = SSEEventPublisher("_init_")
            _execution_manager = ExecutionManager(
                postgres_connection_string=_db_url,
                publisher=publisher,
            )
        logger.info("execution_manager_initialized")


async def _run_turn_async(
    session: Session, publisher: SSEEventPublisher, user_content: str
) -> None:
    try:
        await session.async_run_turn(user_content=user_content, publisher=publisher)
    except Exception as e:
        logger.error("session_run_turn_failed", error=str(e), traceback=traceback.format_exc())
        publisher.publish_result(
            session_id=session.session_id,
            subtype="error_during_execution",
            is_error=True,
            result=str(e),
        )
    finally:
        publisher.close()


@router.post("/session/{session_id}/message")
async def handle_message(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message", "")
    agent_definition = body.get("agent_definition")
    input_payload = body.get("input_payload", {})
    resume_payload = body.get("resume_payload")

    if session_id in _session_store:
        state = _session_store[session_id]
        if resume_payload:
            state.session.initialize(resume_payload=resume_payload)
            # Create a fresh publisher for the resumed turn — the old one was closed
            # when the first turn completed (sentinel written). A new publisher ensures
            # events from the resumed turn reach Redis Streams.
            state.publisher = SSEEventPublisher(session_id)
    else:
        publisher = SSEEventPublisher(session_id)
        if not _execution_manager:
            raise HTTPException(status_code=503, detail="ExecutionManager not initialized")
        try:
            session = Session(
                agent_definition=agent_definition or {},
                input_payload=input_payload,
                execution_manager=_execution_manager,
                publisher=publisher,
                session_id=session_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if resume_payload:
            session.initialize(resume_payload=resume_payload)
        state = SessionState(session=session, publisher=publisher)
        _session_store[session_id] = state
        logger.info("session_initialized", session_id=session_id)

    if message or resume_payload:
        asyncio.create_task(_run_turn_async(state.session, state.publisher, message))

    return {"success": True}


@router.get("/event")
async def stream_events(
    session_id: Optional[str] = None,
    last_event_id: str = "0",
) -> EventSourceResponse:
    if session_id:
        deadline = time.time() + 30
        while session_id not in _session_store:
            if time.time() > deadline:
                raise HTTPException(
                    status_code=404, detail=f"Session {session_id} not found within timeout"
                )
            await asyncio.sleep(0.1)

    if not _session_store:
        raise HTTPException(status_code=404, detail="No active sessions")

    if session_id and session_id not in _session_store:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    resolved_id = session_id or list(_session_store.keys())[-1]

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        key = _stream_key(resolved_id)
        last_id = last_event_id

        while True:
            try:
                result = await r.xread({key: last_id}, count=10, block=2000)
            except Exception as e:
                logger.warning("event_xread_error", error=str(e))
                await asyncio.sleep(0.5)
                continue

            if not result:
                yield {"event": "ping", "data": ""}
                continue

            for _stream_name, entries in result:
                for entry_id, fields in entries:
                    data_raw = fields.get(b"data", b"")
                    if data_raw == _SENTINEL:
                        logger.info("event_stream_sentinel_received", session_id=resolved_id)
                        return
                    data_str = data_raw.decode("utf-8")
                    import json

                    try:
                        parsed = json.loads(data_str)
                        method = parsed.get("method", "unknown")
                        seq = parsed.get("seq", -1)

                        summary = data_str[:200]
                        logger.info(
                            "event_forwarded",
                            session_id=resolved_id,
                            method=method,
                            seq=seq,
                            data_preview=summary,
                        )
                    except (json.JSONDecodeError, TypeError):
                        logger.info(
                            "event_forwarded_raw",
                            session_id=resolved_id,
                            data_preview=data_str[:200],
                        )
                    yield {"event": "message", "data": data_str, "id": entry_id}
                    last_id = entry_id

    return EventSourceResponse(event_generator())
