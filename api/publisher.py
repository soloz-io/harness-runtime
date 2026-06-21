import json
from typing import Any, Optional

import redis

from core.event_publisher import EventPublisher
from models.frames import (
    AssistantFrame,
    ControlResponseFrame,
    OutgoingFrame,
    ResultFrame,
    StreamEventFrame,
    SystemInitFrame,
    UserEchoFrame,
    frame_to_dict,
)

_redis_client: Optional[redis.Redis] = None
_SENTINEL = b"\x00end\x00"


def set_redis_client(client: redis.Redis) -> None:
    global _redis_client
    _redis_client = client


def get_redis_client() -> redis.Redis:
    assert _redis_client is not None, "Redis not initialized"
    return _redis_client


def _stream_key(session_id: str) -> str:
    return f"session:{session_id}:events"


class SSEEventPublisher(EventPublisher):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._stream_key = _stream_key(session_id)
        self._closed = False

    def _write(self, frame: OutgoingFrame) -> None:
        if self._closed:
            return
        r = get_redis_client()
        r.xadd(self._stream_key, {"data": json.dumps(frame_to_dict(frame), default=str)})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        r = get_redis_client()
        r.xadd(self._stream_key, {"data": _SENTINEL})
        r.expire(self._stream_key, 60)

    def publish_system_init(
        self, *, session_id: str, model: str, tools: Optional[list[dict[str, Any]]] = None
    ) -> None:
        self._write(SystemInitFrame(session_id=session_id, model=model, tools=tools or []))

    def publish_assistant(
        self,
        *,
        session_id: str,
        model: str,
        content: list[dict[str, Any]],
        parent_tool_use_id: Optional[str] = None,
    ) -> None:
        self._write(
            AssistantFrame.build(
                session_id=session_id,
                model=model,
                content=content,
                parent_tool_use_id=parent_tool_use_id,
            )
        )

    def publish_user_echo(self, *, session_id: str, content: list[dict[str, Any]]) -> None:
        self._write(UserEchoFrame.build(session_id=session_id, content=content))

    def publish_stream_event_text(self, *, session_id: str, text: str, index: int = 0) -> None:
        self._write(StreamEventFrame.text_delta(session_id=session_id, text=text, index=index))

    def publish_result(
        self,
        *,
        session_id: str,
        subtype: str = "success",
        duration_ms: int = 0,
        is_error: bool = False,
        num_turns: int = 1,
        result: Optional[str] = None,
        structured_response: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        interrupt: Optional[dict[str, Any]] = None,
    ) -> None:
        self._write(
            ResultFrame(
                subtype=subtype,
                session_id=session_id,
                duration_ms=duration_ms,
                duration_api_ms=duration_ms,
                is_error=is_error,
                num_turns=num_turns,
                result=result,
                structured_response=structured_response,
                files=files,
                interrupt=interrupt,
            )
        )

    def publish_control_response(
        self, *, request_id: str, subtype: str = "success", **extra: Any
    ) -> None:
        self._write(ControlResponseFrame.success(request_id=request_id, **extra))
