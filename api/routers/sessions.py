"""Session management endpoints — uses ``RuntimeServices`` singleton for DI."""

import asyncio
import json as _json
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
from core.services import get_services
from core.session import Session
from core.session.skills import SkillsError

logger = structlog.get_logger(__name__)

_SYSTEM_NOTICE_PREFIX = "[System Notification]"


def _notification_text(raw: str) -> str:
    """Extract human-readable text from a JSON notification payload.

    Returns the ``message`` field if present, then ``title``, otherwise the
    raw string itself.  Parsing errors are handled gracefully by returning the
    raw input.
    """
    try:
        payload = _json.loads(raw)
    except (TypeError, ValueError, _json.JSONDecodeError):
        return raw
    if isinstance(payload, dict):
        return payload.get("message") or payload.get("title") or raw
    return raw


router = APIRouter(tags=["sessions"])

_db_url: str = os.environ.get("DATABASE_URL", "")


@dataclass
class SessionState:
    session: Session
    publisher: SSEEventPublisher
    task: Optional[asyncio.Task[Any]] = None


def _trim_sentinel(session_id: str) -> None:
    """Remove stale sentinel entries from the Redis stream for a session.

    When a turn completes, the publisher writes a sentinel (``\x00end\x00``)
    to the stream.  Before starting a new turn we must remove it so the SSE
    event generator doesn't hit the old sentinel and terminate prematurely.
    """
    import redis as sync_redis

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = sync_redis.from_url(redis_url)
    key = _stream_key(session_id)
    try:
        entries = r.xrevrange(key, count=10)
        ids_to_delete: list[str | bytes] = []
        for entry_id, fields in entries:
            data_raw = fields.get(b"data", b"")
            if data_raw == _SENTINEL:
                ids_to_delete.append(entry_id)
        if ids_to_delete:
            r.xdel(key, *ids_to_delete)
            logger.info(
                "sentinel_trimmed",
                session_id=session_id,
                deleted_count=len(ids_to_delete),
            )
    except Exception as e:
        logger.warning("sentinel_trim_failed", session_id=session_id, error=str(e))
    finally:
        r.close()


def _get_session_store() -> dict[str, SessionState]:
    """Return the shared session store from ``RuntimeServices``."""
    return get_services().session_store  # type: ignore[return-value]


def _get_execution_manager() -> ExecutionManager:
    """Return the shared ``ExecutionManager`` from ``RuntimeServices``."""
    svc = get_services()
    assert svc.execution_manager is not None, "ExecutionManager not initialized"
    return svc.execution_manager


async def init_execution_manager_async() -> None:
    """Initialize ``RuntimeServices`` with an async ``ExecutionManager``."""
    svc = get_services()
    if svc.execution_manager is None:
        from api.publisher import SSEEventPublisher

        if not _db_url:
            logger.error("DATABASE_URL not set, starting without checkpointer")
            svc.execution_manager = await ExecutionManager.create_async(
                postgres_connection_string="",
                publisher=SSEEventPublisher("_init_"),
            )
        else:
            publisher = SSEEventPublisher("_init_")
            svc.execution_manager = await ExecutionManager.create_async(
                postgres_connection_string=_db_url,
                publisher=publisher,
            )
        logger.info("execution_manager_initialized")


def init_execution_manager() -> None:
    """Initialize ``RuntimeServices`` with a sync ``ExecutionManager``."""
    svc = get_services()
    if svc.execution_manager is None:
        from api.publisher import SSEEventPublisher

        if not _db_url:
            logger.error("DATABASE_URL not set, starting without checkpointer")
            svc.execution_manager = ExecutionManager(
                postgres_connection_string="",
                publisher=SSEEventPublisher("_init_"),
            )
        else:
            publisher = SSEEventPublisher("_init_")
            svc.execution_manager = ExecutionManager(
                postgres_connection_string=_db_url,
                publisher=publisher,
            )
        logger.info("execution_manager_initialized")


async def shutdown_execution_manager_async() -> None:
    """Shut down the ``ExecutionManager`` from ``RuntimeServices``."""
    svc = get_services()
    if svc.execution_manager is not None:
        await svc.execution_manager.aclose()
        svc.execution_manager = None
        logger.info("execution_manager_shutdown")


def shutdown_execution_manager() -> None:
    """Shut down the ``ExecutionManager`` from ``RuntimeServices``."""
    svc = get_services()
    if svc.execution_manager is not None:
        svc.execution_manager.close()
        svc.execution_manager = None
        logger.info("execution_manager_shutdown")


