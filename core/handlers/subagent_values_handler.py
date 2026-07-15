"""Sub-agent values handler — propagates files to frontend in real-time.

SRP: Extracts files from sub-agent values events and publishes them
as values events so the frontend file tree updates immediately.
"""

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.types import Event


class SubagentValuesHandler(EventHandler):
    """Propagate sub-agent files as values events."""

    def can_handle(self, event: Event) -> bool:
        return bool(event.namespace) and event.method == "values"

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
        if not isinstance(data, dict):
            return True
        state_files = data.get("files")
        if state_files:
            state.last_files.update(state_files)
            publisher.publish_values(
                session_id=session_id,
                messages=[],
                files=state_files,
            )
        return True
