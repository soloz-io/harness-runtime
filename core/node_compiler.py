"""
Node Compiler for harness-runtime.
Handles tool loading, model instantiation, and middleware attachment for individual nodes.
"""

from typing import Any, Dict

import structlog
from langchain_core.runnables import Runnable

try:
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

logger = structlog.get_logger(__name__)


def build_node_middleware(
    node_config: Dict[str, Any],
    response_format: Any = None,
) -> list[Any]:
    """Reconstruct the essential deepagents middleware stack for a single node.

    When the node has a response_format, appends StructuredOutputMappingMiddleware
    so structured output fields (e.g. approved, feedback) are spread into typed
    state fields accessible to edge routers.
    """
    middleware: list[Any] = [
        TodoListMiddleware(),
        FilesystemMiddleware(),
        PatchToolCallsMiddleware(),
    ]
    if node_config.get("allow_ask_user", False):
        try:
            from core.ask_user_middleware import AskUserMiddleware  # noqa: PLC0415
            middleware.append(AskUserMiddleware())
        except ImportError:
            pass
    if response_format:
        from core.structured_output import StructuredOutputMappingMiddleware  # noqa: PLC0415
        middleware.append(StructuredOutputMappingMiddleware())
    return middleware


def compile_node(
    node: Dict[str, Any],
    available_tools: Dict[str, Any],
    state_schema: type,
    checkpointer: Any,
) -> Runnable[Any, Any]:
    """Compile a single node from its JSON config into a create_agent() runnable."""
    config = node.get("config", {})
    model_cfg = config.get("model", {})
    provider = model_cfg.get("provider", "openai")
    model_name = model_cfg.get("model_name") or model_cfg.get("model")

    from core.structured_output import (  # noqa: PLC0415
        resolve_structured_output_model,
    )

    response_format = config.get("response_format")
    model = resolve_structured_output_model(provider, model_name, response_format)

    tool_names = config.get("tools", [])
    tools = []
    for name in tool_names:
        if name in available_tools:
            tools.append(available_tools[name])
        else:
            logger.warning("node_tool_not_found", node=node.get("id", "unknown"), tool_name=name)

    response_format = config.get("response_format")
    middleware = build_node_middleware(config, response_format)

    kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": config.get("system_prompt", ""),
        "tools": tools,
        "middleware": middleware,
        "checkpointer": checkpointer,
        "state_schema": state_schema,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return create_agent(**kwargs)
