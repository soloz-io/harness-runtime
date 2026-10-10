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

from core.execution.handlers import EventHandler
from core.execution.helpers import extract_interrupt_payload, serialize_messages_for_values
from core.execution.state import ExecutionState
from core.execution.types import Event
from core.middleware.background_jobs import is_job_outcome
from core.persistence.message_writer import write_agent_output_files, write_chat_messages
from core.publishers.event_publisher import EventPublisher
from core.waypoint_reports import has_unanswered_question, is_reply, note_new_messages

logger = structlog.get_logger(__name__)


def extract_context_usage(messages: Any) -> Optional[dict[str, int]]:
    """The newest model call's token usage, as the provider counted it.

    Read from the LAST message carrying ``usage_metadata`` rather than summed
    across the conversation: each call reports the size of the whole input it
    was given, so the newest one already describes the current context. Adding
    them would multiply-count every message that survived into the next call.

    ``used`` is input + output + cache reads + cache writes — the total the
    provider weighs against its window, which is the same basis opencode uses
    for its own overflow check. Counting ``input_tokens`` alone under-reports
    and lets an indicator look comfortable right up to a rejection.

    Returns ``None`` when nothing reports usage (a turn with no model call, or a
    provider that omits it) so the caller can leave the field off entirely
    rather than publish a zero that would read as "no context used".
    """
    if not messages:
        return None
    for msg in reversed(messages):
        meta = getattr(msg, "usage_metadata", None)
        if not isinstance(meta, dict):
            continue
        inp = int(meta.get("input_tokens") or 0)
        out = int(meta.get("output_tokens") or 0)
        details = meta.get("input_token_details") or {}
        cache_read = int(details.get("cache_read") or 0)
        cache_write = int(details.get("cache_creation") or 0)
        total = int(meta.get("total_tokens") or 0)
        # `total_tokens` usually already includes cache reads; prefer it and
        # fall back to the sum only when the provider omits it.
        used = total or (inp + out + cache_read + cache_write)
        if used <= 0:
            continue
        return {
            "used": used,
            "input": inp,
            "output": out,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
        }
    return None


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

        # ---- Structured response / files ----
        if "structured_response" in data:
            state.last_structured_response = data["structured_response"]
        state_files = data.get("files")
        if state_files:
            state.last_files.update(state_files)

        # ---- Interrupt detection ----
        #
        # The message flush below runs FIRST, before the interrupt is published
        # and the turn stops. The interrupting assistant message is the one
        # carrying the `ask_user` tool call, so returning early here (as this
        # used to) meant that message was never written to chat_messages and
        # never published on the values channel: the UI saw the agent's preamble
        # text, no question, and a turn that simply went quiet. The client's
        # pending-interaction lookup keys off that tool call, so without it no
        # prompt can ever render.
        #
        # Read from `event.interrupts`, not from `data`. This was
        # `data.get("__interrupt__")`, which is the name of the CHECKPOINT
        # channel, not of anything the v3 stream emits — the protocol carries
        # interrupts in `params.interrupts`, beside `data`. The lookup
        # therefore always returned None, `_publish_interrupt` never ran, and
        # the result frame went out with no questions in it. Two blocking
        # `ask_user` clarifications were lost that way while the graph sat
        # parked on them; the UI showed only "Thought for 86 seconds", and
        # because the stream drained cleanly the turn was logged as completed.
        interrupt_val = event.interrupts or None

        # ---- Messages for values channel ----
        msgs = data.get("messages", [])
        prev_count = state.values_messages_count
        if len(msgs) > prev_count:
            state.values_messages_count = len(msgs)
            # A reported job outcome is the agent's to read, not the chat's to
            # show: the chat already shows the job's own notice row (ADR-015).
            serialized = [m for m in serialize_messages_for_values(msgs) if not is_job_outcome(m)]
            if serialized:
                for msg in serialized:
                    if (
                        msg.get("type") == "tool"
                        and msg.get("tool_call_id") in state.subagent_final_outputs
                    ):
                        msg["subagent_streaming_text"] = state.subagent_final_outputs[
                            msg["tool_call_id"]
                        ]
                if self._pool is not None:
                    logger.debug(
                        "handle_values_writing_messages",
                        session_id=session_id,
                        new_count=len(serialized),
                        prev_count=prev_count,
                    )
                    inserted = write_chat_messages(self._pool, session_id, serialized, prev_count)
                    # A reply the session had not recorded: replayed history is
                    # skipped by the store, so it never counts (ADR-025).
                    if any(is_reply(m) for m in inserted):
                        state.replied = True
                    write_agent_output_files(
                        self._pool,
                        session_id,
                        state.last_files,
                        workspace_id=state.workspace_id,
                        app_id=state.app_id,
                    )
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
                    usage=extract_context_usage(msgs),
                )
                # Whether the agent is now waiting on the user: an `ask_user`
                # call with no answer after it. Recomputed from the whole state
                # each time, never from "new" messages -- a turn's first event
                # carries the session's history, old questions included. The
                # executor reports it once, when the turn ends.
                state.unanswered_question = has_unanswered_question(serialized)
                # A tool of the main agent that can write finished: the
                # workspace may have changed (core/waypoint_reports/workspace.py).
                note_new_messages(session_id, serialized[prev_count:])

        if interrupt_val is not None:
            self._publish_interrupt(
                interrupt_val, state, publisher, session_id, start_time, num_turns
            )
            return False

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
        publisher.publish_turn_end(
            session_id=session_id,
            outcome="completed",
            subtype="interrupted",
            duration_ms=duration_ms,
            num_turns=num_turns,
            result=remaining or None,
            interrupt=interrupt_payload,
        )
        state.interrupted = True
