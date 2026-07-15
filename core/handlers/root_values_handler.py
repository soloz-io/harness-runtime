"""Root (coordinator) values handler — persists state, publishes values.

SRP: Handles coordinator-level values events:
- Interrupt detection and publishing
- Structured response / file extraction
- DB projection of messages + files
- Values channel publishing
"""

import time
from typing import Any, Optional

import structlog

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.executor_helpers import extract_interrupt_payload, serialize_messages_for_values
from core.handlers import EventHandler
from core.message_writer import write_agent_output_files, write_chat_messages
from core.types import Event

logger = structlog.get_logger(__name__)


class RootValuesHandler(EventHandler):
    """Handle coordinator values events (persist + publish snapshot)."""

    def __init__(self, pool: Optional[Any] = None) -> None:
        self._pool = pool

    def can_handle(self, event: Event) -> bool:
        return not event.namespace and event.method == "values"

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

        # ---- Interrupt detection ----
        interrupt_val = data.get("__interrupt__")
        if interrupt_val is not None:
            self._publish_interrupt(
                interrupt_val, state, publisher, session_id, start_time, num_turns
            )
            return False

        # ---- Structured response / files ----
        if "structured_response" in data:
            state.last_structured_response = data["structured_response"]
        state_files = data.get("files")
        if state_files:
            state.last_files.update(state_files)

        # ---- Messages for values channel ----
        msgs = data.get("messages", [])
        prev_count = state.values_messages_count
        if len(msgs) > prev_count:
            state.values_messages_count = len(msgs)
            serialized = serialize_messages_for_values(msgs)
            if serialized:
                if self._pool is not None:
                    logger.debug(
                        "handle_values_writing_messages",
                        session_id=session_id,
                        new_count=len(serialized),
                        prev_count=prev_count,
                    )
                    write_chat_messages(self._pool, session_id, serialized, prev_count)
                    write_agent_output_files(self._pool, session_id, state.last_files)
                else:
                    logger.warning(
                        "handle_values_no_pool_skipping_message_write",
                        session_id=session_id,
                    )
                publisher.publish_checkpoint(session_id=session_id)
                publisher.publish_values(
                    session_id=session_id,
                    messages=serialized,
                    files=state.last_files or None,
                )

        return True

    @staticmethod
    def _publish_interrupt(
        interrupt_val: Any,
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        start_time: float,
        num_turns: int,
    ) -> None:
        """Publish lifecycle completed + result for an interrupt."""
        interrupt_payload = extract_interrupt_payload(interrupt_val)
        remaining = state.streamed_text
        state.streamed_text = ""
        if remaining:
            publisher.publish_assistant(
                session_id=session_id,
                model="",
                content=[{"type": "text", "text": remaining}],
            )
        duration_ms = int((time.time() - start_time) * 1000)
        publisher.publish_lifecycle_completed(session_id=session_id)
        publisher.publish_result(
            session_id=session_id,
            subtype="interrupted",
            duration_ms=duration_ms,
            num_turns=num_turns,
            result=remaining or None,
            interrupt=interrupt_payload,
        )
        state.interrupted = True