async def _run_turn_async(state: SessionState, user_content: str, role: str = "user") -> None:
    session = state.session
    publisher = state.publisher
    state.task = asyncio.current_task()
    try:
        await session.async_run_turn(user_content=user_content, publisher=publisher, role=role)
    except asyncio.CancelledError:
        logger.info("session_run_turn_cancelled", session_id=session.session_id)
        publisher.publish_result(
            session_id=session.session_id,
            subtype="cancelled",
            is_error=True,
            result="Turn cancelled by user",
        )
        raise
    except Exception as e:
        logger.error("session_run_turn_failed", error=str(e), traceback=traceback.format_exc())
        publisher.publish_result(
            session_id=session.session_id,
            subtype="error_during_execution",
            is_error=True,
            result=str(e),
        )
    finally:
        state.task = None
        publisher.close()


@router.get("/checkpoints")
async def list_checkpoints(session_id: str) -> dict[str, Any]:
    """List all LangGraph checkpoints for a session (thread)."""
    execution_manager = _get_execution_manager()
    checkpointer = execution_manager._async_checkpointer or execution_manager.checkpointer
    if checkpointer is None:
        return {"checkpoints": []}

    config = {"configurable": {"thread_id": session_id}}
    history: list[dict[str, Any]] = []

    try:
        if hasattr(checkpointer, "alist"):
            async for snapshot in checkpointer.alist(config):
                cfg = getattr(snapshot, "config", {}) or {}
                meta = getattr(snapshot, "metadata", {}) or {}
                created_at = getattr(snapshot, "created_at", None)
                if hasattr(created_at, "isoformat"):
                    created_at = created_at.isoformat()
                history.append(
                    {
                        "checkpoint_id": cfg.get("configurable", {}).get("checkpoint_id", ""),
                        "step": meta.get("step", -1),
                        "source": meta.get("source", ""),
                        "writes": list((meta.get("writes") or {}).keys())
                        if isinstance(meta.get("writes"), dict)
                        else [],
                        "created_at": created_at,
                    }
                )
        elif hasattr(checkpointer, "list"):
            for snapshot in checkpointer.list(config):
                cfg = getattr(snapshot, "config", {}) or {}
                meta = getattr(snapshot, "metadata", {}) or {}
                created_at = getattr(snapshot, "created_at", None)
                if hasattr(created_at, "isoformat"):
                    created_at = created_at.isoformat()
                history.append(
                    {
                        "checkpoint_id": cfg.get("configurable", {}).get("checkpoint_id", ""),
                        "step": meta.get("step", -1),
                        "source": meta.get("source", ""),
                        "writes": list((meta.get("writes") or {}).keys())
                        if isinstance(meta.get("writes"), dict)
                        else [],
                        "created_at": created_at,
                    }
                )
    except Exception as e:
        logger.warning("list_checkpoints_failed", session_id=session_id, error=str(e))

    return {"checkpoints": history}


