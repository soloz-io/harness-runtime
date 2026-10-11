"""
Invalid tool calls go back to the model instead of ending the turn.

A model sometimes writes a tool call whose arguments are not valid JSON (an
extra closing brace, a truncated string). LangChain records it as an
``invalid_tool_call`` and never runs it, and because the response then carries
no valid tool call, the agent loop treats it as the agent's final answer: the
turn ends silently. An ``ask_user`` lost this way shows the user nothing at all
— no question, no dialog — and the agent believes it asked.

After such a response, this middleware answers each invalid call with an error
the model can act on (the parser's own message) and sends the agent back to the
model to call it again. Bounded: after ``MAX_RETRIES`` consecutive responses of
nothing but invalid calls, the turn ends as before, so a model that cannot form
the call does not loop.
"""

from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, InvalidToolCall, ToolMessage

logger = structlog.get_logger(__name__)

MAX_RETRIES = 2


def _only_invalid(message: Any) -> bool:
    return (
        isinstance(message, AIMessage)
        and bool(message.invalid_tool_calls)
        and not message.tool_calls
    )


def _retry_message(call: InvalidToolCall) -> ToolMessage | None:
    call_id = call.get("id")
    if not call_id:
        return None
    name = call.get("name") or "the tool"
    error = call.get("error") or "the arguments could not be parsed"
    return ToolMessage(
        content=(
            f"Your {name} call was not run: its arguments are not valid JSON ({error}). "
            f"Nothing was done. Call {name} again with valid JSON arguments."
        ),
        name=call.get("name") or "unknown",
        tool_call_id=call_id,
        status="error",
    )


class InvalidToolCallRetryMiddleware(AgentMiddleware):
    """Answer unparseable tool calls with an error and let the model call again."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        messages = state.get("messages") or []
        if not messages or not _only_invalid(messages[-1]):
            return None

        # Consecutive responses of nothing but invalid calls, counting this one.
        streak = 0
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                continue
            if not _only_invalid(message):
                break
            streak += 1
        last: AIMessage = messages[-1]
        names = [c.get("name") for c in last.invalid_tool_calls]
        if streak > MAX_RETRIES:
            logger.warning("invalid_tool_call_gave_up", tools=names, attempts=streak)
            return None

        replies = [m for m in (_retry_message(c) for c in last.invalid_tool_calls) if m is not None]
        if not replies:
            return None
        logger.warning("invalid_tool_call_retry", tools=names, attempt=streak)
        return {"messages": replies, "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
