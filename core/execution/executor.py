"""Agent graph executor — runs compiled LangGraph agent and streams events.

Orchestrates the execution loop: invokes the compiled graph, dispatches
v3 protocol events through a handler chain, detects interrupts, and
publishes results.

Uses ``stream_events(version="v3")`` (deepagents-native streaming
protocol) so that specialist subagent content arrives as real-time token
deltas rather than post-hoc 60-character chunks.
"""

import asyncio
import os
import time
import traceback
from typing import Any, Optional

import psycopg
import structlog
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from core.execution.handlers import create_handler_chain
from core.execution.state import ExecutionState
from core.execution.types import Event
from core.persistence.message_writer import stamp_checkpoint_id
from core.publishers.event_publisher import EventPublisher
from core.waypoint_reports import flush_workspace_change, report_question

logger = structlog.get_logger(__name__)

# Maximum number of LangGraph graph steps (tool invocations + LLM calls)
# allowed per execution.  The deepagents library sets recursion_limit=9999
# which is effectively unlimited and allows degenerate model behaviour
# (e.g. a subagent looping on the same tool call) to run forever.
# 150 is generous enough for deep orchestrator + subagent workflows while
# still guarding against infinite loops.  When hit, LangGraph raises
# GraphRecursionError which the existing exception handler surfaces to the
# client as a clean error.
RECURSION_LIMIT = int(os.environ.get("HARNESS_RECURSION_LIMIT", "150"))


class ExecutionError(Exception):
    pass


# LangGraph's channel for a pending interrupt (private in langgraph >= 1.0).
_INTERRUPT_CHANNEL = "__interrupt__"


