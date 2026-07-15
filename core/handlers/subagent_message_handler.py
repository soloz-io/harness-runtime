"""Sub-agent message handler — streams sub-agent thinking as tool-output-delta.

SRP: Forwards sub-agent content-block-delta text to the frontend
as real-time tool-output-delta events.
"""

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.types import Event


class SubagentMessageHandler(EventHandler):
    """Route sub-agent content-block-delta as tool-output-delta."""

    def can_handle(self, event: Event) -> bool:
        return bool(event.namespace) and event.method == "messages"

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
        ns = event.namespace
        tool_call_id = state.ns_to_tool_call.get(ns)
        if not tool_call_id:
            return True
        subagent_name = state.subagent_names.get(ns, "task")

        raw = event.data
        payload, _metadata = raw if isinstance(raw, tuple) else (raw, {})  # type: ignore[assignment]
        if not isinstance(payload, dict):
            return True
        event_type = payload.get("event")
        if event_type != "content-block-delta":
            return True

        delta = payload.get("delta", {})
        if isinstance(delta, dict):
            delta_type = delta.get("type")
            if delta_type == "tool_use":
                tool_name = delta.get("name", "unknown")
                publisher.publish_stream_event_text(
                    session_id=session_id,
                    text=f"\n*Tool Call: {tool_name}*\n",
                )
                text = ""
            else:
                text = delta.get("text", "")
        else:
            text = ""

        if text:
            publisher.publish_tool_output_delta(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=subagent_name,
                delta=text,
            )
            state.subagent_stream_outputs.setdefault(tool_call_id, "")
            state.subagent_stream_outputs[tool_call_id] += text
        return True
