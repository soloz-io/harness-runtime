"""Sub-agent tools handler — streams sub-agent tool results as chat text.

SRP: Forwards sub-agent tool-finished events to the frontend
as real-time chat text via publish_stream_event_text.
"""

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.types import Event


class SubagentToolsHandler(EventHandler):
    """Route sub-agent tool-finished as chat text."""

    def can_handle(self, event: Event) -> bool:
        return bool(event.namespace) and event.method == "tools"

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
        if event_type == "tool-finished":
            output = data.get("output", "")
            tool_name = data.get("tool_name", "unknown")
            if output:
                publisher.publish_stream_event_text(
                    session_id=session_id,
                    text=f"\n*Tool Result ({tool_name}):*\n{output}\n",
                )
        return True