class ExecutionManager:
    """Executes a compiled LangGraph agent and streams v3 events.

    Uses a **handler chain** (chain-of-responsibility) pattern for
    event dispatch — each handler has a single responsibility and can
    be added / removed without modifying this class.
    """

    # ------------------------------------------------------------------
    # Construction / lifecycle
    # ------------------------------------------------------------------

    def __init__(
        self,
        postgres_connection_string: str,
        publisher: EventPublisher,
    ) -> None:
        self.publisher = publisher
        self.postgres_connection_string = postgres_connection_string
        self.checkpointer = None
        self._checkpointer_context = None
        self._async_checkpointer = None
        self._async_checkpointer_context = None
        self._pool: Any = None
        self._async_pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] | None = None
        self._tracer = None

        try:
            from opentelemetry import trace as _otel_trace

            self._tracer = _otel_trace.get_tracer("harness-runtime", "0.1.13")
        except Exception:
            pass

        if postgres_connection_string:
            self._pool = ConnectionPool(postgres_connection_string, min_size=1, max_size=5)
            self._setup_checkpointer()

        self._handler_chain = create_handler_chain(pool=self._pool)

    def _setup_checkpointer(self) -> None:
        try:
            conn: Any = psycopg.connect(self.postgres_connection_string, autocommit=True)
            self.checkpointer = PostgresSaver(conn=conn)
            self.checkpointer.setup()
            conn.close()
            self.checkpointer = PostgresSaver(conn=self._pool)
        except Exception as e:
            logger.error("checkpointer_setup_failed", error=str(e))
            raise

    @classmethod
    async def create_async(
        cls,
        postgres_connection_string: str,
        publisher: EventPublisher,
    ) -> "ExecutionManager":
        self = cls.__new__(cls)
        self.publisher = publisher
        self.postgres_connection_string = postgres_connection_string
        self.checkpointer = None
        self._checkpointer_context = None
        self._async_checkpointer = None
        self._async_checkpointer_context = None
        self._pool = None
        self._async_pool = None
        self._tracer = None

        try:
            from opentelemetry import trace as _otel_trace

            self._tracer = _otel_trace.get_tracer("harness-runtime", "0.1.13")
        except Exception:
            pass

        if postgres_connection_string:
            self._pool = ConnectionPool(postgres_connection_string, min_size=1, max_size=5)
            await self._async_setup_checkpointer()
            self.checkpointer = self._async_checkpointer

        self._handler_chain = create_handler_chain(pool=self._pool)
        return self

    async def _async_setup_checkpointer(self) -> None:
        try:
            self._async_pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
                self.postgres_connection_string, min_size=1, max_size=5
            )
            aconn: Any = await psycopg.AsyncConnection.connect(
                self.postgres_connection_string,
                autocommit=True,
            )
            self._async_checkpointer = AsyncPostgresSaver(conn=aconn)
            await self._async_checkpointer.setup()
            await aconn.close()
            self._async_checkpointer = AsyncPostgresSaver(conn=self._async_pool)
        except Exception as e:
            logger.error("async_checkpointer_setup_failed", error=str(e))
            raise

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _handle_initial_setup(
        self,
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        agent_definition: Optional[dict[str, Any]],
    ) -> None:
        from core.execution.helpers import compute_tools

        tools = compute_tools(agent_definition) if agent_definition else []
        publisher.publish_system_init(
            session_id=session_id,
            model=model_name,
            tools=tools,
        )
        publisher.publish_lifecycle_started(session_id=session_id)

    def _make_stream_input(
        self,
        input_payload: dict[str, Any],
        resume_payload: Optional[Any],
    ) -> Any:
        if resume_payload is not None:
            from langgraph.types import Command

            return Command(resume=resume_payload)
        return input_payload

    @staticmethod
    def _latest_user_text(input_payload: dict[str, Any]) -> str:
        """Plain text of the newest user message in *input_payload*, or ""."""
        messages = input_payload.get("messages")
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            # Multimodal content: concatenate the text blocks, skipping images.
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
                return " ".join(parts).strip()
            return ""
        return ""

    async def has_pending_question(self, session_id: str) -> bool:
        """Whether the session's graph is paused on a question the user has not answered.

        True when the latest checkpoint has a pending interrupt (an ``ask_user``
        waiting for its answer). A check that cannot be made answers False: the
        caller then proceeds as it would without a question.
        """
        checkpointer = self._async_checkpointer or self.checkpointer
        if checkpointer is None:
            return False
        try:
            config: RunnableConfig = {"configurable": {"thread_id": session_id}}
            if hasattr(checkpointer, "aget_tuple"):
                cpt = await checkpointer.aget_tuple(config)
            else:
                cpt = checkpointer.get_tuple(config)
        except Exception as e:
            logger.warning("pending_interrupt_check_failed", session_id=session_id, error=str(e))
            return False
        if cpt is None:
            return False

        # A paused graph's latest checkpoint holds the interrupt as a pending
        # write on the interrupt channel: (task_id, "__interrupt__", value). A
        # CheckpointTuple carries no `next` in its metadata and no `interrupts`
        # attribute -- those are StateSnapshot's -- so reading either always
        # answered "nothing pending", and a notice cancelled the open question.
        writes = getattr(cpt, "pending_writes", None) or []
        return any(len(w) >= 2 and w[1] == _INTERRUPT_CHANNEL for w in writes)

    async def has_unreported_jobs(self, session_id: str) -> bool:
        """Whether an idle session has finished jobs its agent has not been told
        about, so a wake-up run would report them (ADR-015).

        False while the graph is paused on a question -- the answer's resume
        reports them -- and False whenever the check cannot be made: a wake-up
        on a paused graph would abandon the user's question, so doubt means no
        wake-up. The job stays pending and is reported at the next model call.
        """
        checkpointer = self._async_checkpointer or self.checkpointer
        if checkpointer is None or self._pool is None:
            return False
        try:
            config: RunnableConfig = {"configurable": {"thread_id": session_id}}
            if hasattr(checkpointer, "aget_tuple"):
                cpt = await checkpointer.aget_tuple(config)
            else:
                cpt = checkpointer.get_tuple(config)
            if cpt is None:
                return False
            writes = getattr(cpt, "pending_writes", None) or []
            if any(len(w) >= 2 and w[1] == _INTERRUPT_CHANNEL for w in writes):
                return False
            pending = list((cpt.checkpoint.get("channel_values") or {}).get("pending_jobs") or [])
            if not pending:
                return False
            from core.middleware.background_jobs import finished_jobs

            finished = await asyncio.to_thread(finished_jobs, self._pool, pending)
        except Exception as e:
            logger.warning("unreported_jobs_check_failed", session_id=session_id, error=str(e))
            return False
        return bool(finished)

    async def _resume_from_pending_interrupt(
        self,
        input_payload: dict[str, Any],
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        """Turn a plain user message into a resume decision when the graph is paused.

        Returns a ``decisions`` payload shaped exactly like the one the client
        sends for an explicit answer, or ``None`` when the graph is not parked on
        an interrupt (the ordinary case) — in which case the caller proceeds with
        a normal turn.

        Deliberately only handles the ``respond`` decision. Approve/reject/edit
        carry intent that a free-text message does not express, and guessing at
        one would act on the user's behalf.
        """
        text = self._latest_user_text(input_payload)
        if not text:
            return None
        if not await self.has_pending_question(session_id):
            return None

        logger.info(
            "plain_message_resumed_pending_interrupt",
            session_id=session_id,
            text_preview=text[:120],
        )
        return {"decisions": [{"type": "respond", "message": text}]}

    async def _build_resume_input(
        self,
        input_payload: dict[str, Any],
        resume_payload: Optional[Any],
        session_id: str,
    ) -> Any:
        """Build stream input for resuming, injecting ToolMessages if needed.

        Loads the checkpoint to find orphaned tool_call_ids and
        injects matching ToolMessages into the state via
        ``Command(update=..., resume=...)``.
        """
        if resume_payload is None:
            # The graph may be parked on a human-interaction interrupt (ask_user)
            # that this message is the answer to. The client normally recognises
            # that and sends an explicit resume_payload, but it cannot when its
            # view of the conversation is stale — after a reload, on a second
            # tab, or when the prompt never rendered.
            #
            # Without this, such a message starts a fresh turn and the interrupt
            # is abandoned rather than answered. deepagents' PatchToolCallsMiddleware
            # then rewrites the unanswered call as "was cancelled - another message
            # came in before it could be completed", which the agent reports to the
            # user as an interruption it cannot explain.
            resume_payload = await self._resume_from_pending_interrupt(input_payload, session_id)
            if resume_payload is None:
                return input_payload

        from langgraph.types import Command

        decisions: list[dict[str, Any]] = []
        if isinstance(resume_payload, dict):
            decisions = resume_payload.get("decisions", [])

        if not decisions:
            return Command(resume=resume_payload)

        # ---- Load checkpoint to find orphaned tool_call_ids ----
        # Track (id, name) pairs so we can exclude self-interrupting tools below.
        tool_calls_pending: list[tuple[str, str]] = []  # (tool_call_id, tool_name)
        checkpointer = self._async_checkpointer or self.checkpointer
        if checkpointer is not None:
            try:
                config: RunnableConfig = {"configurable": {"thread_id": session_id}}
                if hasattr(checkpointer, "aget_tuple"):
                    cpt = await checkpointer.aget_tuple(config)
                else:
                    cpt = checkpointer.get_tuple(config)
                if cpt is not None:
                    # With no pending interrupt there is nothing to resume: feed
                    # the decision as a new human turn instead of calling
                    # Command(resume=...), a silent no-op on a finished graph.
                    # (This read metadata["next"], which a CheckpointTuple does
                    # not carry, so every resume fell back to a human turn and
                    # the question was abandoned.)
                    if not await self.has_pending_question(session_id):
                        logger.info(
                            "resume_graph_at_end_fallback_to_human_turn",
                            session_id=session_id,
                        )
                        return input_payload

                    checkpoint = cpt.checkpoint if hasattr(cpt, "checkpoint") else cpt
                    if isinstance(checkpoint, dict):
                        channel_values = checkpoint.get("channel_values", {})
                        msgs: Any = channel_values.get("messages", [])
                        if isinstance(msgs, list):
                            completed_tool_call_ids: set[str] = set()
                            for msg in msgs:
                                tid = getattr(msg, "tool_call_id", None) or (
                                    msg.get("tool_call_id") if isinstance(msg, dict) else None
                                )
                                msg_type = getattr(msg, "type", None) or (
                                    msg.get("type") if isinstance(msg, dict) else None
                                )
                                if (
                                    msg_type in ("tool", "ToolMessage")
                                    or msg.__class__.__name__ == "ToolMessage"
                                ) and tid:
                                    completed_tool_call_ids.add(tid)

                            for msg in msgs:
                                tcs = getattr(msg, "tool_calls", None)
                                if tcs and isinstance(tcs, list):
                                    for tc in tcs:
                                        tid = tc.get("id") or tc.get("tool_call_id") or ""
                                        name = tc.get("name", "")
                                        if tid and tid not in completed_tool_call_ids:
                                            tool_calls_pending.append((tid, name))
            except Exception as e:
                logger.warning("resume_checkpoint_load_failed", error=str(e))

        # Tools that call langgraph.types.interrupt() in their own body: they are
        # resumed by Command(resume=...) alone — LangGraph automatically creates the
        # ToolMessage from the tool's return value.  Manually injecting a ToolMessage
        # here would produce two ToolMessages for the same tool_call_id, corrupting
        # graph state.  Tools NOT in this set (review_content, script_reviewer, …)
        # still need the manual ToolMessage injection via Command(update=...).
        SELF_INTERRUPTING_TOOLS: set[str] = {"ask_user"}

        # Only inject ToolMessages for non-self-interrupting orphaned calls.
        tool_call_ids = [
            tid for tid, name in tool_calls_pending if name not in SELF_INTERRUPTING_TOOLS
        ]

        if not tool_call_ids:
            return Command(resume=resume_payload)

        # ---- Build ToolMessages from decisions ----
        from langchain_core.messages import ToolMessage

        tool_messages: list[Any] = []
        for i, decision in enumerate(decisions):
            if i >= len(tool_call_ids):
                break
            dt = decision.get("type", "")
            if dt in ("respond", "reject", "approve", "edit"):
                tool_messages.append(
                    ToolMessage(
                        tool_call_id=tool_call_ids[i],
                        content=decision.get("message", "Approved"),
                        status="error" if dt == "reject" else "success",
                    )
                )

        if tool_messages:
            return Command(update={"messages": tool_messages}, resume=resume_payload)

        return Command(resume=resume_payload)

    # ------------------------------------------------------------------
    # Event dispatch via handler chain
    # ------------------------------------------------------------------

    def _process_v3_event(
        self,
        raw_event: dict[str, Any],
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        start_time: float,
        num_turns: int,
    ) -> bool:
        """Process a single ProtocolEvent from stream_events(v3).

        Returns True if execution should continue, False if stopped.
        """
        event = Event.from_raw(raw_event)
        handled = False

        for handler in self._handler_chain:
            if handler.can_handle(event):
                handled = True
                handler_name = type(handler).__name__
                try:
                    result = handler.handle(
                        event,
                        state,
                        publisher,
                        session_id,
                        model_name,
                        start_time,
                        num_turns,
                    )
                except Exception as h_e:
                    logger.error(
                        "v3_handler_error",
                        handler=handler_name,
                        method=event.method,
                        ns=event.namespace,
                        data_type=str(type(event.data)),
                        data_keys=list(event.data.keys())
                        if isinstance(event.data, dict)
                        else "N/A",
                        raw_event_keys=list(raw_event.keys()),
                        error=str(h_e),
                        traceback=traceback.format_exc(),
                    )
                    raise
                # DEBUG for the same reason as v3_raw_event above: one line per
                # event, and this one also carries `result` and the data type,
                # so it is the larger of the pair. The two together were two
                # lines for every chunk the model produced.
                logger.debug(
                    "v3_event_dispatch",
                    method=event.method,
                    ns=event.namespace,
                    handler=handler_name,
                    data_event=event.data.get("event", "")
                    if isinstance(event.data, dict)
                    else str(type(event.data)),
                    data_type=str(type(event.data)),
                    result=result,
                )
                if result is False:
                    return False
                return True

        if not handled:
            logger.info(
                "v3_event_unhandled",
                method=event.method,
                ns=event.namespace,
                data_event=event.data.get("event", ""),
                data_keys=list(event.data.keys()),
            )
        return True

    # ------------------------------------------------------------------
    # Final result publishing
    # ------------------------------------------------------------------

    def _publish_final_result(
        self,
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        start_time: float,
        num_turns: int,
        is_error: bool = False,
        error_str: str = "",
    ) -> str:
        """Publish the final result frame."""
        duration_ms = int((time.time() - start_time) * 1000)
        remaining = state.streamed_text
        if remaining and not is_error:
            publisher.publish_assistant(
                session_id=session_id,
                model="",
                content=[{"type": "text", "text": remaining}],
            )
        publisher.publish_message_finish()

        if is_error:
            publisher.publish_turn_end(
                session_id=session_id,
                outcome="failed",
                error=error_str,
                subtype="error_during_execution",
                duration_ms=duration_ms,
                is_error=True,
                result=error_str,
            )
            return ""

        final_text = remaining or ""
        publisher.publish_turn_end(
            session_id=session_id,
            outcome="completed",
            subtype="success",
            duration_ms=duration_ms,
            num_turns=num_turns,
            result=final_text,
            structured_response=state.last_structured_response,
            files=state.last_files or None,
        )
        return final_text

    # ------------------------------------------------------------------
    # Async execution
    # ------------------------------------------------------------------

    async def _warn_if_stalled(self, graph: Any, config: Any, session_id: str) -> None:
        """Report a turn that drained cleanly while the graph is still parked.

        Reaching here means no handler stopped the stream, so nothing was
        published as an interrupt — yet a non-empty ``next`` says the graph did
        not finish. That combination is a turn the user sees end with no answer
        and no error: the failure mode that hid a dropped ``ask_user`` question
        behind a clean ``turn_completed`` line, twice, with the graph waiting on
        an answer the UI was never given the chance to collect.

        Detect and say so; do not repair. Publishing the interrupt from here
        would be a second delivery path for something that already has one, and
        the next time that one breaks the symptom would be hidden again.

        Best-effort like the checkpoint stamp beside it: a graph with no
        checkpointer has no state to read, and failing to read it is not worth
        failing a turn over.
        """
        try:
            snapshot = await graph.aget_state(config)
        except Exception as e:  # noqa: BLE001 - diagnostic only
            logger.debug("stall_check_failed", session_id=session_id, error=str(e))
            return
        if snapshot is None:
            return
        pending = tuple(getattr(snapshot, "next", ()) or ())
        if not pending:
            return
        interrupts = getattr(snapshot, "interrupts", None) or ()
        logger.error(
            "turn_stalled_graph_still_pending",
            session_id=session_id,
            pending_nodes=list(pending),
            interrupt_count=len(interrupts),
            detail=(
                "The stream ended but the graph has not finished. If an interrupt is "
                "pending it was never published, so the client has nothing to answer "
                "and the turn will appear to end in silence."
            ),
        )

    async def _stamp_turn_checkpoint(self, graph: Any, config: Any, session_id: str) -> None:
        """Attach the just-committed checkpoint id to this turn's messages.

        Reads the graph's own state rather than tracking ids during the stream:
        LangGraph writes a checkpoint per superstep, and the one that matters
        for "restore to this message" is the last one of the turn, which is
        exactly what get_state returns once astream_events has drained.

        Entirely best-effort. A graph without a checkpointer has no state to
        read, and a failure here costs the UI a precise restore target — it
        falls back to positional pairing — which is not worth failing a
        completed turn over.
        """
        pool = getattr(self, "_pool", None)
        if pool is None:
            return
        try:
            snapshot = await graph.aget_state(config)
            checkpoint_id = (
                (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
                if snapshot
                else None
            )
            if checkpoint_id:
                await asyncio.to_thread(stamp_checkpoint_id, pool, session_id, str(checkpoint_id))
        except Exception:
            logger.debug("stamp_turn_checkpoint_skipped", session_id=session_id, exc_info=True)

    async def async_execute(
        self,
        graph: Runnable,
        session_id: str,
        input_payload: dict[str, Any],
        model_name: str,
        publisher: EventPublisher,
        agent_definition: Optional[dict[str, Any]] = None,
        num_turns: int = 1,
        resume_payload: Optional[Any] = None,
        workspace_id: Optional[str] = None,
        app_id: Optional[str] = None,
        run_reason: Optional[str] = None,
    ) -> str:
        tracer = self._tracer
        span = None
        if tracer:
            span = tracer.start_span("harness.graph.execute")
            span.set_attribute("session.id", session_id)
            span.set_attribute("model.name", model_name)
            span.set_attribute("num.turns", num_turns)

        start_time = time.time()
        state = ExecutionState()
        state.workspace_id = workspace_id or ""
        state.app_id = app_id

        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        if workspace_id:
            config["configurable"]["workspace_id"] = workspace_id
        if app_id:
            config["configurable"]["app_id"] = app_id
        if self._async_checkpointer:
            config["configurable"]["checkpointer"] = self._async_checkpointer
        # Why this run started, for traces and evals: "external_event" is a
        # wake-up with no human message (ADR-015); absent, a user's turn.
        if run_reason:
            config["metadata"] = {"run_reason": run_reason}
            logger.info("graph_run_reason", session_id=session_id, run_reason=run_reason)

        try:
            self._handle_initial_setup(publisher, session_id, model_name, agent_definition)

            stream_input = await self._build_resume_input(input_payload, resume_payload, session_id)

            # durability="sync": every step's checkpoint -- an interrupt
            # included -- is written before the run moves on, so when the turn
            # ends its pause is already saved. With the default ("async") the
            # interrupt could land after the turn reported done, and the next
            # turn on this session (a notice) saw no question open, ran, and
            # cancelled it.
            run = await graph.astream_events(stream_input, config, version="v3", durability="sync")
            async for raw_event in run:
                if not isinstance(raw_event, dict):
                    # Kept at INFO: a non-dict event is unexpected and rare,
                    # so it carries information rather than volume.
                    logger.info("v3_raw_event_skipped", type=str(type(raw_event)))
                    continue
                # DEBUG, not info.
                #
                # One line per event from `astream_events`, which is one per
                # streamed chunk — thousands per turn. Measured at ~10 MiB of
                # pod log in under seven minutes, which rotated away the
                # evidence anyone was actually looking for: the sandbox logs
                # that explain a failed build or a dead Metro were gone before
                # they could be read. A trace of every event is a debugging
                # tool, not an operational record, so it is available on
                # request and silent by default.
                logger.debug(
                    "v3_raw_event",
                    method=raw_event.get("method"),
                    ns=raw_event.get("params", {}).get("namespace"),
                    data_type=str(type(raw_event.get("params", {}).get("data"))),
                )
                ok = self._process_v3_event(
                    raw_event,
                    state,
                    publisher,
                    session_id,
                    model_name,
                    start_time,
                    num_turns,
                )
                if not ok:
                    # The turn stopped on an interrupt: the agent asked.
                    if state.unanswered_question:
                        report_question(session_id)
                    flush_workspace_change(session_id)
                    if span:
                        span.end()
                    return ""

            await self._warn_if_stalled(graph, config, session_id)
            # The turn ended with the agent waiting on the user: tell Waypoint,
            # for clients following this session without watching its turn.
            if state.unanswered_question:
                report_question(session_id)
            # And a workspace change the throttle was still holding.
            flush_workspace_change(session_id)

            result = self._publish_final_result(
                state,
                publisher,
                session_id,
                start_time,
                num_turns,
            )

            # Record which checkpoint this turn's messages belong to, now that
            # the superstep has committed and the id exists (ADR-017).
            #
            # Here rather than inside the values handler because the stream
            # events that write those rows carry no checkpoint reference — the
            # id is only knowable after the fact, from the graph's own state.
            await self._stamp_turn_checkpoint(graph, config, session_id)

            if span:
                span.set_attribute("duration_ms", int((time.time() - start_time) * 1000))
                span.end()

            return result

        except asyncio.CancelledError:
            logger.info("graph_execution_cancelled", session_id=session_id)
            if span:
                span.set_attribute("cancelled", True)
                span.end()
            publisher.publish_turn_end(
                session_id=session_id,
                outcome="cancelled",
                error="Execution cancelled by user",
                subtype="cancelled",
                is_error=True,
                result="Execution cancelled by user",
            )
            raise
        except Exception as e:
            logger.error("graph_execution_failed", error=str(e), traceback=traceback.format_exc())
            if span:
                span.record_exception(e)
                span.set_attribute("error", True)
                span.end()
            return self._publish_final_result(
                state,
                publisher,
                session_id,
                start_time,
                num_turns,
                is_error=True,
                error_str=str(e),
            )

    # ------------------------------------------------------------------
    # Sync execution (delegates to async via asyncio.run)
    # ------------------------------------------------------------------

    def execute(
        self,
        graph: Runnable,
        session_id: str,
        input_payload: dict[str, Any],
        model_name: str,
        agent_definition: Optional[dict[str, Any]] = None,
        num_turns: int = 1,
        resume_payload: Optional[Any] = None,
        workspace_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> str:
        tracer = self._tracer
        span = None
        if tracer:
            span = tracer.start_span("harness.graph.execute.sync")
            span.set_attribute("session.id", session_id)
            span.set_attribute("model.name", model_name)

        result = asyncio.run(
            self.async_execute(
                graph=graph,
                session_id=session_id,
                input_payload=input_payload,
                model_name=model_name,
                publisher=self.publisher,
                agent_definition=agent_definition,
                num_turns=num_turns,
                resume_payload=resume_payload,
                workspace_id=workspace_id,
                app_id=app_id,
            )
        )

        if span:
            span.end()
        return result

    # ------------------------------------------------------------------
    # Health / cleanup
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        try:
            return self.checkpointer is not None
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self._checkpointer_context:
                self._checkpointer_context.__exit__(None, None, None)
                self._checkpointer_context = None
                self.checkpointer = None
            if self._pool:
                self._pool.close()
                self._pool = None
        except Exception as e:
            logger.error("execution_manager_close_failed", error=str(e))

    async def aclose(self) -> None:
        try:
            if self._checkpointer_context:
                self._checkpointer_context.__exit__(None, None, None)
                self._checkpointer_context = None
                self.checkpointer = None
            if self._async_checkpointer_context:
                await self._async_checkpointer_context.__aexit__(None, None, None)
                self._async_checkpointer_context = None
                self._async_checkpointer = None
            if self._async_pool:
                await self._async_pool.close()
                self._async_pool = None
            if self._pool:
                self._pool.close()
                self._pool = None
        except Exception as e:
            logger.error("execution_manager_aclose_failed", error=str(e))

    def __enter__(self) -> "ExecutionManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
