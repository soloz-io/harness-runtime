"""Agent graph executor — runs compiled LangGraph agent and streams events.

Orchestrates the execution loop: invokes the compiled graph, dispatches
v3 protocol events through a handler chain, detects interrupts, and
publishes results.

Uses ``stream_events(version="v3")`` (deepagents-native streaming
protocol) so that specialist subagent content arrives as real-time token
deltas rather than post-hoc 60-character chunks.
"""

import asyncio
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

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.handlers import create_handler_chain
from core.types import Event

logger = structlog.get_logger(__name__)


class ExecutionError(Exception):
    pass


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
        from core.executor_helpers import compute_tools

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
            return input_payload

        from langgraph.types import Command

        decisions: list[dict[str, Any]] = []
        if isinstance(resume_payload, dict):
            decisions = resume_payload.get("decisions", [])

        if not decisions:
            return Command(resume=resume_payload)

        # ---- Load checkpoint to find orphaned tool_call_ids ----
        tool_call_ids: list[str] = []
        checkpointer = self._async_checkpointer or self.checkpointer
        if checkpointer is not None:
            try:
                config: RunnableConfig = {"configurable": {"thread_id": session_id}}
                if hasattr(checkpointer, "aget_tuple"):
                    cpt = await checkpointer.aget_tuple(config)
                else:
                    cpt = checkpointer.get_tuple(config)
                if cpt is not None:
                    checkpoint = cpt.checkpoint if hasattr(cpt, "checkpoint") else cpt
                    if isinstance(checkpoint, dict):
                        channel_values = checkpoint.get("channel_values", {})
                        msgs: Any = channel_values.get("messages", [])
                        if isinstance(msgs, list):
                            for msg in msgs:
                                tcs = getattr(msg, "tool_calls", None)
                                if tcs and isinstance(tcs, list):
                                    for tc in tcs:
                                        tid = tc.get("id") or tc.get("tool_call_id") or ""
                                        if tid:
                                            tool_call_ids.append(tid)
            except Exception as e:
                logger.warning("resume_checkpoint_load_failed", error=str(e))

        if not tool_call_ids:
            return Command(resume=resume_payload)

        # ---- Build ToolMessages from decisions ----
        from langchain_core.messages import ToolMessage

        tool_messages: list[Any] = []
        for i, decision in enumerate(decisions):
            if i >= len(tool_call_ids):
                break
            dt = decision.get("type", "")
            if dt in ("respond", "reject"):
                tool_messages.append(
                    ToolMessage(
                        tool_call_id=tool_call_ids[i],
                        content=decision.get("message", ""),
                        status="success" if dt == "respond" else "error",
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
                logger.info(
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
            publisher.publish_lifecycle_failed(session_id=session_id, error=error_str)
            publisher.publish_result(
                session_id=session_id,
                subtype="error_during_execution",
                duration_ms=duration_ms,
                is_error=True,
                result=error_str,
            )
            return ""

        publisher.publish_lifecycle_completed(session_id=session_id)
        final_text = remaining or ""
        publisher.publish_result(
            session_id=session_id,
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

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        if self._async_checkpointer:
            config["configurable"]["checkpointer"] = self._async_checkpointer

        try:
            self._handle_initial_setup(publisher, session_id, model_name, agent_definition)

            stream_input = await self._build_resume_input(input_payload, resume_payload, session_id)

            run = await graph.astream_events(stream_input, config, version="v3")
            async for raw_event in run:
                if not isinstance(raw_event, dict):
                    logger.info("v3_raw_event_skipped", type=str(type(raw_event)))
                    continue
                logger.info(
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
                    if span:
                        span.end()
                    return ""

            result = self._publish_final_result(
                state,
                publisher,
                session_id,
                start_time,
                num_turns,
            )

            if span:
                span.set_attribute("duration_ms", int((time.time() - start_time) * 1000))
                span.end()

            return result

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
