"""Root (coordinator) tools handler — dispatches to per-tool event handlers.

SRP: Forwards coordinator-level tool events to the ``ToolHandlerRegistry``
for per-tool dispatch. Sub-agent (task) tools are handled by
``SubagentLifecycleHandler`` instead.
"""

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.tool_handlers import ToolHandlerRegistry
from core.types import Event


class RootToolsHandler(EventHandler):
    """Handle coordinator tools events via registered tool handlers."""

    def __init__(self, registry: ToolHandlerRegistry | None = None) -> None:
        self._registry = registry or ToolHandlerRegistry()

    def set_registry(self, registry: ToolHandlerRegistry) -> None:
        self._registry = registry

    def can_handle(self, event: Event) -> bool:
        return not event.namespace and event.method == "tools"

    def handle(
        self,
        event: Event,
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        start_time: float,
        num_turns: int,
    ) -> bool | None:
        data = event.data
        event_type = data.get("event")
        tool_call_id = data.get("tool_call_id")
        if not tool_call_id:
            return True

        tool_name: str = data.get("tool_name") or data.get("name") or "unknown"
        handler = self._registry.get_handler(tool_name)

        if event_type == "tool-output-delta":
            handler.handle_output_delta(
                data=data,
                state=state,
                publisher=publisher,
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )

        elif event_type == "tool-finished":
            handler.handle_finished(
                data=data,
                state=state,
                publisher=publisher,
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )

        return True
