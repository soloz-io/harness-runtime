"""Sub-agent values handler — persists reasoning to DB, propagates files.

SRP: Extracts messages and files from sub-agent values events.
- Messages are written to chat_messages with source='subagent'
- Files are published as SSE values events for the frontend file tree
"""

from typing import Any, Optional

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.executor_helpers import serialize_messages_for_values
from core.handlers import EventHandler
from core.message_writer import write_chat_messages
from core.types import Event


class SubagentValuesHandler(EventHandler):
    """Persist sub-agent reasoning messages and propagate files."""

    def __init__(self, pool: Optional[Any] = None) -> None:
        self._pool = pool

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

        # ---- Files ----
        state_files = data.get("files")
        if state_files:
            state.last_files.update(state_files)
            publisher.publish_values(
                session_id=session_id,
                messages=[],
                files=state_files,
            )

        # ---- Messages ----
        msgs = data.get("messages", [])
        ns = event.namespace
        prev_count = state.subagent_values_messages_count.get(ns, 0)
        if len(msgs) > prev_count:
            state.subagent_values_messages_count[ns] = len(msgs)
            serialized = serialize_messages_for_values(msgs)
            if serialized:
                # Annotate with namespace and tool_call_id for frontend grouping
                tool_call_id = state.ns_to_tool_call.get(ns)
                for msg in serialized:
                    msg["additional_kwargs"] = {
                        **msg.get("additional_kwargs", {}),
                        "namespace": list(ns),
                    }
                    if tool_call_id:
                        msg["additional_kwargs"]["tool_call_id"] = tool_call_id

                if self._pool is not None:
                    write_chat_messages(
                        self._pool,
                        session_id,
                        serialized,
                        prev_count,
                        source="subagent",
                    )

        return True
