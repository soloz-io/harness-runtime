"""
Structured Input/Output support for agent definitions.

Handles conversion of JSON Schema response_format definitions into
deepagents ToolStrategy objects and model resolution for structured
output.

NOTE: Inter-node communication is artifact-based (filesystem via
read_file/write_file), not message-history-based. Message contexts are
isolated per node in the custom DAG. DeepSeek thinking mode is disabled
for structured output when needed (see resolve_structured_output_model).
"""

from typing import Any, Optional

import structlog
from langchain.agents.middleware import AgentMiddleware
from pydantic import BaseModel


logger = structlog.get_logger(__name__)


def _dict_to_pydantic_model(schema: dict[str, Any]) -> type[BaseModel]:
    """Convert a JSON schema dict to a dynamically-created Pydantic model.

    LangChain's _parse_with_schema bypasses validation for raw JSON schema
    dicts (schema_kind == "json_schema" returns data as-is). By using a
    Pydantic model, validation errors on empty/malformed tool call args
    trigger LangChain's built-in handle_errors retry mechanism, causing
    the model to retry with correct arguments.

    Args:
        schema: A JSON schema dict with properties and optional required fields.

    Returns:
        A dynamically-created Pydantic BaseModel subclass.
    """
    from pydantic import create_model

    title = schema.get("title", "ResponseFormat")
    required = set(schema.get("required", []))
    fields: dict[str, tuple[type, Any]] = {}

    for prop_name, prop_schema in schema.get("properties", {}).items():
        prop_type = prop_schema.get("type", "string")

        if prop_type == "boolean":
            field_type = bool
        elif prop_type == "integer":
            field_type = int
        elif prop_type == "number":
            field_type = float
        elif prop_type == "string":
            field_type = str
        else:
            field_type = Optional[Any]

        if prop_name in required:
            fields[prop_name] = (field_type, ...)
        else:
            fields[prop_name] = (Optional[field_type], None)

    return create_model(title, **fields)


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


    def after_model(
        self,
        state: dict[str, Any],
        runtime: Any,
    ) -> dict[str, Any] | None:
        sr = state.get("structured_response")

        if isinstance(sr, dict):
            if not sr:
                return None
            logger.debug(
                "structured_output_mapping_spread",
                fields=list(sr.keys()),
            )
            return dict(sr)

        if hasattr(sr, "model_dump"):
            dumped = sr.model_dump()
            logger.debug(
                "structured_output_mapping_spread_from_pydantic",
                fields=list(dumped.keys()),
            )
            return dumped

        return None


def build_tool_strategy(response_format: Any) -> Any:
    """Wrap a JSON schema dict into a ToolStrategy for create_deep_agent.

    Converts raw JSON schema dicts to Pydantic models first to ensure
    validation catches empty/malformed tool call args, triggering
    LangChain's built-in handle_errors retry mechanism.

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

        schema = response_format
        if "properties" in schema:
            schema = _dict_to_pydantic_model(schema)

        strategy = ToolStrategy(schema=schema)
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
