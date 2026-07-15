"""Event publisher for streaming agent execution events to clients.

Provides abstract + concrete publishers (Redis, console) that emit
tool calls, model responses, interrupts, and errors during graph execution.
"""

import json
import sys
from abc import ABC, abstractmethod
from typing import Any, Optional

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


class EventPublisher(ABC):
    @abstractmethod
    def publish_lifecycle_started(self, *, session_id: str, node: Optional[str] = None) -> None: ...

    @abstractmethod
    def publish_lifecycle_completed(self, *, session_id: str) -> None: ...

    @abstractmethod
    def publish_lifecycle_failed(self, *, session_id: str, error: str) -> None: ...

    @abstractmethod
    def publish_checkpoint(self, *, session_id: str) -> None: ...

    @abstractmethod
    def publish_values(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        files: Optional[dict[str, Any]] = None,
    ) -> None: ...

    @abstractmethod
    def publish_system_init(
        self, *, session_id: str, model: str, tools: Optional[list[dict[str, Any]]] = None
    ) -> None: ...

    @abstractmethod
    def publish_assistant(
        self,
        *,
        session_id: str,
        model: str,
        content: list[dict[str, Any]],
        parent_tool_use_id: Optional[str] = None,
    ) -> None: ...

    @abstractmethod
    def publish_user_echo(self, *, session_id: str, content: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    def publish_tool_result(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
        is_error: bool = False,
    ) -> None: ...

    @abstractmethod
    def publish_tool_output_delta(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        delta: str,
    ) -> None: ...

    @abstractmethod
    def publish_stream_event_text(self, *, session_id: str, text: str, index: int = 0) -> None: ...

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    def publish_control_response(
        self, *, request_id: str, subtype: str = "success", **extra: Any
    ) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def publish_message_finish(self) -> None:  # noqa: B027
        """Finalize the current message and reset streaming state.

        Closes any open content block, emits message-finish, and resets
        internal state so the next ``publish_stream_event_text`` call
        starts a fresh message. Default no-op.
        """


class StdioPublisher(EventPublisher):
    def _write(self, frame: OutgoingFrame) -> None:
        sys.stdout.write(json.dumps(frame_to_dict(frame), default=str) + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        pass

    def publish_lifecycle_started(self, *, session_id: str, node: Optional[str] = None) -> None:
        pass

    def publish_lifecycle_completed(self, *, session_id: str) -> None:
        pass

    def publish_lifecycle_failed(self, *, session_id: str, error: str) -> None:
        pass

    def publish_checkpoint(self, *, session_id: str) -> None:
        pass

    def publish_values(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        files: Optional[dict[str, Any]] = None,
    ) -> None:
        pass

    def publish_system_init(
        self, *, session_id: str, model: str, tools: Optional[list[dict[str, Any]]] = None
    ) -> None:
        self._write(
            SystemInitFrame(
                session_id=session_id,
                model=model,
                tools=tools or [],
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
        self._write(
            AssistantFrame.build(
                session_id=session_id,
                model=model,
                content=content,
                parent_tool_use_id=parent_tool_use_id,
            )
        )

    def publish_user_echo(self, *, session_id: str, content: list[dict[str, Any]]) -> None:
        self._write(
            UserEchoFrame.build(
                session_id=session_id,
                content=content,
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
        pass

    def publish_tool_output_delta(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        delta: str,
    ) -> None:
        pass

    def publish_stream_event_text(self, *, session_id: str, text: str, index: int = 0) -> None:
        self._write(
            StreamEventFrame.text_delta(
                session_id=session_id,
                text=text,
                index=index,
            )
        )

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
        self._write(
            ControlResponseFrame.success(
                request_id=request_id,
                **extra,
            )
        )
