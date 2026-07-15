"""Sub-agent lifecycle handler — publishes tool-finished on completion.

SRP: Detects sub-agent lifecycle completion/failure events and
emits ``tool-finished`` SSE events so the frontend marks the
sub-agent card as completed instead of stuck in "thinking...".

This is a **new** handler that was previously missing — the old code
never published ``tool-finished`` for sub-agent (task) tools.
"""

import structlog

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.types import Event, Namespace

logger = structlog.get_logger(__name__)


class SubagentLifecycleHandler(EventHandler):
    """Publish ``tool-finished`` when a sub-agent's lifecycle completes.

    Fixes the gap where sub-agent tool calls stayed stuck in
    "thinking..." on the frontend because no ``tool-finished`` SSE
    event was ever emitted for them.
    """

    def can_handle(self, event: Event) -> bool:
        return event.method == "lifecycle" and bool(event.namespace)

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
        ns: Namespace = event.namespace
        data = event.data
        sub_event = data.get("event")

        # --- Started → record namespace → tool_call_id mapping ---
        if sub_event == "started":
            ns_list = data.get("namespace")
            cause = data.get("cause")
            if (
                isinstance(ns_list, list)
                and isinstance(cause, dict)
                and cause.get("type") == "toolCall"
            ):
                tool_call_id = cause.get("tool_call_id")
                if tool_call_id:
                    state.ns_to_tool_call[ns] = tool_call_id
                    state.subagent_names[ns] = data.get("graph_name", "task")
                    logger.debug(
                        "subagent_started",
                        namespace=ns_list,
                        tool_call_id=tool_call_id,
                    )
            return True

        if sub_event not in ("completed", "failed"):
            return True

        tool_call_id = state.ns_to_tool_call.pop(ns, None)
        if not tool_call_id:
            return True

        subagent_name = state.subagent_names.pop(ns, "task")
        accumulated_output = state.subagent_stream_outputs.pop(tool_call_id, None)

        if accumulated_output is not None:
            state.subagent_final_outputs[tool_call_id] = accumulated_output

        content = accumulated_output or f"subagent_{sub_event}"
        is_error = sub_event == "failed"

        logger.debug(
            "subagent_finished",
            tool_call_id=tool_call_id,
            subagent_name=subagent_name,
            is_error=is_error,
            output_length=len(content),
        )

        publisher.publish_tool_result(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=subagent_name,
            content=content,
            is_error=is_error,
        )
        return True
