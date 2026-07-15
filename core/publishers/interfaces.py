"""Segregated publisher interfaces for different event domains.

Follows Interface Segregation Principle — handlers depend only on
the specific publisher interface they need instead of the full
14-method ``EventPublisher`` ABC.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class LifecyclePublisher(ABC):
    """Publish agent lifecycle events (started / completed / failed)."""

    @abstractmethod
    def publish_lifecycle_started(self, *, session_id: str, node: Optional[str] = None) -> None: ...

    @abstractmethod
    def publish_lifecycle_completed(self, *, session_id: str) -> None: ...

    @abstractmethod
    def publish_lifecycle_failed(self, *, session_id: str, error: str) -> None: ...


class MessageStreamPublisher(ABC):
    """Publish message-level streaming events (text deltas, assistant frames)."""

    @abstractmethod
    def publish_stream_event_text(self, *, session_id: str, text: str, index: int = 0) -> None: ...

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
    def publish_message_finish(self) -> None: ...


class ToolEventPublisher(ABC):
    """Publish tool execution events (deltas, results)."""

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
    def publish_tool_result(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
        is_error: bool = False,
    ) -> None: ...


class ValuesPublisher(ABC):
    """Publish state-snapshot values events."""

    @abstractmethod
    def publish_values(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        files: Optional[dict[str, Any]] = None,
    ) -> None: ...

    @abstractmethod
    def publish_checkpoint(self, *, session_id: str) -> None: ...


class ResultPublisher(ABC):
    """Publish turn-level result frames."""

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
