"""Tool handler registry — per-tool event handlers for tool-output-delta and tool-finished.

Follows OCP: add a new tool behavior by subclassing ``ToolEventHandler``
and registering it — no edits to existing code.
"""

from typing import Any, Optional

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState


class ToolEventHandler:
    """Single-responsibility handler for one tool's runtime events.

    Subclasses declare ``tool_name`` and override the hooks
    they care about (default no-ops for both).
    """

    tool_name: str = ""

    def handle_output_delta(
        self,
        *,
        data: dict[str, Any],
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        """Called on ``tool-output-delta`` events for this tool."""

    def handle_finished(
        self,
        *,
        data: dict[str, Any],
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        """Called on ``tool-finished`` events for this tool."""


class DefaultToolHandler(ToolEventHandler):
    """Fallback handler used when no specific handler is registered for a tool name."""

    tool_name = "__default__"

    def handle_output_delta(
        self,
        *,
        data: dict[str, Any],
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        delta = data.get("delta", "")
        if delta:
            publisher.publish_tool_output_delta(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                delta=delta,
            )

    def handle_finished(
        self,
        *,
        data: dict[str, Any],
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        from core.executor_helpers import extract_tool_finished_content

        content = extract_tool_finished_content(data.get("output", ""))
        publisher.publish_tool_result(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content=content,
            is_error=bool(data.get("is_error", False)),
        )


class ToolHandlerRegistry:
    """Registry mapping tool names to ``ToolEventHandler`` instances.

    Usage::

        registry = ToolHandlerRegistry()
        registry.register(MyCustomHandler())
        handler = registry.get_handler("my_custom_tool")  # → MyCustomHandler
        handler = registry.get_handler("unknown_tool")     # → DefaultToolHandler
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ToolEventHandler] = {}
        self._default = DefaultToolHandler()

    def register(self, handler: ToolEventHandler) -> None:
        """Register a handler for ``handler.tool_name``."""
        if not handler.tool_name:
            raise ValueError(
                f"ToolEventHandler must define tool_name, got {type(handler).__name__}"
            )
        self._handlers[handler.tool_name] = handler

    def get_handler(self, tool_name: str) -> ToolEventHandler:
        """Return the registered handler for *tool_name*, or the default."""
        return self._handlers.get(tool_name, self._default)

    @property
    def default(self) -> ToolEventHandler:
        return self._default
