"""
Node Compiler for harness-runtime.
Handles tool loading, model instantiation, and middleware attachment for individual nodes.
"""

from typing import Any, Dict

import structlog
from langchain_core.runnables import Runnable

try:
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.summarization import create_summarization_middleware
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware, TodoListMiddleware

    from core.middleware.human_interaction import HumanInteractionMiddleware
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

logger = structlog.get_logger(__name__)


def build_node_middleware(
    node_config: Dict[str, Any],
    model: Any,
    response_format: Any = None,
) -> list[Any]:
    """Reconstruct the essential deepagents middleware stack for a single node.

    The stack create_deep_agent gives a subagent, in its order: the filesystem,
    then summarization, then tool-call patching. Summarization is what lets a
    long node run compact its history instead of failing at the model's context
    limit; it offloads what it evicts to the same backend the filesystem uses,
    so the agent can read it back.

    When the node has a response_format, appends StructuredOutputMappingMiddleware
    so structured output fields (e.g. approved, feedback) are spread into typed
    state fields accessible to edge routers.
    """
    backend = StateBackend()
    middleware: list[Any] = [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend),
        create_summarization_middleware(model, backend),
        HumanInteractionMiddleware(),
        PatchToolCallsMiddleware(),
    ]
    interrupt_on_config = node_config.get("interrupt_on")
    if interrupt_on_config:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on_config))
    if response_format:
        from core.middleware.structured_output import (
            StructuredOutputMappingMiddleware,  # noqa: PLC0415
        )

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

    from core.middleware.structured_output import (  # noqa: PLC0415
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
    middleware = build_node_middleware(config, model, response_format)

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
