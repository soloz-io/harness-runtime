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
from langchain_core.messages import AIMessage

logger = structlog.get_logger(__name__)

# Monkey-patch: DeepSeek requires reasoning_content to be passed back
# for assistant messages that made tool calls with thinking mode.
# LangChain's _convert_message_to_dict drops additional_kwargs, so we
# inject reasoning_content into the serialized dict at the top level.
import langchain_openai.chat_models.base as _lc_openai_base  # noqa: E402

_original_convert_message_to_dict = _lc_openai_base._convert_message_to_dict


def _patched_convert_message_to_dict(
    message: Any,
    api: Literal["chat/completions", "responses"] = "chat/completions",
) -> dict[str, Any]:
    result = _original_convert_message_to_dict(message, api)
    if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
        result["reasoning_content"] = message.additional_kwargs["reasoning_content"]
    return result


_lc_openai_base._convert_message_to_dict = _patched_convert_message_to_dict
logger.debug("monkey_patched_convert_message_to_dict_for_reasoning_content")


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
        logger.warning("ToolStrategy not available — langchain.agents.structured_output not installed")
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
