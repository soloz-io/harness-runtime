import json
import time
import uuid
from contextlib import _GeneratorContextManager
from typing import Any, Optional, cast

import structlog
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver

from core.event_publisher import EventPublisher

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
        streamed_text = ""
        final_messages = []

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}

        try:
            self.publisher.publish_system_init(
                session_id=session_id,
                model=model_name,
                tools=agent_definition.get("tools", []) if agent_definition else [],
            )

            for event in graph.stream(
                input_payload, config, stream_mode=["values", "messages", "events"]
            ):
                if not isinstance(event, tuple) or len(event) != 2:
                    continue

                mode, data = event

                if mode == "messages":
                    msg_chunk, _metadata = data
                    if hasattr(msg_chunk, "content") and isinstance(msg_chunk.content, str):
                        delta = msg_chunk.content
                        if delta:
                            streamed_text += delta
                            self.publisher.publish_stream_event_text(
                                session_id=session_id,
                                text=delta,
                            )

                elif mode == "events":
                    event_name = data.get("event", "")
                    inner = data.get("data", {})

                    if event_name == "on_chat_model_end":
                        output = inner.get("output") or inner.get("message")
                        if output is not None:
                            content_blocks: list[dict[str, Any]] = []
                            text = streamed_text
                            streamed_text = ""

                            if text:
                                content_blocks.append({
                                    "type": "text",
                                    "text": text,
                                })

                            tool_calls = _extract_tool_calls(output)
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

                    elif event_name == "on_tool_end":
                        output = inner.get("output")
                        if output is not None:
                            tool_call_id = getattr(output, "tool_call_id",
                                                    f"call_{uuid.uuid4().hex[:12]}")
                            tool_content = _serialize_content(
                                getattr(output, "content", "")
                            )
                            is_error = getattr(output, "is_error", False) or (
                                getattr(output, "additional_kwargs", {})
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

                elif mode == "values":
                    if isinstance(data, dict):
                        msgs = data.get("messages", [])
                        if len(msgs) > len(final_messages):
                            # New messages in this state update
                            new_msgs = msgs[len(final_messages):]
                            for msg in new_msgs:
                                if hasattr(msg, "type") and msg.type == "ai":
                                    content_blocks: list[dict[str, Any]] = []
                                    text = getattr(msg, "content", "")
                                    if text and isinstance(text, str):
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
                            final_messages = msgs

            remaining = streamed_text
            streamed_text = ""
            if remaining:
                self.publisher.publish_assistant(
                    session_id=session_id,
                    model=model_name,
                    content=[{"type": "text", "text": remaining}],
                )

            final_text = _extract_final_text(final_messages) or remaining
            duration_ms = int((time.time() - start_time) * 1000)

            self.publisher.publish_result(
                session_id=session_id,
                subtype="success",
                duration_ms=duration_ms,
                num_turns=num_turns,
                result=final_text,
            )

            return final_text

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("graph_execution_failed", error=str(e))
            self.publisher.publish_result(
                session_id=session_id,
                subtype="error_during_execution",
                duration_ms=duration_ms,
                is_error=True,
                result=str(e),
            )
            raise ExecutionError(f"Graph execution failed: {e}") from e

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
