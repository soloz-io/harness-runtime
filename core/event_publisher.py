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
    def publish_system_init(self, *, session_id: str, model: str,
                            tools: Optional[list[dict[str, Any]]] = None) -> None:
        ...

    @abstractmethod
    def publish_assistant(self, *, session_id: str, model: str,
                          content: list[dict[str, Any]],
                          parent_tool_use_id: Optional[str] = None) -> None:
        ...

    @abstractmethod
    def publish_user_echo(self, *, session_id: str,
                          content: list[dict[str, Any]]) -> None:
        ...

    @abstractmethod
    def publish_stream_event_text(self, *, session_id: str,
                                  text: str, index: int = 0) -> None:
        ...

    @abstractmethod
    def publish_result(self, *, session_id: str, subtype: str = "success",
                       duration_ms: int = 0, is_error: bool = False,
                       num_turns: int = 1, result: Optional[str] = None) -> None:
        ...

    @abstractmethod
    def publish_control_response(self, *, request_id: str,
                                 subtype: str = "success", **extra: Any) -> None:
        ...


class StdioPublisher(EventPublisher):
    def _write(self, frame: OutgoingFrame) -> None:
        sys.stdout.write(json.dumps(frame_to_dict(frame), default=str) + "\n")
        sys.stdout.flush()

    def publish_system_init(self, *, session_id: str, model: str,
                            tools: Optional[list[dict[str, Any]]] = None) -> None:
        self._write(SystemInitFrame(
            session_id=session_id,
            model=model,
            tools=tools or [],
        ))

    def publish_assistant(self, *, session_id: str, model: str,
                          content: list[dict[str, Any]],
                          parent_tool_use_id: Optional[str] = None) -> None:
        self._write(AssistantFrame.build(
            session_id=session_id,
            model=model,
            content=content,
            parent_tool_use_id=parent_tool_use_id,
        ))

    def publish_user_echo(self, *, session_id: str,
                          content: list[dict[str, Any]]) -> None:
        self._write(UserEchoFrame.build(
            session_id=session_id,
            content=content,
        ))

    def publish_stream_event_text(self, *, session_id: str,
                                  text: str, index: int = 0) -> None:
        self._write(StreamEventFrame.text_delta(
            session_id=session_id,
            text=text,
            index=index,
        ))

    def publish_result(self, *, session_id: str, subtype: str = "success",
                       duration_ms: int = 0, is_error: bool = False,
                       num_turns: int = 1, result: Optional[str] = None) -> None:
        self._write(ResultFrame(
            subtype=subtype,
            session_id=session_id,
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            is_error=is_error,
            num_turns=num_turns,
            result=result,
        ))

    def publish_control_response(self, *, request_id: str,
                                 subtype: str = "success", **extra: Any) -> None:
        self._write(ControlResponseFrame.success(
            request_id=request_id,
            **extra,
        ))
