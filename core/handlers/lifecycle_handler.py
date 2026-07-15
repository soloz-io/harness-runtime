"""Lifecycle handler — records namespace -> tool_call_id mapping.

SRP: Tracks sub-agent lifecycle events in execution state.
Handles both root and sub-agent lifecycle events (sub-agent
events arrive with ``params.namespace=[]`` but ``data.namespace``
populated — see LangGraph ``_mux._forward``).
"""

import structlog

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import EventHandler
from core.types import Event, Namespace

logger = structlog.get_logger(__name__)


class LifecycleHandler(EventHandler):
    """Record sub-agent namespace -> tool_call_id on lifecycle started,
    publish tool-finished on lifecycle completed/failed."""

    def can_handle(self, event: Event) -> bool:
        return event.method == "lifecycle" and not event.namespace

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
        sub_event = data.get("event")

        # --- Started → record namespace -> tool_call_id mapping ---
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
                    ns_tuple: Namespace = tuple(ns_list)
                    state.ns_to_tool_call[ns_tuple] = tool_call_id
                    state.subagent_names[ns_tuple] = data.get("graph_name", "task")
                    logger.debug(
                        "subagent_started",
                        namespace=ns_list,
                        tool_call_id=tool_call_id,
                    )
            return True

        # --- Completed / Failed → publish tool-finished for sub-agent ---
        if sub_event in ("completed", "failed"):
            ns_list = data.get("namespace")
            if isinstance(ns_list, list):
                ns_tuple: Namespace = tuple(ns_list)
                tool_call_id = state.ns_to_tool_call.pop(ns_tuple, None)
                if tool_call_id:
                    subagent_name = state.subagent_names.pop(ns_tuple, "task")
                    accumulated_output = state.subagent_stream_outputs.pop(tool_call_id, None)
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

        return True