@router.post("/session/{session_id}/message")
async def handle_message(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message", "")
    agent_definition = body.get("agent_definition")
    input_payload = body.get("input_payload", {})
    resume_payload = body.get("resume_payload")
    restore_checkpoint_id = body.get("restore_checkpoint_id")
    workspace_id = body.get("workspace_id") or os.environ.get("WORKSPACE_ID")
    app_id = body.get("app_id")
    role = body.get("role", "user")

    if not workspace_id:
        logger.warning(
            "workspace_id not provided — ArtifactBackend will be unavailable "
            "(set workspace_id in POST body or WORKSPACE_ID env var)"
        )
        raise HTTPException(
            status_code=400,
            detail="workspace_id is required (set in POST body or WORKSPACE_ID env var)",
        )

    session_store = _get_session_store()
    execution_manager = _get_execution_manager()

    if restore_checkpoint_id:
        from core.checkpoint_restore import apply_checkpoint_restore

        checkpointer = execution_manager._async_checkpointer or execution_manager.checkpointer
        try:
            await apply_checkpoint_restore(checkpointer, session_id, restore_checkpoint_id)
        except Exception as e:
            logger.error("checkpoint_restore_failed", session_id=session_id, error=str(e))
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Invalidate in-memory session so next turn starts fresh from restored checkpoint
        session_store.pop(session_id, None)

    # System messages: write the notification row (for the audio-player UI),
    # then fall through to a user-role graph turn so the agent is informed.
    if role == "system" and message:
        from core.message_writer import write_chat_messages

        system_msg = {"type": "system", "content": message}
        pool = getattr(_get_execution_manager(), "_pool", None)
        if pool is not None:
            write_chat_messages(pool, session_id, [system_msg], offset=0, source="notification")
            logger.info(
                "system_message_written",
                session_id=session_id,
                source="notification",
                content_preview=str(message)[:120],
            )
        else:
            logger.warning("system_message_no_pool", session_id=session_id)

        session_store = _get_session_store()
        if session_id in session_store:
            fresh_publisher = SSEEventPublisher(session_id)
            fresh_publisher.publish_values(
                session_id=session_id,
                messages=[{"type": "system", "content": message}],
                files=None,
            )

        # Feed the agent a user-role message so it knows the job completed.
        # Fall through to initialize session state if needed and execute turn.
        notice_text = _notification_text(message)
        if notice_text:
            message = f"{_SYSTEM_NOTICE_PREFIX} {notice_text}"
            role = "user"
        else:
            return {"success": True}

    if session_id in session_store:
        state = session_store[session_id]
        if resume_payload:
            state.session.initialize(resume_payload=resume_payload)

        _trim_sentinel(session_id)
        state.publisher = SSEEventPublisher(session_id)
    else:
        _trim_sentinel(session_id)
        publisher = SSEEventPublisher(session_id)
        try:
            session = Session(
                agent_definition=agent_definition or {},
                input_payload=input_payload,
                execution_manager=execution_manager,
                publisher=publisher,
                session_id=session_id,
                workspace_id=workspace_id,
                app_id=app_id,
            )
        except (ValueError, SkillsError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if resume_payload:
            session.initialize(resume_payload=resume_payload)
        state = SessionState(session=session, publisher=publisher)
        session_store[session_id] = state
        logger.info("session_initialized", session_id=session_id)

    if message or resume_payload:
        state.task = asyncio.create_task(_run_turn_async(state, message, role))

    return {"success": True}


@router.post("/session/{session_id}/cancel")
async def cancel_session(session_id: str) -> dict[str, Any]:
    session_store = _get_session_store()
    if session_id in session_store:
        state = session_store[session_id]
        if state.task and not state.task.done():
            state.task.cancel()
            logger.info("session_turn_cancelled", session_id=session_id)
            return {"success": True, "cancelled": True}
    return {"success": True, "cancelled": False}


@router.get("/event")
async def stream_events(
    session_id: Optional[str] = None,
    last_event_id: str = "0",
) -> EventSourceResponse:
    session_store = _get_session_store()

    if session_id:
        deadline = time.time() + 30
        while session_id not in session_store:
            if time.time() > deadline:
                raise HTTPException(
                    status_code=404, detail=f"Session {session_id} not found within timeout"
                )
            await asyncio.sleep(0.1)

    if not session_store:
        raise HTTPException(status_code=404, detail="No active sessions")

    if session_id and session_id not in session_store:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    resolved_id = session_id or list(session_store.keys())[-1]

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        key = _stream_key(resolved_id)
        last_id = last_event_id
        try:
            while True:
                try:
                    result = await r.xread({key: last_id}, count=10, block=2000)
                except asyncio.CancelledError:
                    logger.warning(
                        "event_generator_cancelled", session_id=resolved_id, last_id=last_id
                    )
                    raise
                except Exception as e:
                    logger.warning("event_xread_error", error=str(e))
                    await asyncio.sleep(0.5)
                    continue

                if not result:
                    yield {"event": "ping", "data": ""}
                    continue

                for _stream_name, entries in result:
                    for entry_id, fields in entries:
                        if isinstance(fields, dict):
                            fields_map: dict[bytes, bytes] = fields
                        else:
                            fields_map = {
                                fields[i]: fields[i + 1] for i in range(0, len(fields), 2)
                            }
                        data_raw = fields_map.get(b"data", b"")
                        entry_id_str = (
                            entry_id.decode("utf-8")
                            if isinstance(entry_id, bytes)
                            else str(entry_id)
                        )
                        if data_raw == _SENTINEL:
                            last_id = entry_id_str
                            continue
                        data_str = data_raw.decode("utf-8")
                        import json

                        try:
                            parsed = json.loads(data_str)
                            method = parsed.get("method", "unknown")
                            seq = parsed.get("seq", -1)
                            ptype = parsed.get("type", "unknown")

                            logger.info(
                                "sse_yield",
                                session_id=resolved_id,
                                type=ptype,
                                method=method,
                                seq=seq,
                                keys=list(parsed.keys()),
                            )
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.info(
                                "sse_yield_raw",
                                session_id=resolved_id,
                                error=str(e),
                                data_preview=data_str[:200],
                            )
                        yield {"event": "message", "data": data_str, "id": entry_id_str}
                        last_id = entry_id_str
        except asyncio.CancelledError:
            logger.warning(
                "event_generator_cancelled_outer",
                session_id=resolved_id,
                last_id=last_id,
            )
            raise
        finally:
            logger.info(
                "event_generator_exit",
                session_id=resolved_id,
                last_id=last_id,
                has_stream=r is not None,
            )
            try:
                await r.aclose()
            except Exception:
                pass

    return EventSourceResponse(event_generator())
