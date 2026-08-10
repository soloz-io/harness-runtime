"""
Structured Input/Output support for agent definitions.

Handles conversion of JSON Schema response_format definitions into
deepagents ToolStrategy objects and model resolution for structured
output.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Optional

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, BaseMessage

logger = structlog.get_logger(__name__)

# Monkey-patch: DeepSeek requires reasoning_content to be passed back
# for assistant messages that made tool calls with thinking mode.
# LangChain's _convert_message_to_dict drops additional_kwargs, so we
# inject reasoning_content into the serialized dict at the top level.
import langchain_openai.chat_models.base as _lc_openai_base  # noqa: E402

_original_convert_message_to_dict = _lc_openai_base._convert_message_to_dict


def _patched_convert_message_to_dict(
    message: BaseMessage,
    api: Literal["chat/completions", "responses"] = "chat/completions",
) -> dict[str, Any]:
    result = _original_convert_message_to_dict(message, api)
    if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
        result["reasoning_content"] = message.additional_kwargs["reasoning_content"]
    # Strip invalid_tool_call content blocks — OpenAI API rejects them
    if isinstance(result.get("content"), list):
        result["content"] = [
            block
            for block in result["content"]
            if not (isinstance(block, dict) and block.get("type") == "invalid_tool_call")
        ]
    return result


_lc_openai_base._convert_message_to_dict = _patched_convert_message_to_dict  # type: ignore
logger.debug("monkey_patched_convert_message_to_dict_for_reasoning_content")

_original_get_request_payload = _lc_openai_base.ChatOpenAI._get_request_payload


def sanitize_openai_messages(messages_dicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bidirectional sanitizer for OpenAI message arrays."""
    open_ids: set[str] = set()
    sanitized: list[dict[str, Any]] = []

    for msg in messages_dicts:
        if not isinstance(msg, dict):
            sanitized.append(msg)
            continue

        role = msg.get("role")

        if role == "assistant":
            # Close previous open_ids by injecting synthetic responses before this assistant turn
            for tc_id in list(open_ids):
                sanitized.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "Action completed or interrupted",
                    }
                )
            open_ids = set()

            sanitized.append(msg)

            # Register new open tool_call_ids from this assistant message
            for tc in msg.get("tool_calls") or []:
                tc_id = (
                    (tc.get("id") or tc.get("tool_call_id"))
                    if isinstance(tc, dict)
                    else (getattr(tc, "id", None) or getattr(tc, "tool_call_id", None))
                )
                if tc_id:
                    open_ids.add(tc_id)

        elif role == "tool":
            tc_id = msg.get("tool_call_id") or msg.get("id")
            if tc_id and tc_id in open_ids:
                # Valid tool response for active open tool_call — append and mark as closed
                sanitized.append(msg)
                open_ids.discard(tc_id)
            else:
                # Orphaned tool message — drop it to prevent API error
                logger.warning(
                    "sanitize_openai_messages: dropping orphaned tool message",
                    tool_call_id=tc_id,
                )

        else:
            # Non-tool, non-assistant message: close any still-open tool_calls first
            for tc_id in list(open_ids):
                sanitized.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "Action completed or interrupted",
                    }
                )
            open_ids = set()
            sanitized.append(msg)

    # Flush any remaining open_ids at end of message list
    for tc_id in list(open_ids):
        sanitized.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": "Action completed or interrupted",
            }
        )

    return sanitized


def _patched_get_request_payload(
    self: Any, input_: Any, *, stop: Any = None, **kwargs: Any
) -> dict:
    payload = _original_get_request_payload(self, input_, stop=stop, **kwargs)
    if "messages" in payload and isinstance(payload["messages"], list):
        payload["messages"] = sanitize_openai_messages(payload["messages"])
    return payload


_lc_openai_base.ChatOpenAI._get_request_payload = _patched_get_request_payload  # type: ignore
logger.debug("monkey_patched_get_request_payload_for_tool_call_sanitization")


class StructuredOutputMappingMiddleware(AgentMiddleware[Any, Any, Any]):
    """Spreads structured_response fields into typed state fields after model execution.

    deepagents/langchain stores structured output as a single opaque value in
    state["structured_response"]. Individual fields (e.g. approved, feedback) are
    never spread into typed state fields automatically. This middleware bridges that
    gap so edge routers can read e.g. state.get("approved", False).

    Works with both ToolStrategy (tool-call-based) and ProviderStrategy (JSON-mode)
    response formats. The after_model hook returns a dict that is auto-merged into
    the LangGraph state via reducers.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Pass-through — no request modification needed."""
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Async pass-through — no request modification needed."""
        return await handler(request)

    def after_model(
        self,
        state: dict[str, Any],
        runtime: Any,
    ) -> dict[str, Any] | None:
        sr = state.get("structured_response")
        if not sr or not isinstance(sr, dict):
            return None
        logger.debug(
            "structured_output_mapping_spread",
            fields=list(sr.keys()),
        )
        return dict(sr)


def build_tool_strategy(response_format: Any) -> Any:
    """Wrap a JSON schema dict into a ToolStrategy for create_deep_agent.

    Args:
        response_format: A dict with type/properties/required fields
                         (JSON Schema), or None.

    Returns:
        ToolStrategy instance if response_format is a valid dict, else None.
    """
    if not response_format or not isinstance(response_format, dict):
        return None

    try:
        from langchain.agents.structured_output import ToolStrategy

        strategy = ToolStrategy(schema=response_format)
        logger.info(
            "structured_output_tool_strategy_created",
            properties=list(response_format.get("properties", {}).keys()),
        )
        return strategy
    except ImportError:
        logger.warning(
            "ToolStrategy not available — langchain.agents.structured_output not installed"
        )
        return None
    except Exception as e:
        logger.error("failed_to_create_tool_strategy", error=str(e))
        return None


def needs_thinking_disabled(model_identifier: str, response_format: Any) -> bool:
    """Return True if the model needs thinking mode disabled for structured output.

    DeepSeek reasoning models' thinking mode is incompatible with
    tool_choice, which ToolStrategy internally requires.
    """
    if not response_format:
        return False
    return "deepseek" in model_identifier.lower()


def resolve_structured_output_model(
    provider: Optional[str],
    model_name: Optional[str],
    response_format: Any = None,
    **extra_kwargs: Any,
) -> Any:
    """Create a model instance suitable for structured output.

    For DeepSeek models with structured output, disables thinking mode
    via extra_body. For other models or no structured output, delegates
    to ModelFactory.create_model.

    Args:
        provider: Provider name (e.g. "openai", "deepseek").
        model_name: Model name (e.g. "deepseek-chat", "gpt-4o").
        response_format: Response format config (if any).
        **extra_kwargs: Additional kwargs passed to ModelFactory.create_model.

    Returns:
        A model instance (ChatOpenAI, ChatAnthropic, etc.) or a
        model identifier string if no special handling is needed.
    """
    from core.model_factory import ModelFactory

    model_identifier = ModelFactory.resolve_model_identifier(
        provider=provider,
        model_name=model_name,
    )

    if needs_thinking_disabled(model_identifier, response_format):
        logger.info(
            "disabling_deepseek_thinking_for_structured_output",
            model=model_identifier,
        )
        thinking_kwargs = {**extra_kwargs, "extra_body": {"thinking": {"type": "disabled"}}}
        return ModelFactory.create_model(
            provider=provider,
            model_name=model_name,
            **thinking_kwargs,
        )

    return ModelFactory.create_model(
        provider=provider,
        model_name=model_name,
        **extra_kwargs,
    )
