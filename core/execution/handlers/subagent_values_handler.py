"""Sub-agent values handler — persists reasoning to DB, propagates files.

SRP: Extracts messages and files from sub-agent values events.
- Messages are written to chat_messages with source='subagent'
- Files are published as SSE values events for the frontend file tree
"""

from typing import Any, Optional

from core.execution.handlers import EventHandler
from core.execution.helpers import serialize_messages_for_values
from core.execution.state import ExecutionState
from core.execution.types import Event
from core.persistence.message_writer import write_agent_output_files, write_chat_messages
from core.publishers.event_publisher import EventPublisher
from core.waypoint_reports import note_new_messages


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
            if self._pool is not None:
                write_agent_output_files(
                    self._pool,
                    session_id,
                    state_files,
                    workspace_id=state.workspace_id,
                    app_id=state.app_id,
                )

        # ---- Todos ----
        # A specialist's or subagent's todo list reaches the browser live, as
        # the agent wrote it, for the chat's task list (waypoint ADR-052). Its
        # write_todos calls are stored with its messages below, so a reload has
        # the same lists. Published only when the list changed.
        ns = event.namespace
        todos = data.get("todos")
        if isinstance(todos, list) and todos != state.subagent_todos.get(ns):
            state.subagent_todos[ns] = todos
            agent = state.subagent_names.get(ns) or (str(ns[-1]).split(":")[0] if ns else "agent")
            publisher.publish_values(
                session_id=session_id,
                messages=[],
                todos={"agent": agent, "namespace": list(ns), "todos": todos},
            )

        # ---- Messages ----
        msgs = data.get("messages", [])
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

                # A specialist's tool that can write finished: the session's
                # workspace may have changed (core/waypoint_reports/workspace.py).
                # The session is the root one: that is whose workspace it is.
                note_new_messages(session_id, serialized[prev_count:])

        return True
