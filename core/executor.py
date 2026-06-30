"""Agent graph executor — runs compiled LangGraph agent and streams events.

Orchestrates the execution loop: invokes the compiled graph, detects
interrupts, publishes events via the event publisher, and handles
resume via Command(resume=...).

Uses ``stream_events(version="v3")`` (deepagents-native streaming
protocol) instead of raw ``stream(stream_mode=["values", "messages"])``
so that specialist subagent content arrives as real-time token deltas
rather than post-hoc 60-character chunks.
"""

import json
import time
import uuid
from contextlib import _GeneratorContextManager
from typing import Any, Optional

import psycopg
import structlog
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from core.event_publisher import EventPublisher
from core.message_writer import write_agent_output_files, write_chat_messages


def _get_middleware_tools() -> list[dict[str, Any]]:
    """Return FilesystemMiddleware tool definitions from deepagents source of truth."""
    mw = FilesystemMiddleware()
    return [{"name": t.name, "description": t.description or ""} for t in mw.tools]


def _compute_tools(agent_definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute the full tools list for the system frame.

    For star-topology definitions, uses the root-level "tools" field.
    For custom DAG definitions, unions tools across all nodes' config.tools
    plus FilesystemMiddleware tools from deepagents.
    """
    root_tools = agent_definition.get("tools", [])
    if root_tools:
        return root_tools

    middleware_tools = _get_middleware_tools()
    seen_names: set[str] = {t["name"] for t in middleware_tools}
    tools: list[dict[str, Any]] = list(middleware_tools)

    nodes = agent_definition.get("nodes", [])
    tool_defs = agent_definition.get("tool_definitions", [])
    tool_def_map = {t.get("name"): t for t in tool_defs if isinstance(t, dict)}

    for node in nodes:
        node_config = node.get("config", {})
        for name in node_config.get("tools", []):
            if name not in seen_names:
                seen_names.add(name)
                if name in tool_def_map:
                    tools.append(dict(tool_def_map[name]))
                else:
                    tools.append({"name": name, "description": ""})

    return tools


logger = structlog.get_logger(__name__)


class ExecutionError(Exception):
    pass


class ExecutionManager:
    def __init__(
        self,
        postgres_connection_string: str,
        publisher: EventPublisher,
    ) -> None:
        self.publisher = publisher
        self.postgres_connection_string = postgres_connection_string
        self.checkpointer: Optional[PostgresSaver] = None
        self._tracer = None
        try:
            from opentelemetry import trace as _otel_trace

            self._tracer = _otel_trace.get_tracer("harness-runtime", "0.1.13")
        except Exception:
            pass
        self._checkpointer_context: Optional[_GeneratorContextManager[PostgresSaver]] = None
        self._async_checkpointer: Optional[AsyncPostgresSaver] = None
        self._async_checkpointer_context: Any = None
        self._pool: Optional[ConnectionPool] = None
        self._async_pool: Optional[AsyncConnectionPool] = None
        if postgres_connection_string:
            self._pool = ConnectionPool(postgres_connection_string, min_size=1, max_size=5)
            self._setup_checkpointer()

    def _setup_checkpointer(self) -> None:
        try:
            conn = psycopg.connect(self.postgres_connection_string, autocommit=True)
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
        if postgres_connection_string:
            self._pool = ConnectionPool(postgres_connection_string, min_size=1, max_size=5)
            await self._async_setup_checkpointer()
            self.checkpointer = self._async_checkpointer
        return self

    async def _async_setup_checkpointer(self) -> None:
        try:
            self._async_pool = AsyncConnectionPool(
                self.postgres_connection_string, min_size=1, max_size=5
            )
            aconn = await psycopg.AsyncConnection.connect(
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
    # Shared v3 event helpers
    # ------------------------------------------------------------------

    def _handle_initial_setup(
        self,
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        agent_definition: Optional[dict[str, Any]],
    ) -> None:
        tools = _compute_tools(agent_definition) if agent_definition else []
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

    def _process_v3_event(
        self,
        event: dict[str, Any],
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        start_time: float,
        num_turns: int,
    ) -> bool:
        """Process a single ProtocolEvent from stream_events(v3).

        Returns True if execution should continue, False if interrupted/ended.
        """
        method = event["method"]
        params = event["params"]
        ns = tuple(params["namespace"])
        data = params["data"]

        # --- Lifecycle events -> build namespace mapping ---
        if method == "lifecycle":
            self._handle_lifecycle(data, state, session_id, publisher, model_name)
            return True

        # --- Subagent events -> route content as tool-output-delta ---
        if ns:
            if method == "messages":
                self._handle_subagent_message(data, ns, state, publisher, session_id)
            return True

        # --- Root (coordinator) events ---

        if method == "messages":
            return self._handle_root_message(
                data, state, publisher, session_id, model_name, start_time, num_turns
            )

        if method == "tools":
            self._handle_root_tools(data, state, publisher, session_id)
            return True

        if method == "values":
            self._handle_root_values(data, state, publisher, session_id, start_time, num_turns)
            # If __interrupt__ was set we stop the run
            if state.get("_interrupted"):
                return False
            return True

        return True

    def _handle_lifecycle(
        self,
        data: dict[str, Any],
        state: dict[str, Any],
        session_id: str,
        publisher: EventPublisher,
        model_name: str,
    ) -> None:
        """Record namespace->tool_call_id mapping from lifecycle events."""
        if data.get("event") == "started":
            ns_list = data.get("namespace")
            cause = data.get("cause")
            if (
                isinstance(ns_list, list)
                and isinstance(cause, dict)
                and cause.get("type") == "toolCall"
            ):
                tool_call_id = cause.get("tool_call_id")
                if tool_call_id:
                    ns_tuple = tuple(ns_list)
                    state["ns_to_tool_call"][ns_tuple] = tool_call_id
                    state["subagent_names"][ns_tuple] = data.get("graph_name", "task")
                    logger.debug(
                        "subagent_started",
                        namespace=ns_list,
                        tool_call_id=tool_call_id,
                    )

    def _handle_subagent_message(
        self,
        data: Any,
        ns: tuple[str, ...],
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
    ) -> None:
        """Route subagent content-block-delta as tool-output-delta."""
        tool_call_id = state["ns_to_tool_call"].get(ns)
        if not tool_call_id:
            return
        subagent_name = state["subagent_names"].get(ns, "task")
        payload, _metadata = data if isinstance(data, tuple) else (data, {})
        if not isinstance(payload, dict):
            return
        event_type = payload.get("event")
        if event_type == "content-block-delta":
            delta = payload.get("delta", {})
            text = delta.get("text", "") if isinstance(delta, dict) else ""
            if text:
                publisher.publish_tool_output_delta(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    tool_name=subagent_name,
                    delta=text,
                )

    def _handle_root_message(
        self,
        data: Any,
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        start_time: float,
        num_turns: int,
    ) -> bool:
        """Handle coordinator messages events.

        - content-block-delta (text) -> publish_stream_event_text
        - content-block-start (tool_call) -> record tool_use block
        - message-finish -> publish assistant + reset message state
        """
        payload, _metadata = data if isinstance(data, tuple) else (data, {})
        if not isinstance(payload, dict):
            return True
        event_type = payload.get("event")

        if event_type == "content-block-delta":
            delta = payload.get("delta", {})
            text = delta.get("text", "") if isinstance(delta, dict) else ""
            if text:
                state["streamed_text"] += text
                publisher.publish_stream_event_text(
                    session_id=session_id,
                    text=text,
                )

        elif event_type == "content-block-start":
            content = payload.get("content", {})
            if isinstance(content, dict) and content.get("type") == "tool_call":
                state["current_tool_use_blocks"].append(
                    {
                        "type": "tool_use",
                        "id": content.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                        "name": content.get("name", "unknown"),
                        "input": content.get("args", content.get("input", {})),
                    }
                )

        elif event_type == "message-finish":
            streamed_text = state.get("streamed_text", "")
            blocks = state["current_tool_use_blocks"]
            if blocks:
                publisher.publish_assistant(
                    session_id=session_id,
                    model=model_name,
                    content=[{"type": "text", "text": streamed_text or ""}] + blocks,
                )
            state["current_tool_use_blocks"] = []
            publisher.publish_message_finish()

        return True

    def _handle_root_tools(
        self,
        data: dict[str, Any],
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
    ) -> None:
        """Handle coordinator tools events (tool-finished for non-task tools)."""
        event_type = data.get("event")
        tool_call_id = data.get("tool_call_id")
        if not tool_call_id:
            return

        if event_type == "tool-output-delta":
            delta = data.get("delta", "")
            if delta:
                publisher.publish_tool_output_delta(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    tool_name=data.get("tool_name") or data.get("name") or "unknown",
                    delta=delta,
                )

        elif event_type == "tool-finished":
            raw_output = data.get("output", "")

            # Extract the state update if the tool returned a LangGraph Command
            from langgraph.types import Command

            if isinstance(raw_output, Command) and raw_output.update:
                raw_output = raw_output.update

            # For sub-agent (task) tools, output is the full graph state
            # dict containing files + messages. Extract the last message's
            # content for display instead of serializing the entire state.
            if isinstance(raw_output, dict):
                msgs = raw_output.get("messages", [])
                if msgs:
                    last_msg = msgs[-1]
                    msg_content = getattr(last_msg, "content", "")
                    if isinstance(msg_content, list):
                        content = " ".join(
                            b.get("text", "") for b in msg_content if isinstance(b, dict)
                        )
                    elif not isinstance(msg_content, str):
                        content = str(msg_content)
                    else:
                        content = msg_content
                else:
                    content = _serialize_content(raw_output)
            else:
                content = _serialize_content(raw_output)
            publisher.publish_tool_result(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=data.get("tool_name") or data.get("name") or "unknown",
                content=content,
                is_error=bool(data.get("is_error", False)),
            )

    def _handle_root_values(
        self,
        data: dict[str, Any],
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
        start_time: float,
        num_turns: int,
    ) -> None:
        """Handle values events.

        - Interrupt detection
        - structured_response / files extraction
        - values channel publishing (only when messages change)
        """
        if not isinstance(data, dict):
            return

        # Interrupt detection
        interrupt_val = data.get("__interrupt__")
        if interrupt_val is not None:
            self._handle_interrupt(
                interrupt_val,
                state,
                publisher,
                session_id,
                start_time,
                num_turns,
            )
            return

        # Extract structured_response and files
        if "structured_response" in data:
            state["last_structured_response"] = data["structured_response"]
        state_files = data.get("files")
        if state_files:
            state["last_files"].update(state_files)

        # Track messages for values channel
        msgs = data.get("messages", [])
        prev_count = state.get("_values_messages_count", 0)
        if len(msgs) > prev_count:
            state["_values_messages_count"] = len(msgs)
            serialized_msgs = _serialize_messages_for_values(msgs)
            if serialized_msgs:
                # Write projection before SSE (narrows inconsistency window)
                if self._pool:
                    logger.debug(
                        "handle_values_writing_messages",
                        session_id=session_id,
                        new_count=len(serialized_msgs),
                        prev_count=prev_count,
                    )
                    write_chat_messages(self._pool, session_id, serialized_msgs, prev_count)
                    write_agent_output_files(self._pool, session_id, state.get("last_files"))
                else:
                    logger.warning(
                        "handle_values_no_pool_skipping_message_write",
                        session_id=session_id,
                    )
                publisher.publish_checkpoint(session_id=session_id)
                publisher.publish_values(
                    session_id=session_id,
                    messages=serialized_msgs,
                    files=state.get("last_files"),
                )

    def _handle_interrupt(
        self,
        interrupt_val: Any,
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
        start_time: float,
        num_turns: int,
    ) -> None:
        """Handle an interrupt during graph execution."""
        interrupt_payload = None
        if isinstance(interrupt_val, (list, tuple)) and len(interrupt_val) > 0:
            raw = interrupt_val[0]
            if hasattr(raw, "value"):
                interrupt_payload = raw.value
            elif isinstance(raw, dict):
                interrupt_payload = raw
            else:
                interrupt_payload = raw
        remaining = state.get("streamed_text", "")
        state["streamed_text"] = ""
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
        state["_interrupted"] = True

    def _publish_final_result(
        self,
        state: dict[str, Any],
        publisher: EventPublisher,
        session_id: str,
        start_time: float,
        num_turns: int,
        is_error: bool = False,
        error_str: str = "",
    ) -> str:
        """Publish the final result frame."""
        duration_ms = int((time.time() - start_time) * 1000)
        remaining = state.get("streamed_text", "")
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
            structured_response=state.get("last_structured_response"),
            files=state.get("last_files") or None,
        )
        return final_text

    def _init_event_state(self) -> dict[str, Any]:
        """Create the shared state dict for v3 event processing."""
        return {
            "streamed_text": "",
            "current_tool_use_blocks": [],
            "ns_to_tool_call": {},
            "subagent_names": {},
            "last_structured_response": None,
            "last_files": {},
            "_interrupted": False,
            "_values_messages_count": 0,
        }

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
        state = self._init_event_state()

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        if self._async_checkpointer:
            config["configurable"]["checkpointer"] = self._async_checkpointer

        try:
            self._handle_initial_setup(
                publisher,
                session_id,
                model_name,
                agent_definition,
            )

            stream_input = self._make_stream_input(input_payload, resume_payload)

            run = await graph.astream_events(
                stream_input,
                config,
                version="v3",
            )
            async for event in run:
                if not isinstance(event, dict):
                    continue
                ok = self._process_v3_event(
                    event,
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
            logger.error("graph_execution_failed", error=str(e))

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

        start_time = time.time()
        state = self._init_event_state()

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}

        try:
            self._handle_initial_setup(
                self.publisher,
                session_id,
                model_name,
                agent_definition,
            )

            stream_input = self._make_stream_input(input_payload, resume_payload)

            run = graph.stream_events(
                stream_input,
                config,
                version="v3",
            )
            for event in run:
                if not isinstance(event, dict):
                    continue
                ok = self._process_v3_event(
                    event,
                    state,
                    self.publisher,
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
                self.publisher,
                session_id,
                start_time,
                num_turns,
            )

            if span:
                span.set_attribute("duration_ms", int((time.time() - start_time) * 1000))
                span.end()

            return result

        except Exception as e:
            logger.error("graph_execution_failed", error=str(e))

            if span:
                span.record_exception(e)
                span.set_attribute("error", True)
                span.end()

            return self._publish_final_result(
                state,
                self.publisher,
                session_id,
                start_time,
                num_turns,
                is_error=True,
                error_str=str(e),
            )

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


def _serialize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return json.dumps(content, default=str)
    return str(content)


def _serialize_messages_for_values(messages: list) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for msg in messages:
        msg_id = getattr(msg, "id", None)
        if not msg_id:
            msg_id = str(uuid.uuid4())
        entry: dict[str, Any] = {
            "id": msg_id,
            "type": getattr(msg, "type", "unknown"),
        }
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            entry["content"] = content
        elif isinstance(content, list):
            entry["content"] = content
        else:
            entry["content"] = str(content)
        tool_calls = getattr(msg, "tool_calls", [])
        if tool_calls:
            entry["tool_calls"] = tool_calls
        additional_kwargs = getattr(msg, "additional_kwargs", {})
        if additional_kwargs:
            entry["additional_kwargs"] = additional_kwargs
        serialized.append(entry)
    return serialized
