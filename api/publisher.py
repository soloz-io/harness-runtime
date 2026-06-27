import json
import time
import uuid
from typing import Any, Optional

import redis
import structlog

from core.event_publisher import EventPublisher

logger = structlog.get_logger(__name__)

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

        # Protocol event state
        self._seq = 0
        self._message_started = False
        self._block_started = False
        self._block_index = 0
        self._checkpoint_step = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event_id(self) -> str:
        return uuid.uuid4().hex

    def _write(self, data: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            r = get_redis_client()
            r.xadd(self._stream_key, {"data": json.dumps(data, default=str)})
        except Exception as e:
            logger.error("redis_xadd_failed", error=str(e), stream_key=self._stream_key)

    def _protocol_event(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "event",
            "event_id": self._event_id(),
            "seq": self._next_seq(),
            "method": method,
            "params": {
                "namespace": [],
                "timestamp": int(time.time() * 1000),
                "data": data,
            },
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        r = get_redis_client()
        r.xadd(self._stream_key, {"data": _SENTINEL})
        r.expire(self._stream_key, 60)

    # ---- Protocol event emission ----

    def publish_lifecycle_started(self, *, session_id: str, node: Optional[str] = None) -> None:
        data: dict[str, Any] = {"event": "started"}
        if node:
            data["node"] = node
        self._write(self._protocol_event("lifecycle", data))

    def publish_lifecycle_completed(self, *, session_id: str) -> None:
        self._write(self._protocol_event("lifecycle", {"event": "completed"}))

    def publish_lifecycle_failed(self, *, session_id: str, error: str) -> None:
        self._write(self._protocol_event("lifecycle", {"event": "failed", "error": error}))

    def publish_checkpoint(self, *, session_id: str) -> None:
        self._checkpoint_step += 1
        self._write(
            self._protocol_event(
                "checkpoints",
                {
                    "id": f"checkpoint_{self._seq}",
                    "step": self._checkpoint_step,
                },
            )
        )

    def publish_values(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        files: Optional[dict[str, Any]] = None,
    ) -> None:
        data: dict[str, Any] = {"messages": messages}
        if files:
            data["files"] = files
        self._write(self._protocol_event("values", data))

    def publish_system_init(
        self, *, session_id: str, model: str, tools: Optional[list[dict[str, Any]]] = None
    ) -> None:
        pass

    def publish_stream_event_text(self, *, session_id: str, text: str, index: int = 0) -> None:
        if not self._message_started:
            self._message_started = True
            self._block_index = 0
            self._write(
                self._protocol_event(
                    "messages",
                    {
                        "event": "message-start",
                        "role": "ai",
                        "id": f"msg_{session_id}_{self._seq}",
                    },
                )
            )

        if not self._block_started:
            self._block_started = True
            self._write(
                self._protocol_event(
                    "messages",
                    {
                        "event": "content-block-start",
                        "index": self._block_index,
                        "content": {"type": "text", "text": ""},
                    },
                )
            )

        self._write(
            self._protocol_event(
                "messages",
                {
                    "event": "content-block-delta",
                    "index": self._block_index,
                    "delta": {"type": "text-delta", "text": text},
                },
            )
        )

    def publish_assistant(
        self,
        *,
        session_id: str,
        model: str,
        content: list[dict[str, Any]],
        parent_tool_use_id: Optional[str] = None,
    ) -> None:
        if self._block_started:
            self._block_started = False
            self._write(
                self._protocol_event(
                    "messages",
                    {
                        "event": "content-block-finish",
                        "index": self._block_index,
                        "content": {"type": "text", "text": ""},
                    },
                )
            )

        for block in content:
            if block.get("type") == "tool_use":
                tool_call_id = block.get("id", f"call_{self._seq}")
                self._block_index += 1

                self._write(
                    self._protocol_event(
                        "messages",
                        {
                            "event": "content-block-start",
                            "index": self._block_index,
                            "content": {
                                "type": "tool_call",
                                "id": tool_call_id,
                                "name": block.get("name", ""),
                                "args": block.get("input", {}),
                            },
                        },
                    )
                )

                self._write(
                    self._protocol_event(
                        "messages",
                        {
                            "event": "content-block-finish",
                            "index": self._block_index,
                            "content": {
                                "type": "tool_call",
                                "id": tool_call_id,
                                "name": block.get("name", ""),
                                "args": block.get("input", {}),
                            },
                        },
                    )
                )

                self._write(
                    self._protocol_event(
                        "tools",
                        {
                            "event": "tool-started",
                            "tool_call_id": tool_call_id,
                            "tool_name": block.get("name", ""),
                            "input": block.get("input", {}),
                        },
                    )
                )

    def publish_tool_output_delta(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        delta: str,
    ) -> None:
        self._write(
            self._protocol_event(
                "tools",
                {
                    "event": "tool-output-delta",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "delta": delta,
                },
            )
        )

    def publish_tool_result(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
        is_error: bool = False,
    ) -> None:
        self._write(
            self._protocol_event(
                "tools",
                {
                    "event": "tool-finished",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "output": content,
                    "is_error": is_error,
                },
            )
        )

    def publish_message_finish(self) -> None:
        if self._block_started:
            self._block_started = False
            self._write(
                self._protocol_event(
                    "messages",
                    {
                        "event": "content-block-finish",
                        "index": self._block_index,
                        "content": {"type": "text", "text": ""},
                    },
                )
            )
        if self._message_started:
            self._write(
                self._protocol_event(
                    "messages",
                    {
                        "event": "message-finish",
                    },
                )
            )
        self._message_started = False
        self._block_started = False
        self._block_index = 0

    def publish_user_echo(self, *, session_id: str, content: list[dict[str, Any]]) -> None:
        pass

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
            self._protocol_event(
                "messages",
                {
                    "event": "message-finish",
                    "usage": {
                        "output_tokens": 0,
                        "input_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            )
        )
        self._message_started = False
        self._block_started = False

    def publish_control_response(
        self, *, request_id: str, subtype: str = "success", **extra: Any
    ) -> None:
        pass
