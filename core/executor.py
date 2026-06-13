import json
import time
import uuid
from contextlib import _GeneratorContextManager
from typing import Any, Optional, cast

import structlog
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver

from core.event_publisher import EventPublisher


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
        self._checkpointer_context: Optional[_GeneratorContextManager[PostgresSaver]] = None
        self._setup_checkpointer()

    def _setup_checkpointer(self) -> None:
        try:
            ctx = PostgresSaver.from_conn_string(
                self.postgres_connection_string
            )
            self._checkpointer_context = ctx
            self.checkpointer = ctx.__enter__()
            self.checkpointer.setup()
        except Exception as e:
            logger.error("checkpointer_setup_failed", error=str(e))
            raise

    def execute(
        self,
        graph: Runnable,
        session_id: str,
        input_payload: dict[str, Any],
        model_name: str,
        agent_definition: Optional[dict[str, Any]] = None,
        num_turns: int = 1,
    ) -> str:
        start_time = time.time()
        final_messages: list[Any] = []
        prev_msg_count = 0
        last_structured_response = None
        last_files: dict[str, Any] = {}

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}

        tools = _compute_tools(agent_definition) if agent_definition else []
        self.publisher.publish_system_init(
            session_id=session_id,
            model=model_name,
            tools=tools,
        )

        completed_normally = False
        error_message: Optional[str] = None

        try:
            for event in graph.stream(
                input_payload, config, stream_mode=["values", "messages"]
            ):
                if not isinstance(event, tuple) or len(event) != 2:
                    continue

                mode, data = event

                if mode == "messages":
                    msg_chunk, _metadata = data
                    if hasattr(msg_chunk, "content") and isinstance(msg_chunk.content, str):
                        delta = msg_chunk.content
                        if delta:
                            self.publisher.publish_stream_event_text(
                                session_id=session_id,
                                text=delta,
                            )

                elif mode == "values":
                    if isinstance(data, dict):
                        msgs = data.get("messages", [])

                        # Detect and emit frames for new messages since last snapshot
                        new_msgs = msgs[prev_msg_count:]
                        for msg in new_msgs:
                            msg_type = getattr(msg, "type", "")
                            if msg_type == "ai":
                                content_blocks: list[dict[str, Any]] = []
                                text = getattr(msg, "content", "") or ""
                                if text:
                                    content_blocks.append({
                                        "type": "text",
                                        "text": text,
                                    })
                                tool_calls = _extract_tool_calls(msg)
                                for tc in tool_calls:
                                    content_blocks.append({
                                        "type": "tool_use",
                                        "id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                                        "name": tc.get("name", "unknown"),
                                        "input": tc.get("args", tc.get("input", {})),
                                    })
                                if content_blocks:
                                    self.publisher.publish_assistant(
                                        session_id=session_id,
                                        model=model_name,
                                        content=content_blocks,
                                    )

                            elif msg_type == "tool":
                                tool_call_id = getattr(
                                    msg, "tool_call_id", f"call_{uuid.uuid4().hex[:12]}"
                                )
                                tool_content = _serialize_content(
                                    getattr(msg, "content", "")
                                )
                                is_error = getattr(msg, "is_error", False) or (
                                    getattr(msg, "additional_kwargs", {})
                                    .get("is_error", False)
                                )
                                self.publisher.publish_user_echo(
                                    session_id=session_id,
                                    content=[{
                                        "type": "tool_result",
                                        "tool_use_id": tool_call_id,
                                        "content": tool_content,
                                        "is_error": is_error,
                                    }],
                                )

                        prev_msg_count = len(msgs)

                        if len(msgs) > len(final_messages):
                            final_messages = msgs
                        if "structured_response" in data:
                            last_structured_response = data["structured_response"]
                        state_files = data.get("files")
                        if state_files:
                            last_files.update(state_files)

            completed_normally = True

        except Exception as e:
            error_message = str(e)
            logger.error("graph_execution_failed", error=error_message)

        duration_ms = int((time.time() - start_time) * 1000)

        if completed_normally:
            final_text = _extract_final_text(final_messages)
            self.publisher.publish_result(
                session_id=session_id,
                subtype="success",
                duration_ms=duration_ms,
                num_turns=num_turns,
                result=final_text,
                structured_response=last_structured_response,
                files=last_files if last_files else None,
            )
            return final_text
        else:
            self.publisher.publish_result(
                session_id=session_id,
                subtype="error_during_execution",
                duration_ms=duration_ms,
                is_error=True,
                result=error_message or "Unknown execution error",
            )
            raise ExecutionError(f"Graph execution failed: {error_message or 'Unknown error'}") from (
                None if not error_message else Exception(error_message)
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
        except Exception as e:
            logger.error("execution_manager_close_failed", error=str(e))

    def __enter__(self) -> "ExecutionManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def _extract_tool_calls(output: Any) -> list[dict[str, Any]]:
    raw = getattr(output, "tool_calls", []) or []
    if isinstance(raw, list):
        return raw
    additional_kwargs = getattr(output, "additional_kwargs", {})
    return cast(list[dict[str, Any]], additional_kwargs.get("tool_calls", []))


def _serialize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return json.dumps(content, default=str)
    return str(content)


def _extract_final_text(messages: list) -> str:
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content:
            if hasattr(msg, "type") and msg.type == "ai":
                return msg.content if isinstance(msg.content, str) else str(msg.content)
    if messages:
        last = messages[-1]
        if hasattr(last, "content"):
            content = last.content
            return content if isinstance(content, str) else str(content)
    return ""
