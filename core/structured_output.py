"""
Structured Input/Output support for agent definitions.

Handles conversion of JSON Schema response_format definitions into
deepagents ToolStrategy objects and model resolution for structured
output (e.g., disabling DeepSeek thinking mode which conflicts with
tool_choice required by ToolStrategy).

Usage:
    from core.structured_output import build_tool_strategy, resolve_structured_output_model

    strategy = build_tool_strategy(response_format_config)
    if strategy:
        model = resolve_structured_output_model(provider, model_name)
        agent_kwargs["response_format"] = strategy
        agent_kwargs["model"] = model
"""

from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


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

    return model_identifier
