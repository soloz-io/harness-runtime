"""Root (coordinator) message handler — streams orchestrator text + tool blocks.

SRP: Forwards coordinator text deltas as stream events and
emits assistant frames on message-finish.
"""

import uuid

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.types import Event


class RootMessageHandler(EventHandler):
    """Handle coordinator messages events (text streaming + tool blocks)."""

    def can_handle(self, event: Event) -> bool:
        return not event.namespace and event.method == "messages"

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
        raw = event.data
        payload, _metadata = raw if isinstance(raw, tuple) else (raw, {})  # type: ignore[assignment]
        if not isinstance(payload, dict):
            return True
        event_type = payload.get("event")

        if event_type == "content-block-delta":
            delta = payload.get("delta", {})
            text = delta.get("text", "") if isinstance(delta, dict) else ""
            if text:
                state.streamed_text += text
                publisher.publish_stream_event_text(
                    session_id=session_id,
                    text=text,
                )

        elif event_type == "content-block-start":
            content = payload.get("content", {})
            if isinstance(content, dict) and content.get("type") == "tool_call":
                state.current_tool_use_blocks.append(
                    {
                        "type": "tool_use",
                        "id": content.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                        "name": content.get("name", "unknown"),
                        "input": content.get("args", content.get("input", {})),
                    }
                )

        elif event_type == "message-finish":
            streamed_text = state.streamed_text
            blocks = state.current_tool_use_blocks
            if blocks:
                publisher.publish_assistant(
                    session_id=session_id,
                    model=model_name,
                    content=[{"type": "text", "text": streamed_text or ""}] + blocks,
                )
            state.current_tool_use_blocks = []
            publisher.publish_message_finish()

        return True
