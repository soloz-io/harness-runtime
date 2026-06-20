import asyncio
import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

import structlog
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from api.publisher import SSEEventPublisher
from core.executor import ExecutionManager
from core.session import Session

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions"])

_executor = ThreadPoolExecutor(max_workers=4)

_db_url: str = os.environ.get("DATABASE_URL", "")


@dataclass
class SessionState:
    session: Session
    publisher: SSEEventPublisher


_session_store: dict[str, SessionState] = {}
_execution_manager: Optional[ExecutionManager] = None


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


def _run_turn(session: Session, publisher: SSEEventPublisher, user_content: str) -> None:
    try:
        session.run_turn(user_content=user_content)
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
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    agent_definition = body.get("agent_definition")
    input_payload = body.get("input_payload", {})

    if session_id in _session_store:
        state = _session_store[session_id]
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
        state = SessionState(session=session, publisher=publisher)
        _session_store[session_id] = state
        logger.info("session_initialized", session_id=session_id)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_turn, state.session, state.publisher, message)

    return {"success": True}


@router.get("/event")
async def stream_events() -> EventSourceResponse:
    if not _session_store:
        raise HTTPException(status_code=404, detail="No active sessions")

    latest_state = list(_session_store.values())[-1]
    publisher = latest_state.publisher

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        while True:
            event_data = await publisher.next_event()
            if event_data is None:
                break
            yield {"event": "message", "data": json.dumps(event_data)}

    return EventSourceResponse(event_generator())
